"""The curated Parquet column contract, transcribed from `fpl-parquet-schemas.md`.

Every writer projects through `select_list()` and every reader can assert against
`COLUMNS`, so a column can't be invented, dropped or reordered in one table without
this file changing. That matters because the whole point of the spec is that a
multi-season `read_parquet` glob unions cleanly: a season missing a column, or
carrying it as a different type, breaks the glob for every season.

Types are DuckDB type names, cast explicitly. Sources are read as VARCHAR and cast
here rather than trusting CSV type sniffing, which drifts between seasons.
"""

from __future__ import annotations

from collections.abc import Mapping

# Table name -> ordered (column, duckdb_type). Order is the Parquet column order.
COLUMNS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "dim_player_master": (
        ("player_master_id", "INTEGER"),
        ("player_code", "INTEGER"),
        ("canonical_first_name", "VARCHAR"),
        ("canonical_second_name", "VARCHAR"),
        ("canonical_web_name", "VARCHAR"),
        ("normalized_name_key", "VARCHAR"),
        ("first_seen_season", "VARCHAR"),
        ("last_seen_season", "VARCHAR"),
    ),
    "map_player_season": (
        ("player_master_id", "INTEGER"),
        ("season", "VARCHAR"),
        ("element_id", "INTEGER"),
    ),
    "dim_team_master": (
        ("team_master_id", "VARCHAR"),
        ("canonical_name", "VARCHAR"),
        ("first_seen_season", "VARCHAR"),
        ("last_seen_season", "VARCHAR"),
    ),
    "map_team_season": (
        ("team_master_id", "VARCHAR"),
        ("season", "VARCHAR"),
        ("team_id", "INTEGER"),
    ),
    "dim_player": (
        ("season", "VARCHAR"),
        ("element_id", "INTEGER"),
        ("player_master_id", "INTEGER"),
        ("first_name", "VARCHAR"),
        ("second_name", "VARCHAR"),
        ("web_name", "VARCHAR"),
        ("element_type", "TINYINT"),
        ("position", "VARCHAR"),
        ("team_id", "INTEGER"),
        ("team_master_id", "VARCHAR"),
    ),
    "dim_team": (
        ("season", "VARCHAR"),
        ("team_id", "INTEGER"),
        ("team_master_id", "VARCHAR"),
        ("name", "VARCHAR"),
        ("short_name", "VARCHAR"),
        ("strength", "TINYINT"),
        ("strength_overall_home", "SMALLINT"),
        ("strength_overall_away", "SMALLINT"),
        ("strength_attack_home", "SMALLINT"),
        ("strength_attack_away", "SMALLINT"),
        ("strength_defence_home", "SMALLINT"),
        ("strength_defence_away", "SMALLINT"),
    ),
    "dim_fixture": (
        ("season", "VARCHAR"),
        ("fixture_id", "INTEGER"),
        ("gameweek", "TINYINT"),
        ("kickoff_time", "TIMESTAMP"),
        ("home_team_id", "INTEGER"),
        ("away_team_id", "INTEGER"),
        ("home_team_master_id", "VARCHAR"),
        ("away_team_master_id", "VARCHAR"),
        ("home_score", "TINYINT"),
        ("away_score", "TINYINT"),
        ("finished", "BOOLEAN"),
        ("finished_provisional", "BOOLEAN"),
        ("home_fdr", "TINYINT"),
        ("away_fdr", "TINYINT"),
    ),
    "dim_gameweek": (
        ("season", "VARCHAR"),
        ("gameweek", "TINYINT"),
        ("name", "VARCHAR"),
        ("deadline_time", "TIMESTAMP"),
        ("deadline_time_epoch", "BIGINT"),
        ("finished", "BOOLEAN"),
        ("data_checked", "BOOLEAN"),
        ("average_entry_score", "SMALLINT"),
        ("highest_score", "INTEGER"),
        ("most_selected", "INTEGER"),
        ("most_transferred_in", "INTEGER"),
        ("most_captained", "INTEGER"),
        ("fixture_count", "TINYINT"),
    ),
    "fact_player_fixture": (
        ("season", "VARCHAR"),
        ("element_id", "INTEGER"),
        ("player_master_id", "INTEGER"),
        ("fixture_id", "INTEGER"),
        ("gameweek", "TINYINT"),
        ("team_id", "INTEGER"),
        ("team_master_id", "VARCHAR"),
        ("opponent_team_id", "INTEGER"),
        ("opponent_team_master_id", "VARCHAR"),
        ("was_home", "BOOLEAN"),
        ("kickoff_time", "TIMESTAMP"),
        ("element_type", "TINYINT"),
        ("minutes", "SMALLINT"),
        ("starts", "TINYINT"),
        ("goals_scored", "TINYINT"),
        ("assists", "TINYINT"),
        ("expected_goals", "DOUBLE"),
        ("expected_assists", "DOUBLE"),
        ("expected_goal_involvements", "DOUBLE"),
        ("clean_sheets", "TINYINT"),
        ("goals_conceded", "TINYINT"),
        ("expected_goals_conceded", "DOUBLE"),
        ("saves", "TINYINT"),
        ("penalties_saved", "TINYINT"),
        ("defensive_contribution", "SMALLINT"),
        ("yellow_cards", "TINYINT"),
        ("red_cards", "TINYINT"),
        ("own_goals", "TINYINT"),
        ("penalties_missed", "TINYINT"),
        ("bps", "SMALLINT"),
        ("bonus", "TINYINT"),
        ("total_points", "SMALLINT"),
        ("influence", "DOUBLE"),
        ("creativity", "DOUBLE"),
        ("threat", "DOUBLE"),
        ("ict_index", "DOUBLE"),
        ("value", "SMALLINT"),
        ("source", "VARCHAR"),
        ("is_partial", "BOOLEAN"),
    ),
    "fact_team_fixture": (
        ("season", "VARCHAR"),
        ("team_id", "INTEGER"),
        ("team_master_id", "VARCHAR"),
        ("fixture_id", "INTEGER"),
        ("gameweek", "TINYINT"),
        ("opponent_team_id", "INTEGER"),
        ("opponent_team_master_id", "VARCHAR"),
        ("was_home", "BOOLEAN"),
        ("kickoff_time", "TIMESTAMP"),
        ("goals_for", "TINYINT"),
        ("goals_against", "TINYINT"),
        ("xg_for", "DOUBLE"),
        ("xg_against", "DOUBLE"),
        ("xgc_reported", "DOUBLE"),
        ("xa_for", "DOUBLE"),
        ("clean_sheet", "BOOLEAN"),
        ("result", "VARCHAR"),
    ),
    "fact_player_gameweek_fpl": (
        ("season", "VARCHAR"),
        ("element_id", "INTEGER"),
        ("player_master_id", "INTEGER"),
        ("gameweek", "TINYINT"),
        ("value", "SMALLINT"),
        ("selected", "INTEGER"),
        ("transfers_in", "INTEGER"),
        ("transfers_out", "INTEGER"),
        ("transfers_balance", "INTEGER"),
    ),
    "fpl_current": (
        ("season", "VARCHAR"),
        ("element_id", "INTEGER"),
        ("player_master_id", "INTEGER"),
        ("web_name", "VARCHAR"),
        ("team_id", "INTEGER"),
        ("team_master_id", "VARCHAR"),
        ("element_type", "TINYINT"),
        ("now_cost", "SMALLINT"),
        ("cost_change_event", "SMALLINT"),
        ("cost_change_start", "SMALLINT"),
        ("selected_by_percent", "DOUBLE"),
        ("transfers_in_event", "INTEGER"),
        ("transfers_out_event", "INTEGER"),
        ("status", "VARCHAR"),
        ("news", "VARCHAR"),
        ("news_added", "TIMESTAMP"),
        ("chance_of_playing_this_round", "TINYINT"),
        ("chance_of_playing_next_round", "TINYINT"),
        ("form", "DOUBLE"),
        ("points_per_game", "DOUBLE"),
        ("ep_this", "DOUBLE"),
        ("ep_next", "DOUBLE"),
        ("total_points", "SMALLINT"),
        ("minutes", "SMALLINT"),
        ("fetched_at", "TIMESTAMP"),
    ),
    "fact_manager_pick": (
        ("season", "VARCHAR"),
        ("gameweek", "TINYINT"),
        ("entry_id", "INTEGER"),
        ("sample_group", "VARCHAR"),
        ("overall_rank", "INTEGER"),
        ("element_id", "INTEGER"),
        ("player_master_id", "INTEGER"),
        ("pick_position", "TINYINT"),
        ("multiplier", "TINYINT"),
        ("is_captain", "BOOLEAN"),
        ("is_vice_captain", "BOOLEAN"),
    ),
    "agg_player_ownership": (
        ("season", "VARCHAR"),
        ("gameweek", "TINYINT"),
        ("element_id", "INTEGER"),
        ("player_master_id", "INTEGER"),
        ("sample_group", "VARCHAR"),
        ("sample_size", "INTEGER"),
        ("owned_count", "INTEGER"),
        ("ownership_pct", "DOUBLE"),
        ("starting_count", "INTEGER"),
        ("starting_pct", "DOUBLE"),
        ("captain_count", "INTEGER"),
        ("captain_pct", "DOUBLE"),
    ),
}

# Tables the backfill writes per season, and the sort key that makes each file
# deterministic so a re-run produces an identical file.
SEASON_TABLES: Mapping[str, tuple[str, ...]] = {
    "dim_team": ("team_id",),
    "dim_player": ("element_id",),
    "dim_fixture": ("fixture_id",),
    "dim_gameweek": ("gameweek",),
    "fact_player_fixture": ("element_id", "fixture_id"),
    "fact_team_fixture": ("fixture_id", "team_id"),
    "fact_player_gameweek_fpl": ("element_id", "gameweek"),
}

MASTER_TABLES: Mapping[str, tuple[str, ...]] = {
    "dim_player_master": ("player_master_id",),
    "map_player_season": ("season", "element_id"),
    "dim_team_master": ("team_master_id",),
    "map_team_season": ("season", "team_id"),
}

# Only the live pipeline writes these — the archive has no equivalent source.
CURRENT_SEASON_TABLES: Mapping[str, tuple[str, ...]] = {
    "fpl_current": ("element_id",),
    "fact_manager_pick": ("gameweek", "entry_id", "pick_position"),
    "agg_player_ownership": ("gameweek", "sample_group", "element_id"),
}

_SORT_KEYS: Mapping[str, tuple[str, ...]] = {
    **SEASON_TABLES,
    **MASTER_TABLES,
    **CURRENT_SEASON_TABLES,
}


def sort_key(table: str) -> tuple[str, ...]:
    """The ordering that makes this table's file byte-reproducible."""
    return _SORT_KEYS[table]


# `element_type` -> the denormalized `position` label the spec stores alongside it.
POSITION_LABELS: Mapping[int, str] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# The archive's per-row position string in `merged_gw.csv` -> `element_type`.
# 'AM' (2024-25's assistant managers, element_type 5) is deliberately absent:
# they are not players, the spec only defines 1-4, and FPL retired the asset.
ARCHIVE_POSITIONS: Mapping[str, int] = {"GK": 1, "GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}

# Position strings that exist in the source but are excluded from every table.
EXCLUDED_POSITIONS = frozenset({"AM", "MNG"})


def column_names(table: str) -> tuple[str, ...]:
    return tuple(name for name, _ in COLUMNS[table])


def create_table_ddl(table: str, *, name: str | None = None) -> str:
    """DDL for an empty table with exactly this contract's columns and types."""
    body = ", ".join(f"{column} {duck_type}" for column, duck_type in COLUMNS[table])
    return f"CREATE OR REPLACE TABLE {name or table} ({body})"


def select_list(table: str, *, source: str = "") -> str:
    """A projection that pins both the column set and the column order.

    `source` optionally qualifies each column with a relation alias.
    """
    prefix = f"{source}." if source else ""
    return ",\n    ".join(
        f"CAST({prefix}{name} AS {duck_type}) AS {name}" for name, duck_type in COLUMNS[table]
    )
