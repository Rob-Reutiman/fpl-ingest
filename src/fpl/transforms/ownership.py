"""Manager picks, and the ownership percentages aggregated from them.

`agg_player_ownership` answers the question the warehouse exists for, which is
who the best managers own that the field does not, so its denominator has to be
right. `sample_size` counts the managers whose picks were fetched, holding every
percentage in the group true to the sample that produced it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

# A squad holds 15 players. Positions 1 through 11 start and the rest sit. This
# reads the pick position, which autosubs leave alone once the matches begin.
STARTING_POSITIONS = 11


def pick_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the harvested picks into one row per manager per pick."""
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
    """Count the managers fetched per group. The ownership denominator."""
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
    """Ownership per player per group, over the managers actually fetched."""
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
    """Add this gameweek to the season's rows, replacing any earlier version.

    Rewrites the whole file. At 30k rows a gameweek that stays cheaper than
    partitioning, and it leaves a gameweek safe to run again.
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
