"""The live snapshot and the season's dimension tables.

Built from one bootstrap and one fixtures response, and rewritten whole on every
run, since these tables describe the present.

Every column is cast explicitly. FPL sends many numbers as strings and nulls the
optional fields freely, so inference would settle on a different physical type
on a quiet day than on a busy one and break the glob.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from fpl import curated_schema

logger = logging.getLogger(__name__)

_POSITION_LABEL_CASE = " ".join(
    f"WHEN {value} THEN '{label}'" for value, label in curated_schema.POSITION_LABELS.items()
)

_PLAYER_TYPES = ", ".join(str(t) for t in (1, 2, 3, 4))


def load_snapshot(
    con: duckdb.DuckDBPyConnection,
    scratch: Path,
    bootstrap: dict[str, Any],
    fixtures: list[dict[str, Any]],
) -> None:
    """Register the two API responses as the `src_element`, `src_team` views.

    Writing the payloads out and reading them back keeps the transforms as plain
    SQL over the shape the API returns, where picking up a new field costs one
    more line of SQL.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("element", bootstrap["elements"]),
        ("team", bootstrap["teams"]),
        ("event", bootstrap["events"]),
        ("fixture", fixtures),
    ):
        path = scratch / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        con.execute(
            f"CREATE OR REPLACE VIEW src_{name} AS "
            f"SELECT * FROM read_json('{path.as_posix()}', format='array')"
        )


def build_dim_team(con: duckdb.DuckDBPyConnection, season: str) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE dim_team AS
        SELECT
            '{season}' AS season,
            CAST(id AS INTEGER) AS team_id,
            short_name AS team_master_id,
            name,
            short_name,
            CAST(strength AS TINYINT) AS strength,
            CAST(strength_overall_home AS SMALLINT) AS strength_overall_home,
            CAST(strength_overall_away AS SMALLINT) AS strength_overall_away,
            CAST(strength_attack_home AS SMALLINT) AS strength_attack_home,
            CAST(strength_attack_away AS SMALLINT) AS strength_attack_away,
            CAST(strength_defence_home AS SMALLINT) AS strength_defence_home,
            CAST(strength_defence_away AS SMALLINT) AS strength_defence_away
        FROM src_team
        """
    )


def build_dim_player(con: duckdb.DuckDBPyConnection, season: str) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE dim_player AS
        SELECT
            '{season}' AS season,
            CAST(e.id AS INTEGER) AS element_id,
            m.player_master_id,
            e.first_name,
            e.second_name,
            e.web_name,
            CAST(e.element_type AS TINYINT) AS element_type,
            CASE CAST(e.element_type AS INTEGER) {_POSITION_LABEL_CASE} END AS position,
            CAST(e.team AS INTEGER) AS team_id,
            t.team_master_id
        FROM src_element e
        JOIN player_map m ON CAST(e.id AS INTEGER) = m.element_id
        JOIN dim_team t ON CAST(e.team AS INTEGER) = t.team_id
        WHERE CAST(e.element_type AS INTEGER) IN ({_PLAYER_TYPES})
        """
    )


def build_dim_fixture(con: duckdb.DuckDBPyConnection, season: str) -> None:
    """Every fixture, played or otherwise. The schedule and the results.

    `gameweek` is nullable, since a postponed fixture carries a null `event`
    until FPL reschedules it.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE dim_fixture AS
        SELECT
            '{season}' AS season,
            CAST(f.id AS INTEGER) AS fixture_id,
            CAST(f.event AS TINYINT) AS gameweek,
            CAST(f.kickoff_time AS TIMESTAMP) AS kickoff_time,
            CAST(f.team_h AS INTEGER) AS home_team_id,
            CAST(f.team_a AS INTEGER) AS away_team_id,
            h.team_master_id AS home_team_master_id,
            a.team_master_id AS away_team_master_id,
            CAST(f.team_h_score AS TINYINT) AS home_score,
            CAST(f.team_a_score AS TINYINT) AS away_score,
            CAST(f.finished AS BOOLEAN) AS finished,
            CAST(f.finished_provisional AS BOOLEAN) AS finished_provisional,
            CAST(f.team_h_difficulty AS TINYINT) AS home_fdr,
            CAST(f.team_a_difficulty AS TINYINT) AS away_fdr
        FROM src_fixture f
        JOIN dim_team h ON CAST(f.team_h AS INTEGER) = h.team_id
        JOIN dim_team a ON CAST(f.team_a AS INTEGER) = a.team_id
        """
    )


def build_dim_gameweek(con: duckdb.DuckDBPyConnection, season: str) -> None:
    """One row per gameweek, every column populated from `events[]`."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE dim_gameweek AS
        SELECT
            '{season}' AS season,
            CAST(e.id AS TINYINT) AS gameweek,
            e.name,
            CAST(e.deadline_time AS TIMESTAMP) AS deadline_time,
            CAST(e.deadline_time_epoch AS BIGINT) AS deadline_time_epoch,
            CAST(e.finished AS BOOLEAN) AS finished,
            CAST(e.data_checked AS BOOLEAN) AS data_checked,
            CAST(e.average_entry_score AS SMALLINT) AS average_entry_score,
            CAST(e.highest_score AS INTEGER) AS highest_score,
            CAST(e.most_selected AS INTEGER) AS most_selected,
            CAST(e.most_transferred_in AS INTEGER) AS most_transferred_in,
            CAST(e.most_captained AS INTEGER) AS most_captained,
            CAST(coalesce(f.fixture_count, 0) AS TINYINT) AS fixture_count
        FROM src_event e
        LEFT JOIN (
            SELECT gameweek, count(*) AS fixture_count
            FROM dim_fixture WHERE gameweek IS NOT NULL GROUP BY gameweek
        ) f ON CAST(e.id AS TINYINT) = f.gameweek
        """
    )


def build_fpl_current(con: duckdb.DuckDBPyConnection, season: str, fetched_at: datetime) -> None:
    """The live state snapshot. Price, ownership, injuries and form.

    `fetched_at` is stamped from the run, since the payload carries no timestamp
    of its own and a consumer needs one to judge how stale the file has grown.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE fpl_current AS
        SELECT
            '{season}' AS season,
            CAST(e.id AS INTEGER) AS element_id,
            m.player_master_id,
            e.web_name,
            CAST(e.team AS INTEGER) AS team_id,
            t.team_master_id,
            CAST(e.element_type AS TINYINT) AS element_type,
            CAST(e.now_cost AS SMALLINT) AS now_cost,
            CAST(e.cost_change_event AS SMALLINT) AS cost_change_event,
            CAST(e.cost_change_start AS SMALLINT) AS cost_change_start,
            CAST(e.selected_by_percent AS DOUBLE) AS selected_by_percent,
            CAST(e.transfers_in_event AS INTEGER) AS transfers_in_event,
            CAST(e.transfers_out_event AS INTEGER) AS transfers_out_event,
            e.status,
            e.news,
            CAST(e.news_added AS TIMESTAMP) AS news_added,
            CAST(e.chance_of_playing_this_round AS TINYINT) AS chance_of_playing_this_round,
            CAST(e.chance_of_playing_next_round AS TINYINT) AS chance_of_playing_next_round,
            CAST(e.form AS DOUBLE) AS form,
            CAST(e.points_per_game AS DOUBLE) AS points_per_game,
            CAST(e.ep_this AS DOUBLE) AS ep_this,
            CAST(e.ep_next AS DOUBLE) AS ep_next,
            CAST(e.total_points AS SMALLINT) AS total_points,
            CAST(e.minutes AS SMALLINT) AS minutes,
            CAST('{fetched_at.isoformat()}' AS TIMESTAMP) AS fetched_at
        FROM src_element e
        JOIN player_map m ON CAST(e.id AS INTEGER) = m.element_id
        JOIN dim_team t ON CAST(e.team AS INTEGER) = t.team_id
        WHERE CAST(e.element_type AS INTEGER) IN ({_PLAYER_TYPES})
        """
    )


TABLES = ("fpl_current", "dim_player", "dim_team", "dim_fixture", "dim_gameweek")


def build_all(con: duckdb.DuckDBPyConnection, season: str, fetched_at: datetime) -> None:
    """Build every table. Teams lead, as the others join to them for master ids."""
    build_dim_team(con, season)
    build_dim_player(con, season)
    build_dim_fixture(con, season)
    build_dim_gameweek(con, season)
    build_fpl_current(con, season, fetched_at)
