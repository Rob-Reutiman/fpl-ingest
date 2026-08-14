"""Historical backfill — load past seasons from the community archive into R2.

One shot, re-runnable, `workflow_dispatch` only. Orchestration lives here; the
fetching, transforms, checks and reporting live in `fpl.backfill.*`.

Nothing is uploaded until every season has been built and validated. A partial
bucket is worse than no bucket: a consumer can't tell a half-loaded season from a
complete one.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from fpl import curated_schema, keys
from fpl.backfill import archive, report, transform, validate
from fpl.backfill.archive import SeasonSources
from fpl.identity import MasterPlayer, MasterRegistry
from fpl.jobs.common import configure_logging
from fpl.r2_client import (
    CSV_CONTENT_TYPE,
    MARKDOWN_CONTENT_TYPE,
    PARQUET_CONTENT_TYPE,
    ObjectStore,
    build_store,
)
from fpl.transforms import parquet, team_fixture

logger = logging.getLogger(__name__)

# Oldest first, always: master ids accrete chronologically so `first_seen_season`
# means what it says, and a re-run allocates the same ids in the same order.
DEFAULT_SEASONS = ("2023-24", "2024-25", "2025-26")

DEFAULT_CACHE_DIR = Path("data/archive")

REVIEW_FILENAME = "player_match_review.csv"
REPORT_FILENAME = "backfill_report.md"

_CONTENT_TYPES = {".parquet": PARQUET_CONTENT_TYPE, ".csv": CSV_CONTENT_TYPE}


def load_existing_masters(store: ObjectStore) -> list[MasterPlayer]:
    """Read `dim_player_master` back so a re-run extends rather than reassigns.

    This is what makes the job non-duplicating: existing players keep their ids and
    only genuinely new ones get fresh ones.
    """
    body = store.get_bytes(keys.master_key("dim_player_master.parquet"))
    if body is None:
        logger.info("no existing dim_player_master; starting master ids from 1")
        return []

    con = duckdb.connect(":memory:")
    with TemporaryDirectory(prefix="fpl-master-") as tmp:
        path = Path(tmp) / "dim_player_master.parquet"
        path.write_bytes(body)
        try:
            rows = con.execute(
                "SELECT player_master_id, player_code, canonical_first_name, "
                "canonical_second_name, canonical_web_name, normalized_name_key, "
                f"first_seen_season, last_seen_season FROM read_parquet('{path.as_posix()}') "
                "ORDER BY player_master_id"
            ).fetchall()
        finally:
            con.close()

    logger.info("loaded %s existing master players", len(rows))
    return [MasterPlayer(*row) for row in rows]


def build_season(
    con: duckdb.DuckDBPyConnection,
    season: str,
    sources: SeasonSources,
    registry: MasterRegistry,
    staging_dir: Path,
    results: validate.CheckResults,
) -> tuple[report.SeasonStats, dict[int, int]]:
    """Transform one season and stage its Parquet locally.

    Returns its stats and its `{element_id: player_master_id}` map, which the
    caller accumulates into `map_player_season`.
    """
    logger.info("building %s", season)
    transform.load_season_sources(con, sources)

    transform.build_dim_team(con, season)
    con.execute("INSERT INTO all_dim_team SELECT * FROM dim_team")

    players = transform.read_season_players(con, season)
    new_ids_before = registry.new_ids_by_season.get(season, 0)
    review_before = len(registry.review)
    assigned = registry.resolve_season(players)
    transform.register_master_map(con, season, assigned)

    transform.build_dim_player(con, season)
    transform.build_dim_fixture(con, season)
    transform.build_dim_gameweek(con, season)

    null_filled = transform.build_fact_source(con, sources)
    managers, duplicates = transform.count_excluded_rows(con)
    transform.build_fact_player_fixture(con, season)
    transform.build_fact_player_gameweek_fpl(con, season)
    team_fixture.build_fact_team_fixture(con, season)

    validate.validate_season(con, season, results)

    stats = report.SeasonStats(
        season=season,
        players=len(players),
        new_master_ids=registry.new_ids_by_season.get(season, 0) - new_ids_before,
        review_rows=len(registry.review) - review_before,
        manager_rows_dropped=managers,
        duplicate_rows_collapsed=duplicates,
        null_filled_columns=null_filled,
    )
    for table, order_by in curated_schema.SEASON_TABLES.items():
        parquet.write_parquet(con, table, staging_dir / season / f"{table}.parquet", order_by)
        count = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        stats.table_rows[table] = int(count[0]) if count else 0
    return stats, assigned


def build_master(
    con: duckdb.DuckDBPyConnection,
    registry: MasterRegistry,
    season_map: Sequence[tuple[int, str, int]],
    staging_dir: Path,
) -> None:
    transform.build_team_master(con)
    transform.register_player_master(con, registry.masters, season_map)
    for table, order_by in curated_schema.MASTER_TABLES.items():
        parquet.write_parquet(con, table, staging_dir / "master" / f"{table}.parquet", order_by)
    parquet.write_review_csv(registry.review, staging_dir / "master" / REVIEW_FILENAME)


def upload(store: ObjectStore, staging_dir: Path, seasons: Sequence[str]) -> list[str]:
    """Push the staged curated files. Called only after validation passes."""
    written: list[str] = []
    for season in seasons:
        for table in curated_schema.SEASON_TABLES:
            path = staging_dir / season / f"{table}.parquet"
            key = keys.curated_key(season, table)
            store.put_bytes(key, path.read_bytes(), content_type=PARQUET_CONTENT_TYPE)
            written.append(key)

    for path in sorted((staging_dir / "master").iterdir()):
        key = keys.master_key(path.name)
        content_type = _CONTENT_TYPES.get(path.suffix, MARKDOWN_CONTENT_TYPE)
        store.put_bytes(key, path.read_bytes(), content_type=content_type)
        written.append(key)
    return written


def upload_provenance(store: ObjectStore, sources: SeasonSources) -> list[str]:
    """Copy the source files to R2 unmodified.

    Storage is trivial at this size and it means a transformation bug is fixable by
    re-running the transform, without re-fetching from a third-party repo that may
    have changed or disappeared in the meantime.
    """
    written: list[str] = []
    for filename, path in sources.paths.items():
        key = keys.raw_archive_key(sources.season, filename)
        content_type = _CONTENT_TYPES.get(path.suffix, MARKDOWN_CONTENT_TYPE)
        store.put_bytes(key, path.read_bytes(), content_type=content_type)
        written.append(key)
    return written


def run(
    store: ObjectStore,
    *,
    seasons: Sequence[str] = DEFAULT_SEASONS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    staging_dir: Path | None = None,
    rebuild_master: bool = False,
    refresh: bool = False,
) -> list[str]:
    """Load every season, validate, then upload. Returns the keys written."""
    ordered = sorted(seasons)
    if list(ordered) != list(seasons):
        logger.info("reordering seasons oldest-first: %s", ordered)

    with TemporaryDirectory(prefix="fpl-backfill-") as tmp:
        staging = staging_dir or Path(tmp)
        con = parquet.connect()
        try:
            con.execute(curated_schema.create_table_ddl("dim_team", name="all_dim_team"))
            registry = MasterRegistry([] if rebuild_master else load_existing_masters(store))
            results = validate.CheckResults()

            fetched: list[SeasonSources] = []
            stats: list[report.SeasonStats] = []
            season_map: list[tuple[int, str, int]] = []
            for season in ordered:
                sources = archive.fetch_season(season, cache_dir, refresh=refresh)
                fetched.append(sources)
                season_stats, assigned = build_season(
                    con, season, sources, registry, staging, results
                )
                stats.append(season_stats)
                season_map += [
                    (master_id, season, element_id)
                    for element_id, master_id in sorted(assigned.items())
                ]

            build_master(con, registry, season_map, staging)
            validate.validate_output(staging, ordered, results)

            (staging / "master" / REPORT_FILENAME).write_text(
                report.render(
                    stats,
                    master_totals=_master_totals(registry),
                    review_by_method=_review_by_method(registry),
                    warnings=results.warnings,
                    notes=results.notes,
                ),
                encoding="utf-8",
            )

            written = upload(store, staging, ordered)
            for sources in fetched:
                written += upload_provenance(store, sources)
        finally:
            con.close()

    logger.info("backfill complete: %s objects written", len(written))
    return written


def _master_totals(registry: MasterRegistry) -> dict[str, int]:
    return {
        "master players total": len(registry.masters),
        "master players with a stable player_code": sum(
            1 for master in registry.masters if master.player_code is not None
        ),
        "review rows": len(registry.review),
    }


def _review_by_method(registry: MasterRegistry) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in registry.review:
        counts[row.match_method] = counts.get(row.match_method, 0) + 1
    return counts


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seasons",
        default=",".join(DEFAULT_SEASONS),
        help="comma-separated YYYY-YY seasons to load (always processed oldest first)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="where to cache the downloaded source CSVs",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=None,
        help="keep the generated Parquet here instead of a temp dir, for inspection",
    )
    parser.add_argument(
        "--rebuild-master",
        action="store_true",
        help="ignore existing master tables and reassign every player_master_id",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-download source files even if cached"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and validate everything but log the R2 writes instead of performing them",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    store = build_store(dry_run=args.dry_run)
    run(
        store,
        seasons=[s.strip() for s in args.seasons.split(",") if s.strip()],
        cache_dir=args.cache_dir,
        staging_dir=args.staging_dir,
        rebuild_master=args.rebuild_master,
        refresh=args.refresh,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
