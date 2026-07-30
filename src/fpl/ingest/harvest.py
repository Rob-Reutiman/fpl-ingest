"""Module 6 — picks and transfer harvesting.

Turns the cohort roster (Module 5) into data: for every sampled manager, fetch
their picks for a gameweek and their transfer history, and write both through
``Storage``. Picks must be harvested *after* the GW deadline — the API returns
404 for a gameweek whose deadline hasn't passed yet.

Failure isolation is per manager: a deleted account 404s, a flaky response
5xxes, and neither takes down the other 9,999 requests. Every outcome is
tallied into a :class:`HarvestResult` so the caller (and the cron log) can see
at a glance whether a run is trustworthy.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from fpl.client import FPLClient
from fpl.ingest.mappers import map_cohort_picks, map_cohort_transfers
from fpl.storage import Storage

logger = logging.getLogger(__name__)

# Managers fetched per get_many call. Same rationale as the standings crawl:
# a 10k-manager gather issued at once gives no progress signal for minutes and
# holds every coroutine in flight; chunking yields a log line per chunk.
HARVEST_CHUNK_SIZE = 100


@dataclass(frozen=True)
class HarvestResult:
    """Outcome tally for one harvest run. ``attempted == succeeded + failed + skipped``."""

    attempted: int
    succeeded: int
    failed: int  # 5xx, timeouts, other errors — worth re-running.
    skipped: int  # 404s — deleted/invalid managers, permanently gone.
    duration_seconds: float


def _is_404(result: BaseException) -> bool:
    return isinstance(result, httpx.HTTPStatusError) and result.response.status_code == 404


async def _fetch_in_chunks(
    client: FPLClient,
    label: str,
    paths: list[str],
    manager_ids: list[int],
) -> list[tuple[int, Any]]:
    """Fetch ``paths`` in chunks, returning ``(manager_id, result)`` pairs.

    A result is either the parsed payload or the exception that request raised
    (``get_many(return_exceptions=True)``) — classification is the caller's job.
    """
    results: list[tuple[int, Any]] = []
    for start in range(0, len(paths), HARVEST_CHUNK_SIZE):
        chunk = paths[start : start + HARVEST_CHUNK_SIZE]
        chunk_results = await client.get_many(chunk, return_exceptions=True)
        results.extend(zip(manager_ids[start : start + len(chunk)], chunk_results, strict=True))
        logger.info(
            "harvest %s: %d/%d managers",
            label,
            min(start + HARVEST_CHUNK_SIZE, len(paths)),
            len(paths),
        )
    return results


def _classify(
    fetched: list[tuple[int, Any]],
    label: str,
) -> tuple[list[tuple[int, Any]], int, int]:
    """Split fetch results into (successes, skipped_count, failed_count)."""
    successes: list[tuple[int, Any]] = []
    skipped = 0
    failed = 0
    for manager_id, result in fetched:
        if isinstance(result, BaseException):
            if _is_404(result):
                skipped += 1
                logger.debug("harvest %s: manager %d gone (404)", label, manager_id)
            else:
                failed += 1
                logger.warning("harvest %s: manager %d failed: %r", label, manager_id, result)
        else:
            successes.append((manager_id, result))
    return successes, skipped, failed


async def harvest_picks(
    client: FPLClient,
    storage: Storage,
    manager_ids: list[int],
    gw: int,
) -> HarvestResult:
    """Fetch and store every cohort manager's picks for ``gw``.

    Safe to re-run: responses are disk-cached (a picks URL embeds the GW and is
    immutable post-deadline) and ``cohort_pick``'s primary key makes the write
    an upsert.
    """
    started = time.monotonic()
    paths = [f"entry/{mid}/event/{gw}/picks/" for mid in manager_ids]

    fetched = await _fetch_in_chunks(client, "picks", paths, manager_ids)
    successes, skipped, failed = _classify(fetched, "picks")

    rows: list[dict] = []
    for manager_id, payload in successes:
        rows.extend(map_cohort_picks(payload, manager_id=manager_id, gameweek=gw))
    storage.insert_picks(rows)

    elapsed = time.monotonic() - started
    logger.info(
        "harvest picks: gw=%d attempted=%d ok=%d skipped=%d failed=%d rows=%d %.1fs",
        gw,
        len(manager_ids),
        len(successes),
        skipped,
        failed,
        len(rows),
        elapsed,
    )
    return HarvestResult(
        attempted=len(manager_ids),
        succeeded=len(successes),
        failed=failed,
        skipped=skipped,
        duration_seconds=elapsed,
    )


async def harvest_transfers(
    client: FPLClient,
    storage: Storage,
    manager_ids: list[int],
    target_gw: int | None = None,
) -> HarvestResult:
    """Fetch every cohort manager's transfer history; store ``target_gw``'s rows.

    ``target_gw=None`` stores the full season history.

    The transfers URL is the same all season while its response grows, so the
    client's indefinite disk cache would otherwise serve week N-1's snapshot
    forever. The ``h`` query param (ignored by the API) scopes the cache key to
    a gameweek — post-deadline that GW's transfer list is frozen, so cached
    copies stay valid and a crashed run still resumes from cache. Unfiltered
    harvests scope the key to the calendar day instead.
    """
    started = time.monotonic()
    bust = f"gw{target_gw}" if target_gw is not None else time.strftime("%Y%m%d")
    paths = [f"entry/{mid}/transfers/?h={bust}" for mid in manager_ids]

    fetched = await _fetch_in_chunks(client, "transfers", paths, manager_ids)
    successes, skipped, failed = _classify(fetched, "transfers")

    rows: list[dict] = []
    for _manager_id, payload in successes:
        rows.extend(map_cohort_transfers(payload, target_gw=target_gw))
    storage.insert_transfers(rows)

    elapsed = time.monotonic() - started
    logger.info(
        "harvest transfers: target_gw=%s attempted=%d ok=%d skipped=%d failed=%d rows=%d %.1fs",
        target_gw,
        len(manager_ids),
        len(successes),
        skipped,
        failed,
        len(rows),
        elapsed,
    )
    return HarvestResult(
        attempted=len(manager_ids),
        succeeded=len(successes),
        failed=failed,
        skipped=skipped,
        duration_seconds=elapsed,
    )
