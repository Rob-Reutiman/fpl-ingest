"""One-time setup for ad-hoc exploration: point DuckDB at the R2 bucket.

Creates a small local DuckDB file with a persistent R2 secret and one view
per curated table, so a SQL client (DuckDB CLI, its local UI, DBeaver, a
notebook) can browse and query the bucket directly without downloading
anything.

    uv run python scripts/duckdb_explore.py
    duckdb -ui .duckdb/explore.duckdb      # opens a browser UI, or:
    duckdb .duckdb/explore.duckdb          # plain SQL shell

Re-run any time a new season or table shows up; views are CREATE OR REPLACE
and the secret is stored once and reused.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from fpl.config import get_settings
from fpl.curated_schema import CURRENT_SEASON_TABLES, MASTER_TABLES, SEASON_TABLES
from fpl.keys import curated_key, master_key

DB_PATH = Path(".duckdb/explore.duckdb")


def main() -> None:
    settings = get_settings()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(DB_PATH))
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")

    # Persistent secret: stored under ~/.duckdb, survives across `duckdb` CLI
    # invocations and doesn't need to be recreated per session.
    con.execute(f"""
        CREATE OR REPLACE PERSISTENT SECRET r2 (
            TYPE s3,
            KEY_ID '{settings.r2_access_key_id}',
            SECRET '{settings.r2_secret_access_key}',
            ENDPOINT '{settings.r2_account_id}.r2.cloudflarestorage.com',
            URL_STYLE 'path',
            REGION 'auto'
        )
    """)

    bucket = settings.r2_bucket
    registered: list[str] = []
    pending: list[str] = []

    # Season tables are written once per season; '*' globs every season's
    # file into one view. `season` is already a column in every row, so
    # nothing is lost by unioning them here.
    for table in {**SEASON_TABLES, **CURRENT_SEASON_TABLES}:
        glob = curated_key("*", table)
        try:
            con.execute(f"""
                CREATE OR REPLACE VIEW {table} AS
                SELECT * FROM read_parquet('s3://{bucket}/{glob}', union_by_name=true)
            """)
            registered.append(table)
        except duckdb.IOException:
            # Nothing written for this table yet (e.g. no gameweek has
            # settled this season). Skip rather than leave a dangling view.
            con.execute(f"DROP VIEW IF EXISTS {table}")
            pending.append(table)

    # Master tables already span every season in one file.
    for table in MASTER_TABLES:
        key = master_key(f"{table}.parquet")
        try:
            con.execute(f"""
                CREATE OR REPLACE VIEW {table} AS
                SELECT * FROM read_parquet('s3://{bucket}/{key}')
            """)
            registered.append(table)
        except duckdb.IOException:
            con.execute(f"DROP VIEW IF EXISTS {table}")
            pending.append(table)

    print(f"Registered {len(registered)} views in {DB_PATH}:")
    for table in sorted(registered):
        print(f"  {table}")
    if pending:
        print(f"\nNo data yet, skipped ({len(pending)}) — re-run once these exist:")
        for table in sorted(pending):
            print(f"  {table}")
    con.close()


if __name__ == "__main__":
    main()
