"""Capture the squads of some 2,000 highly ranked managers for one gameweek.

Runs on settlement. Picks for a gameweek return 404 until its deadline passes,
and the overall league standings describe that gameweek's cohort once its points
settle, so waiting draws the cohort and their squads from one moment in time.

Responses accumulate in memory and reach `raw/` in four writes, holding a 2,000
entry harvest to a handful of PutObject calls.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx

from fpl import keys
from fpl.constants import TOP_PAGE_COUNT
from fpl.fpl_client import FPLClient
from fpl.gameweek import Target, partial_metadata, resolve_target
from fpl.jobs.common import configure_logging, parse_args
from fpl.r2_client import PARQUET_CONTENT_TYPE, ObjectStore, build_store
from fpl.sampling import seeded_rng, select_entries, select_pages
from fpl.season import derive_season
from fpl.transforms import master, ownership, parquet

logger = logging.getLogger(__name__)

TOP_GROUP = "top1000"
SAMPLED_GROUP = "sampled"


@dataclass(frozen=True)
class CohortEntry:
    entry_id: int
    rank: int | None
    group: str


def _results(page: dict[str, Any]) -> list[dict[str, Any]]:
    return page.get("standings", {}).get("results", [])


def _to_cohort(results: list[dict[str, Any]], group: str) -> list[CohortEntry]:
    return [CohortEntry(r["entry"], r.get("rank"), group) for r in results]


def collect_cohort(
    client: FPLClient, rng: random.Random
) -> tuple[list[CohortEntry], list[dict[str, Any]], list[dict[str, Any]], list[int]]:
    """Build the manager cohort from the overall league standings.

    Returns the cohort, the raw pages behind it for storage as fetched, and the
    page numbers the random draw landed on.
    """
    top_pages = [json.loads(client.standings_page(p)) for p in range(1, TOP_PAGE_COUNT + 1)]
    cohort = [e for page in top_pages for e in _to_cohort(_results(page), TOP_GROUP)]

    sampled_page_numbers = select_pages(rng)
    sampled_pages = [json.loads(client.standings_page(p)) for p in sampled_page_numbers]
    for page in sampled_pages:
        cohort += _to_cohort(select_entries(_results(page), rng), SAMPLED_GROUP)

    deduped: dict[int, CohortEntry] = {}
    for entry in cohort:
        deduped.setdefault(entry.entry_id, entry)
    if len(deduped) != len(cohort):
        logger.warning("dropped %d duplicate entries from the cohort", len(cohort) - len(deduped))

    return list(deduped.values()), top_pages, sampled_pages, sampled_page_numbers


def harvest_picks(
    client: FPLClient, cohort: list[CohortEntry], gw: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch every manager's picks. Returns ``(records, failures)``.

    An entry still failing after its retries is recorded by id and the harvest
    continues, so one bad entry costs one squad.
    """
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for entry in cohort:
        try:
            picks = json.loads(client.entry_picks(entry.entry_id, gw))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("entry %d failed: %s", entry.entry_id, exc)
            failures.append(
                {
                    "entry_id": entry.entry_id,
                    "group": entry.group,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        records.append(
            {
                "entry_id": entry.entry_id,
                "gameweek": gw,
                "group": entry.group,
                "rank": entry.rank,
                "data": picks,
            }
        )

    return records, failures


def _summary(
    season: str,
    target: Target,
    cohort: list[CohortEntry],
    records: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    sampled_page_numbers: list[int],
) -> dict[str, Any]:
    return {
        "season": season,
        "gameweek": target.gw,
        "partial": target.partial,
        "pending_fixture_ids": target.pending_fixture_ids,
        "requested": len(cohort),
        "succeeded": len(records),
        "failed": len(failures),
        "failures": failures,
        "sampled_pages": sampled_page_numbers,
        "ingested_at": datetime.now(UTC).isoformat(),
    }


def run(client: FPLClient, store: ObjectStore, now: datetime | None = None) -> int | None:
    """Harvest the cohort for the earliest settled gameweek absent from the bucket."""
    bootstrap = json.loads(client.bootstrap_static())
    season = derive_season(bootstrap)

    target = resolve_target(
        bootstrap["events"],
        already_ingested=lambda gw: store.exists(keys.manager_summary_key(season, gw)),
        fetch_fixtures=lambda gw: json.loads(client.fixtures(event=gw)),
        now=now,
    )
    if target is None:
        logger.info("season %s: no gameweek ready to sample", season)
        return None

    gw = target.gw
    metadata = partial_metadata(target)
    if target.partial:
        logger.warning("GW%d sampled with fixture(s) %s postponed", gw, target.pending_fixture_ids)

    rng = seeded_rng(season, gw)
    cohort, top_pages, sampled_pages, sampled_page_numbers = collect_cohort(client, rng)
    logger.info(
        "GW%d cohort: %d managers across %d standings pages",
        gw,
        len(cohort),
        TOP_PAGE_COUNT + len(sampled_pages),
    )

    # An empty summary would mark this gameweek done for good. The standings
    # read empty until the first gameweek of a season settles, so fail early.
    if not cohort:
        raise RuntimeError(f"GW{gw}: overall league standings returned no entries")

    records, failures = harvest_picks(client, cohort, gw)
    logger.info("GW%d picks: %d succeeded, %d failed", gw, len(records), len(failures))

    store.put_json(keys.standings_top_key(season, gw), top_pages, metadata=metadata)
    store.put_json(keys.standings_sample_key(season, gw), sampled_pages, metadata=metadata)
    store.put_ndjson(keys.manager_picks_key(season, gw), records, metadata=metadata)

    transform(store, season, target, bootstrap, records, metadata)

    # Written last, and the key `already_ingested` above checks. Its presence
    # marks a finished run, leaving a broken one to be retried.
    store.put_json(
        keys.manager_summary_key(season, gw),
        _summary(season, target, cohort, records, failures, sampled_page_numbers),
        metadata=metadata,
    )
    return gw


def transform(
    store: ObjectStore,
    season: str,
    target: Target,
    bootstrap: dict[str, Any],
    records: list[dict[str, Any]],
    metadata: dict[str, str],
) -> None:
    """Turn harvested picks into `fact_manager_pick` and `agg_player_ownership`."""
    rows = ownership.pick_rows(records)
    sizes = ownership.sample_sizes(records)
    logger.info(
        "GW%d ownership denominators: %s",
        target.gw,
        ", ".join(f"{group}={size}" for group, size in sorted(sizes.items())),
    )

    with TemporaryDirectory(prefix="fpl-picks-") as tmp:
        scratch = Path(tmp)
        with master.MasterTables(store, scratch) as masters:
            assigned = masters.resolve(season, bootstrap)
            if masters.changed:
                masters.write(store)

            con = parquet.connect()
            try:
                master.register_player_map(con, season, assigned)
                ownership.load_picks(con, scratch, rows, sizes)
                ownership.build_fact_manager_pick(con, season, target.gw)
                ownership.build_agg_player_ownership(con, season, target.gw)

                for table in ("fact_manager_pick", "agg_player_ownership"):
                    previous = store.get_bytes(keys.curated_key(season, table))
                    loaded = parquet.register_parquet(con, f"existing_{table}", previous, scratch)
                    ownership.merge_by_gameweek(
                        con,
                        table,
                        existing=f"existing_{table}" if loaded else None,
                        gameweek=target.gw,
                    )
                    store.put_bytes(
                        keys.curated_key(season, table),
                        parquet.to_parquet_bytes(con, table, scratch),
                        content_type=PARQUET_CONTENT_TYPE,
                        metadata=metadata,
                    )
            finally:
                con.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(__doc__ or "", argv)
    configure_logging()
    store = build_store(dry_run=args.dry_run)
    with FPLClient() as client:
        run(client, store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
