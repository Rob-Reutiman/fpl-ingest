"""Hourly refresh of the live snapshot and the season's dimension tables.

Writes both layers every run. The bootstrap and fixtures bodies land verbatim
under `raw/`, then `fpl_current` and the four dimensions under `curated/`.

This job overwrites unconditionally, since prices, ownership and injury news
describe the present. Each body also lands under a key naming the day, which
retains one snapshot per day and keeps `curated/` rebuildable from `raw/`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from fpl import keys
from fpl.fpl_client import FPLClient
from fpl.jobs.common import configure_logging, parse_args
from fpl.r2_client import PARQUET_CONTENT_TYPE, ObjectStore, build_store
from fpl.season import derive_season
from fpl.transforms import current, master, parquet

logger = logging.getLogger(__name__)


def run(
    client: FPLClient,
    store: ObjectStore,
    on: date | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Fetch, store raw, transform, upload curated. Returns the keys written."""
    now = now or datetime.now(UTC)
    on = on or now.date()

    bootstrap_body = client.bootstrap_static()
    fixtures_body = client.fixtures()
    bootstrap = json.loads(bootstrap_body)
    fixtures = json.loads(fixtures_body)
    season = derive_season(bootstrap)
    logger.info(
        "season %s, %d players, %d fixtures",
        season,
        len(bootstrap["elements"]),
        len(fixtures),
    )

    written = [
        _put(store, keys.current_bootstrap_key(season), bootstrap_body),
        _put(store, keys.current_fixtures_key(season), fixtures_body),
        # Provenance. The hourly keys above are overwritten in place, so a key
        # naming the day retains that day's last snapshot.
        _put(store, keys.bootstrap_key(season, on), bootstrap_body),
        _put(store, keys.fixtures_key(season, on), fixtures_body),
    ]

    with TemporaryDirectory(prefix="fpl-current-") as tmp:
        scratch = Path(tmp)
        with master.MasterTables(store, scratch) as masters:
            assigned = masters.resolve(season, bootstrap)
            if masters.changed:
                written += masters.write(store)

            con = parquet.connect()
            try:
                current.load_snapshot(con, scratch, bootstrap, fixtures)
                master.register_player_map(con, season, assigned)
                current.build_all(con, season, now)

                for table in current.TABLES:
                    key = keys.curated_key(season, table)
                    store.put_bytes(
                        key,
                        parquet.to_parquet_bytes(con, table, scratch),
                        content_type=PARQUET_CONTENT_TYPE,
                    )
                    written.append(key)
                    _log_rows(con, table)
            finally:
                con.close()

    return written


def _put(store: ObjectStore, key: str, body: bytes) -> str:
    store.put_bytes(key, body)
    return key


def _log_rows(con: duckdb.DuckDBPyConnection, table: str) -> None:
    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    logger.info("%s: %s rows", table, rows[0] if rows else 0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(__doc__ or "", argv)
    configure_logging()
    store = build_store(dry_run=args.dry_run)
    with FPLClient() as client:
        run(client, store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
