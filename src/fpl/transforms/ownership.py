"""Job 3's transforms: manager picks and the ownership aggregate.

`agg_player_ownership` answers the question the whole warehouse is for — "who do
the best managers own that I don't" — so its denominator has to be right.
`sample_size` is the number of managers whose picks were **actually fetched** in
that group, not the number the sampler aimed for. Using the target would let a
handful of failed requests quietly deflate every percentage in the group.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

# A squad is 15: positions 1-11 start, 12-15 are the bench. Autosubs can give a
# benched player a multiplier later, so "starting" means the position, not the
# multiplier.
STARTING_POSITIONS = 11


def pick_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the harvested NDJSON into one row per (manager, pick)."""
    rows: list[dict[str, Any]] = []
    for record in records:
        entry_id = int(record["entry_id"])
        for pick in record.get("data", {}).get("picks", []):
            rows.append(
                {
                    "entry_id": entry_id,
                    "sample_group": record["group"],
                    "overall_rank": record.get("rank"),
                    "element_id": int(pick["element"]),
                    "pick_position": int(pick["position"]),
                    "multiplier": int(pick["multiplier"]),
                    "is_captain": bool(pick.get("is_captain")),
                    "is_vice_captain": bool(pick.get("is_vice_captain")),
                }
            )
    return rows


def sample_sizes(records: list[dict[str, Any]]) -> dict[str, int]:
    """Managers successfully fetched per group — the ownership denominator."""
    sizes: dict[str, set[int]] = {}
    for record in records:
        sizes.setdefault(record["group"], set()).add(int(record["entry_id"]))
    return {group: len(entries) for group, entries in sizes.items()}


def load_picks(
    con: duckdb.DuckDBPyConnection,
    scratch: Path,
    rows: list[dict[str, Any]],
    sizes: dict[str, int],
    view: str = "src_pick",
) -> None:
    path = scratch / f"{view}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")
    con.execute(
        f"CREATE OR REPLACE VIEW {view} AS "
        f"SELECT * FROM read_json('{path.as_posix()}', format='array')"
    )
    con.execute("CREATE OR REPLACE TABLE sample_size (sample_group VARCHAR, sample_size INTEGER)")
    con.executemany("INSERT INTO sample_size VALUES (?, ?)", sorted(sizes.items()))


def build_fact_manager_pick(
    con: duckdb.DuckDBPyConnection,
    season: str,
    gameweek: int,
    table: str = "fact_manager_pick",
) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT
            '{season}' AS season,
            CAST({gameweek} AS TINYINT) AS gameweek,
            CAST(p.entry_id AS INTEGER) AS entry_id,
            p.sample_group,
            CAST(p.overall_rank AS INTEGER) AS overall_rank,
            CAST(p.element_id AS INTEGER) AS element_id,
            m.player_master_id,
            CAST(p.pick_position AS TINYINT) AS pick_position,
            CAST(p.multiplier AS TINYINT) AS multiplier,
            CAST(p.is_captain AS BOOLEAN) AS is_captain,
            CAST(p.is_vice_captain AS BOOLEAN) AS is_vice_captain
        FROM src_pick p
        LEFT JOIN player_map m ON CAST(p.element_id AS INTEGER) = m.element_id
        """
    )


def build_agg_player_ownership(
    con: duckdb.DuckDBPyConnection,
    season: str,
    gameweek: int,
    *,
    picks: str = "fact_manager_pick",
    table: str = "agg_player_ownership",
) -> None:
    """Ownership per (element, sample_group), against the real denominator."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT
            '{season}' AS season,
            CAST({gameweek} AS TINYINT) AS gameweek,
            p.element_id,
            min(p.player_master_id) AS player_master_id,
            p.sample_group,
            CAST(s.sample_size AS INTEGER) AS sample_size,
            CAST(count(DISTINCT p.entry_id) AS INTEGER) AS owned_count,
            count(DISTINCT p.entry_id) * 100.0 / s.sample_size AS ownership_pct,
            CAST(count(DISTINCT p.entry_id)
                 FILTER (WHERE p.pick_position <= {STARTING_POSITIONS}) AS INTEGER)
                AS starting_count,
            count(DISTINCT p.entry_id) FILTER (WHERE p.pick_position <= {STARTING_POSITIONS})
                * 100.0 / s.sample_size AS starting_pct,
            CAST(count(DISTINCT p.entry_id) FILTER (WHERE p.is_captain) AS INTEGER)
                AS captain_count,
            count(DISTINCT p.entry_id) FILTER (WHERE p.is_captain) * 100.0 / s.sample_size
                AS captain_pct
        FROM {picks} p
        JOIN sample_size s ON p.sample_group = s.sample_group
        GROUP BY p.element_id, p.sample_group, s.sample_size
        """
    )


def merge_by_gameweek(
    con: duckdb.DuckDBPyConnection,
    table: str,
    *,
    existing: str | None,
    gameweek: int,
) -> None:
    """Append this gameweek to the season's file, replacing any earlier version.

    Same whole-file rewrite as the match facts: at ~30k rows a gameweek it is far
    simpler than partitioning, and re-running a gameweek has to be safe.
    """
    if existing is None:
        return
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT * FROM {existing} WHERE gameweek <> {gameweek}
        UNION ALL
        SELECT * FROM {table}
        """
    )
