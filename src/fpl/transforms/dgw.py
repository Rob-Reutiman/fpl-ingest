"""Recovers stats at fixture grain when a club plays twice in one gameweek.

`FPLClient.event_live` reports a player's stats summed over the whole gameweek.
Its `explain` array splits by fixture but names only the events that score
points, and xG and xA score none, so the split for those stays out of reach.
`FPLClient.element_summary` returns a `history` at fixture grain carrying xG,
and supplies these players instead.

The fallback costs one request per affected player, typically 40 to 60 of them,
a handful of times a season.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fpl.transforms.match_facts import ELEMENT_SUMMARY, stat_columns

logger = logging.getLogger(__name__)

# Summing these across a player's fixtures reproduces the gameweek total.
RECONCILED_STATS = ("minutes", "total_points", "goals_scored", "assists", "bps")


def teams_with_multiple_fixtures(fixtures: Iterable[dict[str, Any]]) -> set[int]:
    counts: dict[int, int] = {}
    for fixture in fixtures:
        for team in (fixture["team_h"], fixture["team_a"]):
            counts[team] = counts.get(team, 0) + 1
    return {team for team, count in counts.items() if count > 1}


def affected_elements(element_teams: dict[int, int], doubled_teams: set[int]) -> list[int]:
    """Players needing the fixture grain fallback, in id order for a stable run."""
    return sorted(element_id for element_id, team in element_teams.items() if team in doubled_teams)


def history_rows(
    summary: dict[str, Any],
    element_id: int,
    gameweek: int,
    fixtures_by_id: dict[int, dict[str, Any]],
    element_type: int | None,
) -> list[dict[str, Any]]:
    """Build one row per fixture from an element summary `history`.

    The club comes from the fixture and `was_home`, pinning it to the match as
    played whatever the player's registration says today.
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
    """Compare the summed fixture rows against the gameweek total.

    Returns readable descriptions of any mismatch, empty when the two agree. The
    job keeps the finer grained rows and carries on. A mismatch points at one of
    the two endpoints changing shape, which otherwise surfaces months later
    inside a model that has been quietly wrong.
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
