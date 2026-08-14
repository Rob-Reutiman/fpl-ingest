"""Deciding which gameweek is ready to ingest.

Pure logic — the caller supplies the R2 idempotency check and the fixtures
fetch as callables, so this module needs no network and no bucket.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fpl.constants import POSTPONED_FIXTURE_THRESHOLD_HOURS

Event = dict[str, Any]
Fixture = dict[str, Any]


@dataclass(frozen=True)
class Target:
    """A gameweek that is ready to ingest."""

    gw: int
    partial: bool = False
    pending_fixture_ids: list[int] = field(default_factory=list)


def partial_metadata(target: Target) -> dict[str, str]:
    """R2 object metadata marking a gameweek ingested with fixtures outstanding.

    Carried as metadata rather than injected into the payload, so the stored
    bytes stay identical to what the API returned and the key stays stable for
    the idempotency check.
    """
    if not target.partial:
        return {}
    return {
        "partial": "true",
        "pending-fixtures": ",".join(str(i) for i in target.pending_fixture_ids),
    }


def settled_gameweeks(events: Iterable[Event]) -> list[int]:
    """Gameweek numbers that FPL has finished *and* verified, ascending.

    `finished` alone is not enough: bonus points and autosubs are still revised
    for a few hours after the last whistle. `data_checked` is the settle signal.
    """
    return sorted(e["id"] for e in events if e.get("finished") and e.get("data_checked"))


def _kickoff(fixture: Fixture) -> datetime | None:
    raw = fixture.get("kickoff_time")
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_effectively_complete(fixtures: Iterable[Fixture], now: datetime) -> tuple[bool, list[int]]:
    """Whether a gameweek is done bar one or more postponed fixtures.

    A single rescheduled match holds `data_checked` at false for its whole
    gameweek, potentially for months. If every unfinished fixture in the event
    has no kickoff time or one far in the future, the rest of the gameweek's
    data is stable and worth capturing — flagged partial.

    Returns ``(complete, pending_fixture_ids)``. All fixtures finished but
    `data_checked` still false is *not* complete: that is the normal
    bonus-points settling window, not a postponement.
    """
    unfinished = [f for f in fixtures if not f.get("finished")]
    if not unfinished:
        return False, []

    horizon = now + timedelta(hours=POSTPONED_FIXTURE_THRESHOLD_HOURS)
    for fixture in unfinished:
        kickoff = _kickoff(fixture)
        if kickoff is not None and kickoff <= horizon:
            return False, []

    return True, [f["id"] for f in unfinished]


def resolve_target(
    events: Iterable[Event],
    *,
    already_ingested: Callable[[int], bool],
    fetch_fixtures: Callable[[int], list[Fixture]],
    now: datetime | None = None,
) -> Target | None:
    """The earliest gameweek that is ready and not yet in the bucket.

    Returns ``None`` on the common day, when there is nothing new. At most one
    gameweek per run, so a backlog is worked off one per day rather than in one
    long burst.
    """
    now = now or datetime.now(UTC)

    for event in sorted(events, key=lambda e: e["id"]):
        gw = event["id"]
        if not event.get("finished") or already_ingested(gw):
            continue
        if event.get("data_checked"):
            return Target(gw)

        # Finished but unverified — the only case worth the extra fixtures call.
        complete, pending = is_effectively_complete(fetch_fixtures(gw), now)
        if complete:
            return Target(gw, partial=True, pending_fixture_ids=pending)

    return None
