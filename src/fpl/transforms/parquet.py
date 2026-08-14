"""Writing curated files, and reading existing ones back.

Every curated Parquet file in the bucket is written by `write_parquet`, so the
column contract and the deterministic ordering are applied in exactly one place.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from pathlib import Path

import duckdb

from fpl import curated_schema
from fpl.identity import REVIEW_COLUMNS, ReviewRow

logger = logging.getLogger(__name__)


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def write_parquet(
    con: duckdb.DuckDBPyConnection,
    table: str,
    destination: Path,
    order_by: Sequence[str] | None = None,
) -> Path:
    """Project a table through the column contract and write one zstd Parquet file.

    The explicit ORDER BY is what makes a re-run reproduce the same file rather
    than the same rows in whatever order the joins happened to produce. Defaults
    to the contract's sort key for the table.
    """
    if order_by is None:
        order_by = curated_schema.sort_key(table)
    destination.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY (
            SELECT
                {curated_schema.select_list(table)}
            FROM {table}
            ORDER BY {", ".join(order_by)}
        ) TO '{destination.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
        """
    )
    return destination


def to_parquet_bytes(
    con: duckdb.DuckDBPyConnection,
    table: str,
    scratch: Path,
    order_by: Sequence[str] | None = None,
) -> bytes:
    """`write_parquet` into a scratch directory and hand back the bytes to upload."""
    return write_parquet(con, table, scratch / f"{table}.parquet", order_by).read_bytes()


def register_parquet(
    con: duckdb.DuckDBPyConnection, name: str, body: bytes | None, scratch: Path
) -> bool:
    """Register previously-written Parquet bytes as a view. False if there were none.

    Curated files are read back whenever a job appends to one — DuckDB reads from a
    path, so the bytes land in a scratch file first.
    """
    if body is None:
        return False
    path = scratch / f"{name}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{path.as_posix()}')")
    return True


def write_review_csv(rows: Iterable[ReviewRow], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = list(REVIEW_COLUMNS)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r.season, r.element_id)):
            writer.writerow(asdict(row))
    return destination


def review_csv_bytes(rows: Iterable[ReviewRow], scratch: Path) -> bytes:
    return write_review_csv(rows, scratch / "player_match_review.csv").read_bytes()


def parse_review_csv(body: bytes | None) -> list[ReviewRow]:
    """Read back an existing review file so new findings append rather than replace."""
    if not body:
        return []
    reader = csv.DictReader(body.decode("utf-8").splitlines())
    return [
        ReviewRow(
            season=row["season"],
            element_id=int(row["element_id"]),
            web_name=row["web_name"],
            first_name=row["first_name"],
            second_name=row["second_name"],
            team_short=row["team_short"],
            candidate_master_ids=row["candidate_master_ids"],
            match_method=row["match_method"],
            confidence=row["confidence"],
            resolution=row.get("resolution", ""),
        )
        for row in reader
    ]
