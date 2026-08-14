"""Extending the cross-season master tables from a live bootstrap.

The backfill assigns `player_master_id`s for historical seasons; the live jobs
extend the same tables as new players arrive. Both go through `fpl.identity`, so
the matching tiers are defined once — a second implementation here would drift and
start splitting careers the backfill had already joined.

Every job that writes a `player_master_id` calls `resolve`, because a player can
appear mid-week (a transfer, a promoted club's late squad registration) and a job
that assumed Job 1 had already seen them would fail on a missing id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from fpl import curated_schema, keys
from fpl.identity import MasterPlayer, MasterRegistry, ReviewRow, SeasonPlayer
from fpl.r2_client import CSV_CONTENT_TYPE, PARQUET_CONTENT_TYPE, ObjectStore
from fpl.transforms import parquet

logger = logging.getLogger(__name__)

REVIEW_FILENAME = "player_match_review.csv"

# bootstrap-static carries assistant managers as element_type 5 in the seasons that
# had them. The schema defines 1-4, so they never reach a curated table.
PLAYER_ELEMENT_TYPES = (1, 2, 3, 4)


@dataclass
class Resolution:
    """The outcome of resolving one season's squads against the master tables."""

    players: dict[int, int]
    """element_id -> player_master_id"""
    teams: dict[int, str]
    """team_id -> team_master_id"""
    new_player_ids: int
    new_review_rows: int


def team_master_ids(teams: list[dict[str, Any]]) -> dict[int, str]:
    """`short_name` is stable across seasons, so it *is* the team master id."""
    return {int(team["id"]): team["short_name"] for team in teams}


def season_players(
    elements: list[dict[str, Any]], teams: dict[int, str], season: str
) -> list[SeasonPlayer]:
    return [
        SeasonPlayer(
            season=season,
            element_id=int(element["id"]),
            player_code=_int_or_none(element.get("code")),
            first_name=element.get("first_name") or "",
            second_name=element.get("second_name") or "",
            web_name=element.get("web_name") or "",
            team_master_id=teams[int(element["team"])],
        )
        for element in elements
        if int(element["element_type"]) in PLAYER_ELEMENT_TYPES
    ]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MasterTables:
    """Loads the master tables from R2, extends them, and writes back if changed."""

    def __init__(self, store: ObjectStore, scratch: Path) -> None:
        self._store = store
        self._scratch = scratch
        self._con = parquet.connect()
        self._registry = MasterRegistry(self._load_players())
        self._existing_review = parquet.parse_review_csv(
            store.get_bytes(keys.master_key(REVIEW_FILENAME))
        )
        self._map_rows = self._load_table("map_player_season")
        self._team_rows = self._load_table("map_team_season")
        self._team_master_rows = self._load_table("dim_team_master")
        self._dirty = False

    # -- Loading --------------------------------------------------------------

    def _load_table(self, table: str) -> list[tuple[Any, ...]]:
        body = self._store.get_bytes(keys.master_key(f"{table}.parquet"))
        if not parquet.register_parquet(self._con, f"existing_{table}", body, self._scratch):
            return []
        columns = ", ".join(curated_schema.column_names(table))
        return self._con.execute(f"SELECT {columns} FROM existing_{table}").fetchall()

    def _load_players(self) -> list[MasterPlayer]:
        rows = self._load_table("dim_player_master")
        logger.info("loaded %d existing master players", len(rows))
        return [MasterPlayer(*row) for row in rows]

    # -- Resolving ------------------------------------------------------------

    def resolve(self, season: str, bootstrap: dict[str, Any]) -> Resolution:
        teams = team_master_ids(bootstrap["teams"])
        players = season_players(bootstrap["elements"], teams, season)

        before_ids = len(self._registry.masters)
        before_review = len(self._registry.review)
        assigned = self._registry.resolve_season(players)

        new_ids = len(self._registry.masters) - before_ids
        new_review = len(self._registry.review) - before_review
        if new_ids:
            logger.info("%s: %d new player_master_id(s) assigned", season, new_ids)
        if new_review:
            logger.warning(
                "%s: %d identity match(es) need review — see curated/master/%s",
                season,
                new_review,
                REVIEW_FILENAME,
            )

        self._merge_season_maps(season, assigned, teams, bootstrap["teams"])
        return Resolution(
            players=assigned,
            teams=teams,
            new_player_ids=new_ids,
            new_review_rows=new_review,
        )

    def _merge_season_maps(
        self,
        season: str,
        assigned: dict[int, int],
        teams: dict[int, str],
        team_rows: list[dict[str, Any]],
    ) -> None:
        """Replace this season's rows in the season maps, keeping other seasons."""
        players = [(master_id, season, element_id) for element_id, master_id in assigned.items()]
        self._map_rows = self._replace_season(self._map_rows, players, season)

        mapped = [(short, season, team_id) for team_id, short in teams.items()]
        self._team_rows = self._replace_season(self._team_rows, mapped, season)

        names = {team["short_name"]: team["name"] for team in team_rows}
        merged = {row[0]: list(row) for row in self._team_master_rows}
        for short, name in sorted(names.items()):
            if short in merged:
                row = merged[short]
                row[1] = name
                row[2] = min(row[2], season)
                row[3] = max(row[3], season)
            else:
                merged[short] = [short, name, season, season]
        self._team_master_rows = self._commit(
            self._team_master_rows, [tuple(row) for row in merged.values()]
        )

    def _replace_season(
        self, existing: list[tuple[Any, ...]], rows: list[tuple[Any, ...]], season: str
    ) -> list[tuple[Any, ...]]:
        """Swap out one season's rows, leaving every other season's untouched."""
        return self._commit(existing, [row for row in existing if row[1] != season] + rows)

    def _commit(
        self, existing: list[tuple[Any, ...]], rebuilt: list[tuple[Any, ...]]
    ) -> list[tuple[Any, ...]]:
        """Keep the rebuilt rows, flagging a write only if the content really moved.

        Sorted before comparing: rows read back from Parquet arrive in the file's
        sort order, which isn't the order they were assembled in, and an hourly job
        that rewrote four files every run over a spurious diff would churn the
        bucket for nothing.
        """
        rebuilt = sorted(rebuilt)
        if rebuilt != sorted(existing):
            self._dirty = True
        return rebuilt

    # -- Writing --------------------------------------------------------------

    @property
    def changed(self) -> bool:
        """Whether anything actually moved. Hourly no-op runs shouldn't rewrite."""
        return self._dirty or bool(self._registry.review)

    @property
    def review_rows(self) -> list[ReviewRow]:
        """Existing findings plus this run's — the file accumulates, never resets."""
        return self._existing_review + self._registry.review

    def write(self, store: ObjectStore) -> list[str]:
        """Write the four master tables and the review file. Returns keys written."""
        self._stage()
        written: list[str] = []
        for table in curated_schema.MASTER_TABLES:
            key = keys.master_key(f"{table}.parquet")
            store.put_bytes(
                key,
                parquet.to_parquet_bytes(self._con, table, self._scratch),
                content_type=PARQUET_CONTENT_TYPE,
            )
            written.append(key)

        key = keys.master_key(REVIEW_FILENAME)
        store.put_bytes(
            key,
            parquet.review_csv_bytes(self.review_rows, self._scratch),
            content_type=CSV_CONTENT_TYPE,
        )
        written.append(key)
        return written

    def _stage(self) -> None:
        self._con.execute(curated_schema.create_table_ddl("dim_player_master"))
        self._con.executemany(
            "INSERT INTO dim_player_master VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(master.as_row().values()) for master in self._registry.masters],
        )
        for table, rows in (
            ("map_player_season", self._map_rows),
            ("dim_team_master", self._team_master_rows),
            ("map_team_season", self._team_rows),
        ):
            self._con.execute(curated_schema.create_table_ddl(table))
            placeholders = ", ".join("?" for _ in curated_schema.column_names(table))
            self._con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> MasterTables:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def register_player_map(
    con: duckdb.DuckDBPyConnection, season: str, assigned: dict[int, int]
) -> None:
    """Materialize `{element_id: player_master_id}` so a transform can join to it."""
    con.execute(
        "CREATE OR REPLACE TABLE player_map "
        "(season VARCHAR, element_id INTEGER, player_master_id INTEGER)"
    )
    con.executemany(
        "INSERT INTO player_map VALUES (?, ?, ?)",
        [(season, element_id, master_id) for element_id, master_id in sorted(assigned.items())],
    )
