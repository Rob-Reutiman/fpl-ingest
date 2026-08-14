"""Job 2's transforms: one settled gameweek into `fact_player_fixture`.

The three grain rules from the spec are enforced here, and they are the reason
this module can't just dump `event/{gw}/live/`:

* **Blank gameweek → no row.** `event/{gw}/live/` returns *every* player in the
  game, including those whose club had no fixture. Writing those through would
  put a zero-minute row against a blank, and every rolling window downstream
  would read it as a genuine non-appearance. The join to the gameweek's fixtures
  is what drops them.
* **Didn't feature → a row with `minutes = 0`.** Same endpoint, opposite
  treatment: their club did play, so the row belongs.
* **Double gameweek → two rows.** `event/{gw}/live/` aggregates a player's stats
  across both fixtures, so a DGW player's stats are fetched per fixture from
  `element-summary/{id}` instead. See `dgw.py`.

Team attribution is pinned at ingest time from the bootstrap snapshot taken in
the same run. Football facts are immutable once written, so a January transfer
can never reach back and reattribute an August fixture.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

EVENT_LIVE = "event_live"
ELEMENT_SUMMARY = "element_summary"

# Stat columns the contract takes straight off a live `stats` object or an
# element-summary `history[]` entry. Both endpoints use the same field names.
STAT_COLUMNS = (
    "minutes",
    "starts",
    "goals_scored",
    "assists",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "clean_sheets",
    "goals_conceded",
    "expected_goals_conceded",
    "saves",
    "penalties_saved",
    "defensive_contribution",
    "yellow_cards",
    "red_cards",
    "own_goals",
    "penalties_missed",
    "bps",
    "bonus",
    "total_points",
    "influence",
    "creativity",
    "threat",
    "ict_index",
)

# FPL sends these as decimal strings; the rest arrive as integers.
_DECIMAL_STATS = frozenset(
    {
        "influence",
        "creativity",
        "threat",
        "ict_index",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "expected_goals_conceded",
    }
)


def stat_columns(stats: dict[str, Any]) -> dict[str, Any]:
    """Pull the contract's stat columns off a live or element-summary payload.

    A stat the season doesn't report stays NULL. Coercing it to 0 would claim it
    was measured and happened to be zero — the distinction the whole schema turns
    on for `defensive_contribution`.
    """
    columns: dict[str, Any] = {}
    for name in STAT_COLUMNS:
        raw = stats.get(name)
        columns[name] = float(raw) if raw is not None and name in _DECIMAL_STATS else raw
    return columns


def appearance_rows(
    live: dict[str, Any],
    fixtures: list[dict[str, Any]],
    element_teams: dict[int, int],
    element_types: dict[int, int],
    values: dict[int, int],
) -> list[dict[str, Any]]:
    """Flatten `event/{gw}/live/` into one row per (player, fixture).

    Players whose club had no fixture this gameweek produce nothing — that is the
    blank-gameweek rule, and it is enforced by construction rather than filtered
    afterwards.
    """
    by_team: dict[int, list[dict[str, Any]]] = {}
    for fixture in fixtures:
        by_team.setdefault(fixture["team_h"], []).append(fixture)
        by_team.setdefault(fixture["team_a"], []).append(fixture)

    rows: list[dict[str, Any]] = []
    for element in live.get("elements", []):
        element_id = int(element["id"])
        team_id = element_teams.get(element_id)
        if team_id is None:
            # In the live payload but not in bootstrap — a player removed from the
            # game mid-week. No club, so no fixture to attribute anything to.
            continue
        played = by_team.get(team_id, [])
        if not played:
            continue
        if len(played) > 1:
            # Handled from element-summary, where the stats are split per fixture.
            continue
        rows.append(
            _row(
                element_id=element_id,
                team_id=team_id,
                fixture=played[0],
                stats=element.get("stats", {}),
                element_type=element_types.get(element_id),
                value=values.get(element_id),
                source=EVENT_LIVE,
            )
        )
    return rows


def _row(
    *,
    element_id: int,
    team_id: int,
    fixture: dict[str, Any],
    stats: dict[str, Any],
    element_type: int | None,
    value: int | None,
    source: str,
) -> dict[str, Any]:
    was_home = fixture["team_h"] == team_id
    return {
        "element_id": element_id,
        "fixture_id": int(fixture["id"]),
        "team_id": team_id,
        "opponent_team_id": fixture["team_a"] if was_home else fixture["team_h"],
        "was_home": was_home,
        "kickoff_time": fixture.get("kickoff_time"),
        "element_type": element_type,
        "value": value,
        "source": source,
        **stat_columns(stats),
    }


ROW_COLUMNS = (
    "element_id",
    "fixture_id",
    "team_id",
    "opponent_team_id",
    "was_home",
    "kickoff_time",
    "element_type",
    "value",
    "source",
    *STAT_COLUMNS,
)


def load_rows(
    con: duckdb.DuckDBPyConnection,
    scratch: Path,
    rows: list[dict[str, Any]],
    view: str = "src_appearance",
) -> None:
    """Register the flattened rows as a view.

    An empty gameweek is possible — a partial ingest where every fixture that has
    been played belongs to a club with nothing else on. `read_json` infers no
    columns from `[]`, so the empty case gets an explicitly-shaped view instead of
    a binder error three statements later.
    """
    if not rows:
        columns = ", ".join(f"NULL AS {name}" for name in ROW_COLUMNS)
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT {columns} WHERE false")
        return

    path = scratch / f"{view}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")
    con.execute(
        f"CREATE OR REPLACE VIEW {view} AS "
        f"SELECT * FROM read_json('{path.as_posix()}', format='array')"
    )


def build_gameweek_facts(
    con: duckdb.DuckDBPyConnection,
    season: str,
    gameweek: int,
    *,
    is_partial: bool,
    table: str = "gameweek_facts",
) -> None:
    """Project the flattened rows onto the contract, joining team master ids."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT
            '{season}' AS season,
            CAST(a.element_id AS INTEGER) AS element_id,
            m.player_master_id,
            CAST(a.fixture_id AS INTEGER) AS fixture_id,
            CAST({gameweek} AS TINYINT) AS gameweek,
            CAST(a.team_id AS INTEGER) AS team_id,
            t.team_master_id,
            CAST(a.opponent_team_id AS INTEGER) AS opponent_team_id,
            o.team_master_id AS opponent_team_master_id,
            CAST(a.was_home AS BOOLEAN) AS was_home,
            CAST(a.kickoff_time AS TIMESTAMP) AS kickoff_time,
            CAST(a.element_type AS TINYINT) AS element_type,
            CAST(a.minutes AS SMALLINT) AS minutes,
            CAST(a.starts AS TINYINT) AS starts,
            CAST(a.goals_scored AS TINYINT) AS goals_scored,
            CAST(a.assists AS TINYINT) AS assists,
            CAST(a.expected_goals AS DOUBLE) AS expected_goals,
            CAST(a.expected_assists AS DOUBLE) AS expected_assists,
            CAST(a.expected_goal_involvements AS DOUBLE) AS expected_goal_involvements,
            CAST(a.clean_sheets AS TINYINT) AS clean_sheets,
            CAST(a.goals_conceded AS TINYINT) AS goals_conceded,
            CAST(a.expected_goals_conceded AS DOUBLE) AS expected_goals_conceded,
            CAST(a.saves AS TINYINT) AS saves,
            CAST(a.penalties_saved AS TINYINT) AS penalties_saved,
            CAST(a.defensive_contribution AS SMALLINT) AS defensive_contribution,
            CAST(a.yellow_cards AS TINYINT) AS yellow_cards,
            CAST(a.red_cards AS TINYINT) AS red_cards,
            CAST(a.own_goals AS TINYINT) AS own_goals,
            CAST(a.penalties_missed AS TINYINT) AS penalties_missed,
            CAST(a.bps AS SMALLINT) AS bps,
            CAST(a.bonus AS TINYINT) AS bonus,
            CAST(a.total_points AS SMALLINT) AS total_points,
            CAST(a.influence AS DOUBLE) AS influence,
            CAST(a.creativity AS DOUBLE) AS creativity,
            CAST(a.threat AS DOUBLE) AS threat,
            CAST(a.ict_index AS DOUBLE) AS ict_index,
            CAST(a.value AS SMALLINT) AS value,
            a.source,
            {str(is_partial).lower()} AS is_partial
        FROM src_appearance a
        JOIN player_map m ON CAST(a.element_id AS INTEGER) = m.element_id
        JOIN dim_team t ON CAST(a.team_id AS INTEGER) = t.team_id
        JOIN dim_team o ON CAST(a.opponent_team_id AS INTEGER) = o.team_id
        """
    )


def merge_fact_player_fixture(
    con: duckdb.DuckDBPyConnection,
    *,
    existing: str | None,
    incoming: str = "gameweek_facts",
    table: str = "fact_player_fixture",
) -> None:
    """Whole-file rewrite: the season so far, with this gameweek's rows replacing
    any earlier version of the same keys.

    Re-ingesting is how a gameweek first stored as `is_partial` gets corrected once
    its postponed fixture is played, so the incoming rows must win.
    """
    if existing is None:
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM {incoming}")
        return

    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT * FROM {existing} e
        WHERE NOT EXISTS (
            SELECT 1 FROM {incoming} i
            WHERE i.season = e.season
              AND i.element_id = e.element_id
              AND i.fixture_id = e.fixture_id
        )
        UNION ALL
        SELECT * FROM {incoming}
        """
    )
