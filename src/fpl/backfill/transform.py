"""DuckDB transforms from archive CSV to the curated schema.

Everything is loaded as VARCHAR and cast explicitly against `curated_schema`. CSV
type sniffing looks convenient right up to the season where a column is empty
throughout and gets inferred as BOOLEAN, at which point a multi-season glob stops
unioning. Casting from text costs nothing at 30k rows and can't drift.

Source columns absent from a season are injected as typed NULLs — never as 0.
"NULL" means the stat wasn't measured that season; 0 means it was measured and was
zero, and conflating them poisons any cross-season model.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import duckdb

from fpl import curated_schema
from fpl.backfill.archive import SeasonSources
from fpl.identity import MasterPlayer, SeasonPlayer

logger = logging.getLogger(__name__)

SOURCE_TAG = "archive_backfill"

# Spec columns sourced from merged_gw.csv that some seasons predate. Probed per
# season and NULL-filled when absent (`defensive_contribution` arrived in 2025-26).
OPTIONAL_FACT_COLUMNS: dict[str, str] = {
    "starts": "TINYINT",
    "defensive_contribution": "SMALLINT",
}

# In merged_gw.csv, `element_type` is not present; the per-row `position` string is,
# and it reflects the player's position *at that fixture*, which is what the spec
# wants. Managers are dropped here and nowhere else.
_ELEMENT_TYPE_CASE = " ".join(
    f"WHEN '{label}' THEN {value}" for label, value in curated_schema.ARCHIVE_POSITIONS.items()
)
_POSITION_LABEL_CASE = " ".join(
    f"WHEN {value} THEN '{label}'" for value, label in curated_schema.POSITION_LABELS.items()
)


def _csv(path: Path) -> str:
    """A read_csv call that reads everything as text and keeps empties as NULL."""
    return (
        f"read_csv('{path.as_posix()}', all_varchar=true, header=true, "
        "sample_size=-1, nullstr=['', 'NA', 'None'])"
    )


def _header(con: duckdb.DuckDBPyConnection, path: Path) -> set[str]:
    return {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {_csv(path)}").fetchall()}


def load_season_sources(con: duckdb.DuckDBPyConnection, sources: SeasonSources) -> None:
    """Register one season's CSVs as views named `src_<file>`."""
    for filename, path in sources.paths.items():
        if not filename.endswith(".csv"):
            continue
        view = f"src_{filename.removesuffix('.csv')}"
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {_csv(path)}")
    logger.info("loaded %s source CSVs for %s", len(sources.paths) - 1, sources.season)


# -- Teams --------------------------------------------------------------------


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
        FROM src_teams
        """
    )


# -- Players ------------------------------------------------------------------


def read_season_players(con: duckdb.DuckDBPyConnection, season: str) -> list[SeasonPlayer]:
    """The identity module's input: one row per player, with their club's code."""
    rows = con.execute(
        """
        SELECT
            CAST(p.id AS INTEGER),
            TRY_CAST(p.code AS INTEGER),
            p.first_name,
            p.second_name,
            p.web_name,
            t.team_master_id
        FROM src_players_raw p
        JOIN dim_team t ON CAST(p.team AS INTEGER) = t.team_id
        WHERE CAST(p.element_type AS INTEGER) IN (1, 2, 3, 4)
        ORDER BY CAST(p.id AS INTEGER)
        """
    ).fetchall()
    return [
        SeasonPlayer(
            season=season,
            element_id=element_id,
            player_code=code,
            first_name=first_name or "",
            second_name=second_name or "",
            web_name=web_name or "",
            team_master_id=team_master_id,
        )
        for element_id, code, first_name, second_name, web_name, team_master_id in rows
    ]


def register_master_map(
    con: duckdb.DuckDBPyConnection, season: str, assigned: dict[int, int]
) -> None:
    """Materialize `{element_id: player_master_id}` so SQL can join to it."""
    con.execute(
        "CREATE OR REPLACE TABLE player_map (season VARCHAR, element_id INTEGER, "
        "player_master_id INTEGER)"
    )
    con.executemany(
        "INSERT INTO player_map VALUES (?, ?, ?)",
        [(season, element_id, master_id) for element_id, master_id in sorted(assigned.items())],
    )


def build_dim_player(con: duckdb.DuckDBPyConnection, season: str) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE dim_player AS
        SELECT
            '{season}' AS season,
            CAST(p.id AS INTEGER) AS element_id,
            m.player_master_id,
            p.first_name,
            p.second_name,
            p.web_name,
            CAST(p.element_type AS TINYINT) AS element_type,
            CASE CAST(p.element_type AS INTEGER) {_POSITION_LABEL_CASE} END AS position,
            CAST(p.team AS INTEGER) AS team_id,
            t.team_master_id
        FROM src_players_raw p
        JOIN player_map m ON CAST(p.id AS INTEGER) = m.element_id
        JOIN dim_team t ON CAST(p.team AS INTEGER) = t.team_id
        """
    )


# -- Fixtures and gameweeks ---------------------------------------------------


def build_dim_fixture(con: duckdb.DuckDBPyConnection, season: str) -> None:
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
        FROM src_fixtures f
        JOIN dim_team h ON CAST(f.team_h AS INTEGER) = h.team_id
        JOIN dim_team a ON CAST(f.team_a AS INTEGER) = a.team_id
        """
    )


def build_dim_gameweek(con: duckdb.DuckDBPyConnection, season: str) -> None:
    """Derived from the fixture list — the archive has no events file.

    `deadline_time`, the scoring aggregates and the `most_*` columns are
    unobtainable after the fact and are written NULL, as the spec documents for
    archive-backfilled seasons. The full column set is still present so the file
    unions with the live pipeline's fully-populated rows.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE dim_gameweek AS
        SELECT
            '{season}' AS season,
            gameweek,
            'Gameweek ' || CAST(gameweek AS VARCHAR) AS name,
            CAST(NULL AS TIMESTAMP) AS deadline_time,
            CAST(NULL AS BIGINT) AS deadline_time_epoch,
            bool_and(finished) AS finished,
            bool_and(finished) AS data_checked,
            CAST(NULL AS SMALLINT) AS average_entry_score,
            CAST(NULL AS INTEGER) AS highest_score,
            CAST(NULL AS INTEGER) AS most_selected,
            CAST(NULL AS INTEGER) AS most_transferred_in,
            CAST(NULL AS INTEGER) AS most_captained,
            CAST(count(*) AS TINYINT) AS fixture_count
        FROM dim_fixture
        WHERE gameweek IS NOT NULL
        GROUP BY gameweek
        """
    )


# -- Facts --------------------------------------------------------------------


def build_fact_source(con: duckdb.DuckDBPyConnection, sources: SeasonSources) -> list[str]:
    """`merged_gw` deduplicated and typed, the shared base for both fact tables.

    Returns the spec columns this season's source lacks, which are NULL-filled.

    2025-26's merged_gw carries 10 byte-identical duplicate rows (an archive
    artifact). DISTINCT absorbs them; validation then asserts the key is unique, so
    a *conflicting* duplicate — two different readings of one appearance — fails
    loudly instead of being silently collapsed to whichever row sorts first.
    """
    present = _header(con, sources.path("merged_gw.csv"))
    optional = ",\n            ".join(
        f"CAST({name} AS {duck_type}) AS {name}"
        if name in present
        else f"CAST(NULL AS {duck_type}) AS {name}"
        for name, duck_type in OPTIONAL_FACT_COLUMNS.items()
    )
    missing = sorted(set(OPTIONAL_FACT_COLUMNS) - present)
    if missing:
        logger.info("%s: NULL-filling absent source columns %s", sources.season, missing)

    excluded = ", ".join(f"'{p}'" for p in sorted(curated_schema.EXCLUDED_POSITIONS))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE fact_source AS
        SELECT
            CAST(element AS INTEGER) AS element_id,
            CAST(fixture AS INTEGER) AS fixture_id,
            CAST("GW" AS TINYINT) AS gameweek,
            CAST(round AS TINYINT) AS source_round,
            team AS team_name,
            CAST(opponent_team AS INTEGER) AS opponent_team_id,
            CAST(was_home AS BOOLEAN) AS was_home,
            CAST(kickoff_time AS TIMESTAMP) AS kickoff_time,
            CASE position {_ELEMENT_TYPE_CASE} END AS element_type,
            CAST(minutes AS SMALLINT) AS minutes,
            CAST(goals_scored AS TINYINT) AS goals_scored,
            CAST(assists AS TINYINT) AS assists,
            CAST(expected_goals AS DOUBLE) AS expected_goals,
            CAST(expected_assists AS DOUBLE) AS expected_assists,
            CAST(expected_goal_involvements AS DOUBLE) AS expected_goal_involvements,
            CAST(clean_sheets AS TINYINT) AS clean_sheets,
            CAST(goals_conceded AS TINYINT) AS goals_conceded,
            CAST(expected_goals_conceded AS DOUBLE) AS expected_goals_conceded,
            CAST(saves AS TINYINT) AS saves,
            CAST(penalties_saved AS TINYINT) AS penalties_saved,
            CAST(yellow_cards AS TINYINT) AS yellow_cards,
            CAST(red_cards AS TINYINT) AS red_cards,
            CAST(own_goals AS TINYINT) AS own_goals,
            CAST(penalties_missed AS TINYINT) AS penalties_missed,
            CAST(bps AS SMALLINT) AS bps,
            CAST(bonus AS TINYINT) AS bonus,
            CAST(total_points AS SMALLINT) AS total_points,
            CAST(influence AS DOUBLE) AS influence,
            CAST(creativity AS DOUBLE) AS creativity,
            CAST(threat AS DOUBLE) AS threat,
            CAST(ict_index AS DOUBLE) AS ict_index,
            CAST(value AS SMALLINT) AS value,
            CAST(selected AS INTEGER) AS selected,
            CAST(transfers_in AS INTEGER) AS transfers_in,
            CAST(transfers_out AS INTEGER) AS transfers_out,
            CAST(transfers_balance AS INTEGER) AS transfers_balance,
            {optional}
        FROM (SELECT DISTINCT * FROM src_merged_gw)
        WHERE position NOT IN ({excluded})
        """
    )
    return missing


def count_excluded_rows(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """(manager rows dropped, exact duplicate rows collapsed) — for the report."""
    excluded = ", ".join(f"'{p}'" for p in sorted(curated_schema.EXCLUDED_POSITIONS))
    managers = con.execute(
        f"SELECT count(*) FROM src_merged_gw WHERE position IN ({excluded})"
    ).fetchone()
    dupes = con.execute(
        "SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM src_merged_gw)) "
        "FROM src_merged_gw"
    ).fetchone()
    return (managers[0] if managers else 0, dupes[0] if dupes else 0)


def build_fact_player_fixture(con: duckdb.DuckDBPyConnection, season: str) -> None:
    """One row per `(season, element_id, fixture_id)`.

    Team attribution is pinned to the fixture via merged_gw's own `team` name, not
    joined from `dim_player` — a January transfer would otherwise retroactively
    reattribute the player's earlier fixtures to their new club.

    Blank gameweeks need no handling: a team with no fixture simply has no source
    rows, which is exactly the "no row at all" rule. Validation asserts it rather
    than trusting it.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE fact_player_fixture AS
        SELECT
            '{season}' AS season,
            s.element_id,
            m.player_master_id,
            s.fixture_id,
            s.gameweek,
            t.team_id,
            t.team_master_id,
            s.opponent_team_id,
            o.team_master_id AS opponent_team_master_id,
            s.was_home,
            s.kickoff_time,
            s.element_type,
            s.minutes,
            s.starts,
            s.goals_scored,
            s.assists,
            s.expected_goals,
            s.expected_assists,
            s.expected_goal_involvements,
            s.clean_sheets,
            s.goals_conceded,
            s.expected_goals_conceded,
            s.saves,
            s.penalties_saved,
            s.defensive_contribution,
            s.yellow_cards,
            s.red_cards,
            s.own_goals,
            s.penalties_missed,
            s.bps,
            s.bonus,
            s.total_points,
            s.influence,
            s.creativity,
            s.threat,
            s.ict_index,
            s.value,
            '{SOURCE_TAG}' AS source,
            false AS is_partial
        FROM fact_source s
        JOIN player_map m ON s.element_id = m.element_id
        JOIN dim_team t ON s.team_name = t.name
        JOIN dim_team o ON s.opponent_team_id = o.team_id
        """
    )


def build_fact_player_gameweek_fpl(con: duckdb.DuckDBPyConnection, season: str) -> None:
    """The FPL game layer at gameweek grain.

    Price and ownership are gameweek-level, so a double gameweek repeats them on
    both fixture rows. DISTINCT collapses that; validation asserts the repeated
    values actually agree rather than assuming it.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE fact_player_gameweek_fpl AS
        SELECT DISTINCT
            '{season}' AS season,
            s.element_id,
            m.player_master_id,
            s.gameweek,
            s.value,
            s.selected,
            s.transfers_in,
            s.transfers_out,
            s.transfers_balance
        FROM fact_source s
        JOIN player_map m ON s.element_id = m.element_id
        """
    )


# -- Master tables ------------------------------------------------------------


def build_team_master(con: duckdb.DuckDBPyConnection) -> None:
    """`short_name` is stable across seasons, so it *is* the master id.

    Reads every season's `dim_team` rows, already staged as `all_dim_team`.
    """
    con.execute(
        """
        CREATE OR REPLACE TABLE dim_team_master AS
        SELECT
            team_master_id,
            last(name ORDER BY season) AS canonical_name,
            min(season) AS first_seen_season,
            max(season) AS last_seen_season
        FROM all_dim_team
        GROUP BY team_master_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE map_team_season AS
        SELECT team_master_id, season, team_id FROM all_dim_team
        """
    )


def register_player_master(
    con: duckdb.DuckDBPyConnection,
    masters: Iterable[MasterPlayer],
    season_map: Iterable[tuple[int, str, int]],
) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE dim_player_master (
            player_master_id INTEGER, player_code INTEGER,
            canonical_first_name VARCHAR, canonical_second_name VARCHAR,
            canonical_web_name VARCHAR, normalized_name_key VARCHAR,
            first_seen_season VARCHAR, last_seen_season VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO dim_player_master VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [tuple(master.as_row().values()) for master in masters],
    )
    con.execute(
        "CREATE OR REPLACE TABLE map_player_season "
        "(player_master_id INTEGER, season VARCHAR, element_id INTEGER)"
    )
    con.executemany("INSERT INTO map_player_season VALUES (?, ?, ?)", list(season_map))


# -- Output -------------------------------------------------------------------
