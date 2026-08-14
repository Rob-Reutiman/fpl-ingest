"""Double gameweeks: getting per-fixture stats when a club plays twice.

`event/{gw}/live/` reports a player's stats **aggregated over the gameweek**. Its
`explain` array is broken out per fixture, but only carries point-scoring
identifiers — and xG/xA don't score points, so the per-fixture xG split isn't
recoverable from that endpoint. `element-summary/{id}`'s `history[]` *is* genuinely
per-fixture and includes xG, so DGW players are fetched from there instead.

This costs one extra request per affected player — typically 40-60 players, a
handful of times a season — and only for clubs with two fixtures in the gameweek.

`reconcile` checks the two endpoints against each other whenever a DGW is
processed: the per-fixture rows should sum to the gameweek aggregate. That both
verifies the fallback is doing its job and would catch FPL changing the shape of
either endpoint, which is otherwise the kind of thing you discover months later in
a model that has quietly been wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fpl.transforms.match_facts import ELEMENT_SUMMARY, stat_columns

logger = logging.getLogger(__name__)

# Summing these across a player's fixtures must reproduce the live aggregate.
RECONCILED_STATS = ("minutes", "total_points", "goals_scored", "assists", "bps")


def teams_with_multiple_fixtures(fixtures: Iterable[dict[str, Any]]) -> set[int]:
    counts: dict[int, int] = {}
    for fixture in fixtures:
        for team in (fixture["team_h"], fixture["team_a"]):
            counts[team] = counts.get(team, 0) + 1
    return {team for team, count in counts.items() if count > 1}


def affected_elements(element_teams: dict[int, int], doubled_teams: set[int]) -> list[int]:
    """Players needing the per-fixture fallback, in id order for a stable run."""
    return sorted(element_id for element_id, team in element_teams.items() if team in doubled_teams)


def history_rows(
    summary: dict[str, Any],
    element_id: int,
    gameweek: int,
    fixtures_by_id: dict[int, dict[str, Any]],
    element_type: int | None,
) -> list[dict[str, Any]]:
    """One row per fixture from `element-summary`'s `history[]`.

    The team is derived from the fixture and `was_home`, exactly as the backfill
    does it — so it is pinned to the fixture rather than to the player's current
    club, for free.
    """
    rows: list[dict[str, Any]] = []
    for entry in summary.get("history", []):
        if int(entry.get("round", -1)) != gameweek:
            continue
        fixture = fixtures_by_id.get(int(entry["fixture"]))
        if fixture is None:
            continue
        was_home = bool(entry["was_home"])
        rows.append(
            {
                "element_id": element_id,
                "fixture_id": int(entry["fixture"]),
                "team_id": fixture["team_h"] if was_home else fixture["team_a"],
                "opponent_team_id": int(entry["opponent_team"]),
                "was_home": was_home,
                "kickoff_time": entry.get("kickoff_time") or fixture.get("kickoff_time"),
                "element_type": element_type,
                "value": entry.get("value"),
                "source": ELEMENT_SUMMARY,
                **stat_columns(entry),
            }
        )
    return rows


def reconcile(
    element_id: int,
    per_fixture: list[dict[str, Any]],
    live_stats: dict[str, Any],
) -> list[str]:
    """Compare the summed per-fixture rows against the live aggregate.

    Returns a list of human-readable mismatches — empty when they agree. A
    mismatch doesn't fail the job: `element-summary` is the more granular source
    and is what we keep. But it means one of the two endpoints changed shape, and
    that is worth shouting about.
    """
    problems: list[str] = []
    for stat in RECONCILED_STATS:
        aggregate = live_stats.get(stat)
        if aggregate is None:
            continue
        total = sum(row.get(stat) or 0 for row in per_fixture)
        if total != aggregate:
            problems.append(f"{stat}: fixtures sum to {total}, live reports {aggregate}")
    if problems:
        logger.warning(
            "element %d: per-fixture stats disagree with the gameweek aggregate (%s). "
            "Keeping the per-fixture rows; check whether the API changed shape.",
            element_id,
            "; ".join(problems),
        )
    return problems
