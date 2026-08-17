"""Deciding which gameweek is ready to ingest.

The idempotency check and the fixtures fetch arrive as callables, keeping this
module free of the network and the bucket.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fpl.constants import SETTLEMENT_LEAD_HOURS

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


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_effectively_complete(
    fixtures: Iterable[Fixture], cutoff: datetime
) -> tuple[bool, list[int]]:
    """Report whether every unfinished fixture is stuck at or beyond `cutoff`.

    A fixture counts as stuck once its kickoff is null or falls at or after
    `cutoff`, meaning it cannot complete in the time available. A gameweek with
    no unfinished fixtures at all is stuck by the same measure, vacuously.

    Returns ``(complete, pending_fixture_ids)``, the latter naming every
    unfinished fixture when `complete` is true.
    """
    unfinished = [f for f in fixtures if not f.get("finished")]
    pending = [
        f["id"]
        for f in unfinished
        if (kickoff := _parse_datetime(f.get("kickoff_time"))) is None or kickoff >= cutoff
    ]
    if len(pending) == len(unfinished):
        return True, pending
    return False, []


def resolve_target(
    events: Iterable[Event],
    *,
    already_ingested: Callable[[int], bool],
    fetch_fixtures: Callable[[int], list[Fixture]],
    now: datetime | None = None,
) -> Target | None:
    """The earliest gameweek that is ready and absent from the bucket, else None.

    A gameweek becomes a candidate once its own deadline has passed. From there,
    `data_checked` is trusted outright. Short of that, the gameweek is held for
    `data_checked` until `now` closes to within `SETTLEMENT_LEAD_HOURS` of the
    following gameweek's deadline, at which point its fixtures are checked
    directly and it is ingested as partial if none can complete in time. The
    final gameweek of a season has no following deadline, so it is held for
    `data_checked` indefinitely.

    Yields at most one gameweek a call, so a backlog clears one run at a time.
    """
    now = now or datetime.now(UTC)
    ordered = sorted(events, key=lambda e: e["id"])

    for index, event in enumerate(ordered):
        gw = event["id"]
        if already_ingested(gw):
            continue

        deadline = _parse_datetime(event.get("deadline_time"))
        if deadline is None or now < deadline:
            continue
        if event.get("data_checked"):
            return Target(gw)

        next_event = ordered[index + 1] if index + 1 < len(ordered) else None
        if next_event is None:
            continue
        next_deadline = _parse_datetime(next_event.get("deadline_time"))
        if next_deadline is None or now < next_deadline - timedelta(hours=SETTLEMENT_LEAD_HOURS):
            continue

        complete, pending = is_effectively_complete(fetch_fixtures(gw), next_deadline)
        if complete:
            return Target(gw, partial=True, pending_fixture_ids=pending)

    return None
