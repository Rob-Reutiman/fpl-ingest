"""Module 5 — cohort discovery and sampling.

Decides *which* managers to track for a gameweek. This module identifies the
cohort; it does not fetch their picks or transfers (that's the harvest module).

The pool narrows as the season progresses, because rank means progressively
more. Early on, standings are dominated by captain luck and differential hauls,
so a wide pool sampled purely at random is the honest read of "engaged
managers". By GW 10 the top 25k is genuinely ability-selected and a top slice
becomes worth taking at 100%.

IMPORTANT — from GW 5 the two slices are *separate populations*, not one sample.
The top slice is sampled at 100% and the random slice at ~5-10% of its stratum.
Pooling them into a single ownership number represents neither population.
Downstream analytics must compute metrics per slice and report both.

Boundary caveat: the pool changes at GW 4->5 and GW 9->10, so effective-ownership
trends spanning those weeks are not directly comparable and will show artificial
jumps. Transfer flow is unaffected — it's computed within a single week's sample.
"""

from __future__ import annotations

import logging
import random

from fpl.client import FPLClient
from fpl.config import settings
from fpl.constants import COHORT_STRATEGY, ENTRIES_PER_PAGE, OVERALL_LEAGUE_ID
from fpl.ingest.mappers import StandingsEntry, map_standings_page, standings_has_next
from fpl.storage import Storage

logger = logging.getLogger(__name__)

# Standings pages fetched per get_many call. A full 100k pool is 2,000 pages;
# issuing them as one gather gives no progress signal for several minutes and
# holds every coroutine in flight at once.
PAGE_CHUNK_SIZE = 100


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def get_cohort_strategy(gw: int) -> str:
    """Return the ``COHORT_STRATEGY`` key that applies for this gameweek."""
    if gw <= 1:
        return "template"
    if gw <= 4:
        return "gw_2_4"
    if gw <= 9:
        return "gw_5_9"
    return "gw_10_plus"


def is_template_only(gw: int) -> bool:
    """True if this GW uses global ownership instead of a manager cohort.

    Pre-season and GW 1 there is nothing meaningful to rank, so effective
    ownership comes from ``selected_by_percent`` in bootstrap-static instead.
    """
    return get_cohort_strategy(gw) == "template"


def cohort_seed(gw: int, season_year: int) -> int:
    """Derive the sampling seed for a gameweek.

    The seed deliberately changes every gameweek, so the random slice re-draws
    weekly; cross-week overlap of ~5-10% by chance is fine because transfer flow
    is computed within a single week's sample.

    What fixing the seed buys is *idempotency*: re-running the same gameweek's
    ingestion after a crash reproduces the identical sample, so a partial
    harvest resumes cleanly instead of chasing a different set of managers.
    """
    return gw * 10_000 + season_year


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def sample_cohort(
    manager_ids_with_ranks: list[tuple[int, int]],
    top_slice: int = 5_000,
    random_slice: int = 5_000,
    seed: int | None = None,
) -> list[tuple[int, int, bool]]:
    """Draw the cohort from a ranked pool.

    Args:
        manager_ids_with_ranks: ``(manager_id, rank)`` pairs, any order.
        top_slice: Best-ranked managers taken at 100%. These are the sharpest
            signal — the consensus that defines "the correct team". Pass 0 for
            GW 2-4, where rank is still luck rather than skill.
        random_slice: Drawn uniformly from everything below the top slice.
            Captures breadth: emerging transfers and differential thinking that
            hasn't reached the very top yet.
        seed: See :func:`cohort_seed`. ``None`` means non-deterministic.

    Returns:
        ``(manager_id, rank, is_top_slice)``, top slice first.
    """
    ordered = sorted(manager_ids_with_ranks, key=lambda pair: pair[1])

    top = ordered[:top_slice]
    remainder = ordered[top_slice:]

    # Clamp: a pool smaller than requested is legitimate (a short league, or a
    # truncated scrape) and must not raise out of random.sample.
    draw_count = min(random_slice, len(remainder))
    rng = random.Random(seed)  # Local RNG — never perturbed by global seeding.
    drawn = rng.sample(remainder, draw_count) if draw_count else []

    return [(mid, rank, True) for mid, rank in top] + [(mid, rank, False) for mid, rank in drawn]


# ---------------------------------------------------------------------------
# Standings crawl
# ---------------------------------------------------------------------------


async def scrape_current_standings(
    client: FPLClient,
    gw: int,
    max_rank: int = 50_000,
) -> list[StandingsEntry]:
    """Paginate the overall league's standings and collect the ranked pool.

    Pure fetch — persistence is :func:`ingest_cohort`'s job, because
    ``cohort_manager.is_top_slice`` isn't known until after sampling.

    Pages needed is ``max_rank // 50``: 2,000 for a 100k pool, 1,000 for 50k,
    500 for 25k. The crawl stops early once the API reports no further pages,
    so a league shorter than ``max_rank`` costs one chunk rather than the full
    page count.
    """
    pages_needed = max_rank // ENTRIES_PER_PAGE
    paths = [
        f"leagues-classic/{OVERALL_LEAGUE_ID}/standings/?page_standings={page}"
        for page in range(1, pages_needed + 1)
    ]

    entries: list[StandingsEntry] = []
    for start in range(0, len(paths), PAGE_CHUNK_SIZE):
        chunk = paths[start : start + PAGE_CHUNK_SIZE]
        pages = await client.get_many(chunk)
        for page in pages:
            entries.extend(map_standings_page(page))

        exhausted = not all(standings_has_next(page) for page in pages)
        logger.info(
            "standings gw=%d: %d/%d pages, %d entries%s",
            gw,
            min(start + PAGE_CHUNK_SIZE, len(paths)),
            len(paths),
            len(entries),
            " (end of league)" if exhausted else "",
        )
        if exhausted:
            break

    return entries


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def ingest_cohort(
    client: FPLClient,
    storage: Storage,
    gw: int,
    seed: int | None = None,
) -> list[tuple[int, int, bool]]:
    """Scrape, sample, and persist the cohort for ``gw``.

    The only writer of ``cohort_manager``. Re-running a gameweek is safe: the
    seed is deterministic for a given ``(gw, season_year)`` so the sample is
    identical, and the write is an upsert on ``(gameweek, manager_id)``.

    Returns the sampled ``(manager_id, rank, is_top_slice)`` triples — empty for
    template-only gameweeks.
    """
    strategy = get_cohort_strategy(gw)
    if is_template_only(gw):
        logger.info("gw=%d is template-only (%s); no manager cohort", gw, strategy)
        return []

    config = COHORT_STRATEGY[strategy]
    pool_size = config["pool_size"]
    top_slice = config["top_slice"]
    random_slice = config["random_slice"]

    entries = await scrape_current_standings(client, gw, max_rank=pool_size)

    if seed is None:
        seed = cohort_seed(gw, settings.season_year)

    # sample_cohort works in (manager_id, rank) pairs; total_points rides along
    # in a lookup and is rejoined afterwards to satisfy the NOT NULL column.
    totals = {entry.manager_id: entry.total_points for entry in entries}
    sampled = sample_cohort(
        [(entry.manager_id, entry.rank) for entry in entries],
        top_slice=top_slice,
        random_slice=random_slice,
        seed=seed,
    )

    storage.upsert_cohort_managers(
        gw,
        [
            {
                "manager_id": manager_id,
                "rank": rank,
                "total_points": totals[manager_id],
                "is_top_slice": is_top,
            }
            for manager_id, rank, is_top in sampled
        ],
    )

    top_count = sum(1 for _, _, is_top in sampled if is_top)
    logger.info(
        "cohort gw=%d strategy=%s pool=%d sampled=%d (top=%d random=%d)",
        gw,
        strategy,
        len(entries),
        len(sampled),
        top_count,
        len(sampled) - top_count,
    )
    return sampled
