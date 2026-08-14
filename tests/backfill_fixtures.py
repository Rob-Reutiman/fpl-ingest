"""Builders for tiny archive-shaped CSVs.

The transforms run for real against DuckDB — only the download is faked — so a
fixture season is a handful of rows written to disk in exactly the archive's shape.
Each builder defaults to a coherent minimal season that individual tests bend.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fpl.backfill.archive import SeasonSources

# Four clubs is enough to exercise home/away, blanks and doubles without the
# fixtures list becoming something you have to read twice.
TEAMS: tuple[dict[str, Any], ...] = (
    {"id": 1, "name": "Arsenal", "short_name": "ARS"},
    {"id": 2, "name": "Brentford", "short_name": "BRE"},
    {"id": 3, "name": "Chelsea", "short_name": "CHE"},
    {"id": 4, "name": "Everton", "short_name": "EVE"},
)

MERGED_GW_COLUMNS = (
    "name",
    "position",
    "team",
    "xP",
    "assists",
    "bonus",
    "bps",
    "clean_sheets",
    "creativity",
    "element",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals",
    "expected_goals_conceded",
    "fixture",
    "goals_conceded",
    "goals_scored",
    "ict_index",
    "influence",
    "kickoff_time",
    "minutes",
    "opponent_team",
    "own_goals",
    "penalties_missed",
    "penalties_saved",
    "red_cards",
    "round",
    "saves",
    "selected",
    "starts",
    "team_a_score",
    "team_h_score",
    "threat",
    "total_points",
    "transfers_balance",
    "transfers_in",
    "transfers_out",
    "value",
    "was_home",
    "yellow_cards",
    "GW",
)


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def team_rows() -> list[dict[str, Any]]:
    return [
        {
            **team,
            "code": 100 + team["id"],
            "strength": 3,
            "strength_overall_home": 1200,
            "strength_overall_away": 1200,
            "strength_attack_home": 1200,
            "strength_attack_away": 1200,
            "strength_defence_home": 1200,
            "strength_defence_away": 1200,
        }
        for team in TEAMS
    ]


def player(
    element_id: int,
    first_name: str,
    second_name: str,
    *,
    code: int | None = None,
    team_id: int = 1,
    element_type: int = 3,
    web_name: str | None = None,
    total_points: int = 0,
) -> dict[str, Any]:
    return {
        "id": element_id,
        "code": "" if code is None else code,
        "first_name": first_name,
        "second_name": second_name,
        "web_name": web_name or second_name,
        "element_type": element_type,
        "team": team_id,
        "total_points": total_points,
    }


def fixture(
    fixture_id: int,
    gameweek: int | None,
    home: int,
    away: int,
    *,
    home_score: int = 1,
    away_score: int = 0,
    finished: bool = True,
) -> dict[str, Any]:
    return {
        "id": fixture_id,
        "event": "" if gameweek is None else gameweek,
        "team_h": home,
        "team_a": away,
        "team_h_score": home_score,
        "team_a_score": away_score,
        "finished": str(finished).lower(),
        "finished_provisional": str(finished).lower(),
        "kickoff_time": f"2024-08-{10 + (gameweek or 1):02d}T14:00:00Z",
        "team_h_difficulty": 3,
        "team_a_difficulty": 3,
    }


def appearance(
    element_id: int,
    name: str,
    fixture_id: int,
    gameweek: int,
    *,
    team: str,
    opponent: int,
    was_home: bool,
    position: str = "MID",
    minutes: int = 90,
    total_points: int = 2,
    expected_goals: float = 0.1,
    expected_assists: float = 0.05,
    expected_goals_conceded: float = 1.1,
    value: int = 50,
    defensive_contribution: int | None = None,
    starts: int | None = 1,
) -> dict[str, Any]:
    row = {
        "name": name,
        "position": position,
        "team": team,
        "xP": 3.5,
        "element": element_id,
        "fixture": fixture_id,
        "round": gameweek,
        "GW": gameweek,
        "opponent_team": opponent,
        "was_home": str(was_home),
        "kickoff_time": f"2024-08-{10 + gameweek:02d}T14:00:00Z",
        "minutes": minutes,
        "total_points": total_points,
        "expected_goals": expected_goals,
        "expected_assists": expected_assists,
        "expected_goal_involvements": round(expected_goals + expected_assists, 4),
        "expected_goals_conceded": expected_goals_conceded,
        "value": value,
        "selected": 1000,
        "transfers_in": 10,
        "transfers_out": 5,
        "transfers_balance": 5,
        "assists": 0,
        "bonus": 0,
        "bps": 10,
        "clean_sheets": 0,
        "creativity": 1.0,
        "goals_conceded": 1,
        "goals_scored": 0,
        "ict_index": 1.0,
        "influence": 1.0,
        "own_goals": 0,
        "penalties_missed": 0,
        "penalties_saved": 0,
        "red_cards": 0,
        "saves": 0,
        "team_a_score": 0,
        "team_h_score": 1,
        "threat": 1.0,
        "yellow_cards": 0,
    }
    if starts is not None:
        row["starts"] = starts
    if defensive_contribution is not None:
        row["defensive_contribution"] = defensive_contribution
    return row


def write_season(
    root: Path,
    season: str,
    *,
    players: Sequence[dict[str, Any]],
    fixtures: Sequence[dict[str, Any]],
    appearances: Sequence[dict[str, Any]],
    teams: Sequence[dict[str, Any]] | None = None,
    with_defcon: bool = False,
    with_starts: bool = True,
) -> SeasonSources:
    """Write one fixture season to disk and return it as `SeasonSources`."""
    season_dir = root / season
    columns: list[str] = list(MERGED_GW_COLUMNS)
    if with_starts is False:
        columns.remove("starts")
    if with_defcon:
        columns.append("defensive_contribution")

    _write_csv(season_dir / "merged_gw.csv", columns, appearances)
    _write_csv(
        season_dir / "players_raw.csv",
        ["id", "code", "first_name", "second_name", "web_name", "element_type", "team"],
        players,
    )
    _write_csv(
        season_dir / "cleaned_players.csv",
        ["first_name", "second_name", "total_points", "element_type"],
        [
            {
                "first_name": p["first_name"],
                "second_name": p["second_name"],
                "total_points": p["total_points"],
                "element_type": p["element_type"],
            }
            for p in players
        ],
    )
    _write_csv(
        season_dir / "teams.csv",
        [
            "code",
            "id",
            "name",
            "short_name",
            "strength",
            "strength_overall_home",
            "strength_overall_away",
            "strength_attack_home",
            "strength_attack_away",
            "strength_defence_home",
            "strength_defence_away",
        ],
        list(teams or team_rows()),
    )
    _write_csv(
        season_dir / "fixtures.csv",
        [
            "code",
            "event",
            "finished",
            "finished_provisional",
            "id",
            "kickoff_time",
            "team_a",
            "team_a_score",
            "team_h",
            "team_h_score",
            "team_h_difficulty",
            "team_a_difficulty",
        ],
        fixtures,
    )
    (season_dir / "DATA_DICTIONARY.md").write_text("# fixture data dictionary\n", encoding="utf-8")

    return SeasonSources(
        season=season,
        paths={
            name: season_dir / name
            for name in (
                "merged_gw.csv",
                "players_raw.csv",
                "cleaned_players.csv",
                "teams.csv",
                "fixtures.csv",
                "DATA_DICTIONARY.md",
            )
        },
    )
