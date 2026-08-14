"""Job 1's transforms: the live snapshot and the season's dimensions.

Source is one `bootstrap-static` and one `fixtures` response. Everything here is
overwritten on every run — these tables describe the present, not history.

The JSON is loaded through DuckDB's `read_json` and every column is cast
explicitly against the contract. FPL sends plenty of numbers as strings
(`selected_by_percent`, `form`, `ep_this`) and nulls freely in the optional
fields, so inference would give a different physical type on a quiet day than on
a busy one and break the multi-season glob.
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
    """Register the two API responses as `src_element`, `src_team`, ... views.

    Written out and read back with `read_json` rather than handed over row by row:
    it keeps the transforms as SQL over the API's own shape, so a new field is one
    line of SQL rather than a plumbing change.
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
    """Every fixture, played or not — this is the schedule as well as the results.

    `gameweek` is nullable on purpose: a postponed fixture has a null `event` until
    it is rescheduled.
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
    """The live pipeline fills every column — `events[]` carries the lot.

    Backfilled seasons leave the deadline and scoring columns NULL because the
    archive has no events file; the column set is identical either way.
    """
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
    """The live state snapshot: price, ownership, injuries, form.

    `fetched_at` is stamped from the run, not from anything in the payload — the
    API doesn't say when its numbers were computed, and a consumer reading a stale
    hourly file needs to know how stale it is.
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
    """Teams first: the player and fixture tables join to it for master ids."""
    build_dim_team(con, season)
    build_dim_player(con, season)
    build_dim_fixture(con, season)
    build_dim_gameweek(con, season)
    build_fpl_current(con, season, fetched_at)
