"""Job 2 — capture and transform a gameweek's match facts once it has settled.

Most days this is a no-op: it exits as soon as it finds nothing new to do.

`fact_player_fixture` is rewritten whole each time rather than partitioned. A full
season is ~27k rows and about 2 MB, so rewriting beats managing 38 per-gameweek
files and the small-file problem that comes with them. `fact_team_fixture` is then
regenerated from the whole thing, which is why this job needs the season's entire
fixture list and not just the gameweek's.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fpl import keys
from fpl.fpl_client import FPLClient
from fpl.gameweek import Target, partial_metadata, resolve_target
from fpl.jobs.common import configure_logging, parse_args
from fpl.r2_client import PARQUET_CONTENT_TYPE, ObjectStore, build_store
from fpl.season import derive_season
from fpl.transforms import current, dgw, master, match_facts, parquet, team_fixture

logger = logging.getLogger(__name__)

CURATED_TABLES = ("fact_player_fixture", "fact_team_fixture")


def run(client: FPLClient, store: ObjectStore, now: datetime | None = None) -> int | None:
    """Ingest and transform the earliest settled, not-yet-stored gameweek."""
    bootstrap = json.loads(client.bootstrap_static())
    season = derive_season(bootstrap)

    target = resolve_target(
        bootstrap["events"],
        already_ingested=lambda gw: store.exists(keys.gameweek_live_key(season, gw)),
        fetch_fixtures=lambda gw: json.loads(client.fixtures(event=gw)),
        now=now,
    )
    if target is None:
        logger.info("season %s: no gameweek ready to ingest", season)
        return None

    if target.partial:
        logger.warning(
            "GW%d is finished but unverified with fixture(s) %s postponed; "
            "ingesting as partial — those fixtures' stats are still pending",
            target.gw,
            target.pending_fixture_ids,
        )

    _ingest_and_transform(client, store, season, bootstrap, target)
    logger.info("ingested %s GW%d", season, target.gw)
    return target.gw


def _ingest_and_transform(
    client: FPLClient,
    store: ObjectStore,
    season: str,
    bootstrap: dict[str, Any],
    target: Target,
) -> None:
    gw = target.gw
    metadata = partial_metadata(target)

    live_body = client.event_live(gw)
    live = json.loads(live_body)

    # The whole season's fixtures: `fact_team_fixture` is regenerated over every
    # gameweek loaded so far, so it needs every fixture's score, not this week's.
    all_fixtures = json.loads(client.fixtures())
    gw_fixtures = [f for f in all_fixtures if f.get("event") == gw]

    elements = bootstrap["elements"]
    element_teams = {int(e["id"]): int(e["team"]) for e in elements}
    element_types = {int(e["id"]): int(e["element_type"]) for e in elements}
    values = {int(e["id"]): e.get("now_cost") for e in elements}

    rows = match_facts.appearance_rows(live, gw_fixtures, element_teams, element_types, values)
    rows += _double_gameweek_rows(
        client, store, season, target, live, gw_fixtures, element_teams, element_types
    )

    # Raw goes down only after every fetch has succeeded, so a run that dies
    # mid-harvest leaves no key behind for the idempotency check to trip over.
    store.put_bytes(keys.gameweek_live_key(season, gw), live_body, metadata=metadata)
    store.put_json(keys.gameweek_fixtures_key(season, gw), gw_fixtures, metadata=metadata)

    _transform(store, season, target, bootstrap, all_fixtures, rows, metadata)


def _double_gameweek_rows(
    client: FPLClient,
    store: ObjectStore,
    season: str,
    target: Target,
    live: dict[str, Any],
    fixtures: list[dict[str, Any]],
    element_teams: dict[int, int],
    element_types: dict[int, int],
) -> list[dict[str, Any]]:
    """Per-fixture rows for players whose club played twice this gameweek.

    `event/{gw}/live/` aggregates a double gameweek into a single set of stats, so
    these players come from `element-summary` instead — see `transforms/dgw.py`.
    """
    doubled = dgw.teams_with_multiple_fixtures(fixtures)
    if not doubled:
        return []

    affected = dgw.affected_elements(element_teams, doubled)
    logger.info(
        "GW%d is a double gameweek for %d team(s); fetching per-fixture stats for %d players",
        target.gw,
        len(doubled),
        len(affected),
    )

    live_stats = {int(e["id"]): e.get("stats", {}) for e in live.get("elements", [])}
    fixtures_by_id = {int(f["id"]): f for f in fixtures}
    rows: list[dict[str, Any]] = []
    mismatched = 0

    for element_id in affected:
        body = client.element_summary(element_id)
        store.put_bytes(
            keys.element_summary_key(season, target.gw, element_id),
            body,
            metadata=partial_metadata(target),
        )
        per_fixture = dgw.history_rows(
            json.loads(body),
            element_id,
            target.gw,
            fixtures_by_id,
            element_types.get(element_id),
        )
        # Cross-check the fallback against the endpoint it replaces: the
        # per-fixture rows should sum to the gameweek aggregate.
        if dgw.reconcile(element_id, per_fixture, live_stats.get(element_id, {})):
            mismatched += 1
        rows += per_fixture

    if mismatched:
        logger.warning(
            "%d of %d double-gameweek players disagree with the live aggregate — "
            "check whether either endpoint changed shape",
            mismatched,
            len(affected),
        )
    return rows


def _transform(
    store: ObjectStore,
    season: str,
    target: Target,
    bootstrap: dict[str, Any],
    fixtures: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    metadata: dict[str, str],
) -> None:
    with TemporaryDirectory(prefix="fpl-gameweek-") as tmp:
        scratch = Path(tmp)
        with master.MasterTables(store, scratch) as masters:
            resolution = masters.resolve(season, bootstrap)
            if masters.changed:
                masters.write(store)

            con = parquet.connect()
            try:
                current.load_snapshot(con, scratch, bootstrap, fixtures)
                current.build_dim_team(con, season)
                current.build_dim_fixture(con, season)
                master.register_player_map(con, season, resolution.players)

                match_facts.load_rows(con, scratch, rows)
                match_facts.build_gameweek_facts(con, season, target.gw, is_partial=target.partial)

                previous = store.get_bytes(keys.curated_key(season, "fact_player_fixture"))
                loaded = parquet.register_parquet(con, "existing_facts", previous, scratch)
                match_facts.merge_fact_player_fixture(
                    con, existing="existing_facts" if loaded else None
                )

                team_fixture.build_fact_team_fixture(con, season)
                team_fixture.warn_on_xgc_divergence(con, season)

                for table in CURATED_TABLES:
                    store.put_bytes(
                        keys.curated_key(season, table),
                        parquet.to_parquet_bytes(con, table, scratch),
                        content_type=PARQUET_CONTENT_TYPE,
                        metadata=metadata,
                    )
                count = con.execute("SELECT count(*) FROM fact_player_fixture").fetchone()
                logger.info("fact_player_fixture now holds %s rows", count[0] if count else 0)
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
