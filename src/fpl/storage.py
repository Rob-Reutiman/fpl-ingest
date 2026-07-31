"""DuckDB storage layer for FPL Edge."""

from __future__ import annotations

import logging
from typing import Any

import duckdb
import polars as pl

from fpl.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA: tuple[str, ...] = (
    # Static reference data, refreshed from bootstrap-static.
    """
    CREATE TABLE IF NOT EXISTS dim_player (
        code        INTEGER PRIMARY KEY,  -- Permanent cross-season ID
        fpl_id      INTEGER NOT NULL,     -- This-season ID
        web_name    VARCHAR NOT NULL,
        full_name   VARCHAR,
        team        INTEGER NOT NULL,
        team_name   VARCHAR NOT NULL,
        position    INTEGER NOT NULL,     -- 1=GK, 2=DEF, 3=MID, 4=FWD
        now_cost    INTEGER NOT NULL,
        news        VARCHAR,              -- Injury/suspension text
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_team (
        id          INTEGER PRIMARY KEY,
        name        VARCHAR NOT NULL,
        short_name  VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_fixture (
        id              INTEGER PRIMARY KEY,
        gameweek        INTEGER,
        team_h          INTEGER NOT NULL,
        team_a          INTEGER NOT NULL,
        team_h_score    INTEGER,
        team_a_score    INTEGER,
        kickoff_time    TIMESTAMP,
        finished        BOOLEAN DEFAULT FALSE
    )
    """,
    # Cohort membership per gameweek.
    """
    CREATE TABLE IF NOT EXISTS cohort_manager (
        gameweek     INTEGER NOT NULL,
        manager_id   INTEGER NOT NULL,
        rank         INTEGER NOT NULL,
        total_points INTEGER NOT NULL,
        is_top_slice BOOLEAN NOT NULL,   -- TRUE = deterministic top 5k, FALSE = random sample
        PRIMARY KEY (gameweek, manager_id)
    )
    """,
    # Picks: one row per player per manager per gameweek.
    """
    CREATE TABLE IF NOT EXISTS cohort_pick (
        gameweek        INTEGER NOT NULL,
        manager_id      INTEGER NOT NULL,
        fpl_id          INTEGER NOT NULL,
        squad_position  INTEGER NOT NULL, -- 1-15
        multiplier      INTEGER NOT NULL, -- 0, 1, 2, or 3
        is_captain      BOOLEAN NOT NULL,
        is_vice_captain BOOLEAN NOT NULL,
        active_chip     VARCHAR,          -- NULL, 'freehit', 'wildcard', 'bboost', '3xc'
        PRIMARY KEY (gameweek, manager_id, fpl_id)
    )
    """,
    # Transfers: one row per transfer event. The natural-key PK makes
    # INSERT OR REPLACE idempotent across harvest re-runs.
    """
    CREATE TABLE IF NOT EXISTS cohort_transfer (
        manager_id      INTEGER NOT NULL,
        gameweek        INTEGER NOT NULL,
        fpl_id_in       INTEGER NOT NULL,
        fpl_id_out      INTEGER NOT NULL,
        cost_in         INTEGER NOT NULL,
        cost_out        INTEGER NOT NULL,
        transfer_time   TIMESTAMP NOT NULL,
        PRIMARY KEY (manager_id, gameweek, fpl_id_in, fpl_id_out, transfer_time)
    )
    """,
    # Global player stats per gameweek from bootstrap (snapshot at time of run).
    """
    CREATE TABLE IF NOT EXISTS fact_player_gw (
        fpl_id              INTEGER NOT NULL,
        gameweek            INTEGER NOT NULL,
        total_points        INTEGER,
        minutes             INTEGER,
        goals_scored        INTEGER,
        assists             INTEGER,
        clean_sheets        INTEGER,
        bps                 INTEGER,
        selected_by_percent FLOAT,
        transfers_in_event  INTEGER,
        transfers_out_event INTEGER,
        ep_next             FLOAT,
        PRIMARY KEY (fpl_id, gameweek)
    )
    """,
    # My own team snapshot (for gap report).
    """
    CREATE TABLE IF NOT EXISTS my_pick (
        gameweek    INTEGER NOT NULL,
        fpl_id      INTEGER NOT NULL,
        multiplier  INTEGER NOT NULL,
        is_captain  BOOLEAN NOT NULL,
        PRIMARY KEY (gameweek, fpl_id)
    )
    """,
)


class Storage:
    """DuckDB-backed persistence for FPL Edge.

    Opens a connection on construction and initialises the schema. Usable as a
    context manager (``with Storage(path) as s: ...``) or managed manually via
    ``close()``.
    """

    def __init__(self, db_path: str = settings.db_path) -> None:
        self._conn = duckdb.connect(db_path)
        self.init_schema()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        self._conn.close()

    def init_schema(self) -> None:
        """Create all tables if they don't exist. Idempotent."""
        for ddl in _SCHEMA:
            self._conn.execute(ddl)

    # -- Write ----------------------------------------------------------------

    def _insert_or_replace(self, table: str, rows: list[dict]) -> None:
        """Upsert schema-keyed dicts into ``table``, matching keys to columns.

        Uses ``INSERT OR REPLACE ... BY NAME`` so re-running an ingestion never
        duplicates rows (primary keys enforce identity) and omitted columns fall
        back to their DEFAULTs.
        """
        if not rows:
            return
        df = pl.DataFrame(rows)  # noqa: F841 — referenced by DuckDB replacement scan
        self._conn.execute(f"INSERT OR REPLACE INTO {table} BY NAME SELECT * FROM df")

    def upsert_players(self, players: list[dict]) -> None:
        self._insert_or_replace("dim_player", players)

    def upsert_teams(self, teams: list[dict]) -> None:
        self._insert_or_replace("dim_team", teams)

    def upsert_fixtures(self, fixtures: list[dict]) -> None:
        self._insert_or_replace("dim_fixture", fixtures)

    def upsert_cohort_managers(self, gw: int, managers: list[dict]) -> None:
        self._insert_or_replace("cohort_manager", [{**m, "gameweek": gw} for m in managers])

    def insert_picks(self, picks: list[dict]) -> None:
        self._insert_or_replace("cohort_pick", picks)

    def insert_transfers(self, transfers: list[dict]) -> None:
        self._insert_or_replace("cohort_transfer", transfers)

    def upsert_player_gw_stats(self, gw: int, stats: list[dict]) -> None:
        self._insert_or_replace("fact_player_gw", [{**s, "gameweek": gw} for s in stats])

    def upsert_my_picks(self, gw: int, picks: list[dict]) -> None:
        self._insert_or_replace("my_pick", [{**p, "gameweek": gw} for p in picks])

    # -- Read -----------------------------------------------------------------

    @staticmethod
    def _rows_to_dicts(cur: duckdb.DuckDBPyConnection) -> list[dict]:
        """Materialise a just-executed cursor as a list of column-keyed dicts."""
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def get_player_by_fpl_id(self, fpl_id: int) -> dict | None:
        cur = self._conn.execute("SELECT * FROM dim_player WHERE fpl_id = ?", [fpl_id])
        rows = self._rows_to_dicts(cur)
        return rows[0] if rows else None

    def get_player_map(self) -> dict[int, dict]:
        """Return ``{fpl_id: player_dict}`` for all players."""
        cur = self._conn.execute("SELECT * FROM dim_player")
        return {row["fpl_id"]: row for row in self._rows_to_dicts(cur)}

    def get_cohort_manager_ids(self, gw: int) -> list[int]:
        cur = self._conn.execute("SELECT manager_id FROM cohort_manager WHERE gameweek = ?", [gw])
        return [row[0] for row in cur.fetchall()]

    def get_cohort_picks(self, gw: int) -> pl.DataFrame:
        return self._conn.execute("SELECT * FROM cohort_pick WHERE gameweek = ?", [gw]).pl()

    def get_cohort_transfers(self, gw: int) -> pl.DataFrame:
        return self._conn.execute("SELECT * FROM cohort_transfer WHERE gameweek = ?", [gw]).pl()

    def get_my_picks(self, gw: int) -> list[int]:
        """Return list of fpl_ids in my team for this GW."""
        cur = self._conn.execute("SELECT fpl_id FROM my_pick WHERE gameweek = ?", [gw])
        return [row[0] for row in cur.fetchall()]

    def get_latest_gameweek(self) -> int:
        """Return the most recent GW with cohort data, or 0 if there is none."""
        result = self._conn.execute("SELECT max(gameweek) FROM cohort_manager").fetchone()
        return result[0] if result and result[0] is not None else 0

    def has_cohort_picks(self, gw: int) -> bool:
        """True if cohort_pick has rows for this GW."""
        result = self._conn.execute(
            "SELECT count(*) FROM cohort_pick WHERE gameweek = ?", [gw]
        ).fetchone()
        return bool(result and result[0] > 0)

    def has_player_gw_stats(self, gw: int) -> bool:
        """True if fact_player_gw has rows for this GW."""
        result = self._conn.execute(
            "SELECT count(*) FROM fact_player_gw WHERE gameweek = ?", [gw]
        ).fetchone()
        return bool(result and result[0] > 0)
