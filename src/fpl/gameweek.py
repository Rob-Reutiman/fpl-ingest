"""Deciding which gameweek is ready to ingest.

The idempotency check and the fixtures fetch arrive as callables, keeping this
module free of the network and the bucket.
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
    """R2 metadata marking a gameweek ingested with fixtures outstanding.

    Living in metadata keeps the stored body exactly as the API returned it and
    leaves the key stable for the idempotency check.
    """
    if not target.partial:
        return {}
    return {
        "partial": "true",
        "pending-fixtures": ",".join(str(i) for i in target.pending_fixture_ids),
    }


def _kickoff(fixture: Fixture) -> datetime | None:
    raw = fixture.get("kickoff_time")
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_effectively_complete(fixtures: Iterable[Fixture], now: datetime) -> tuple[bool, list[int]]:
    """Report whether a gameweek is done bar one or more postponed fixtures.

    One rescheduled match holds a gameweek unverified for months. Once every
    unfinished fixture has a null or distant kickoff, the rest of the gameweek
    has stabilised and is worth capturing.

    Returns ``(complete, pending_fixture_ids)``. A gameweek whose fixtures have
    all finished is still settling its bonus points, and reads as incomplete.
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
    """The earliest gameweek that is ready and absent from the bucket, else None.

    Ready means `data_checked`, since bonus points and autosubs go on being
    revised for hours after the last whistle, or held up by postponed fixtures
    alone, which returns a partial target. Yields at most one gameweek a call,
    so a backlog clears one run at a time.
    """
    now = now or datetime.now(UTC)

    for event in sorted(events, key=lambda e: e["id"]):
        gw = event["id"]
        if not event.get("finished") or already_ingested(gw):
            continue
        if event.get("data_checked"):
            return Target(gw)

        # Finished but unverified. The one case worth an extra fixtures call.
        complete, pending = is_effectively_complete(fetch_fixtures(gw), now)
        if complete:
            return Target(gw, partial=True, pending_fixture_ids=pending)

    return None
