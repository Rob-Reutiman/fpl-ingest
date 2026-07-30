"""Acceptance tests for Module 5 — Cohort Discovery & Sampling."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fpl.ingest.cohort import (
    cohort_seed,
    get_cohort_strategy,
    ingest_cohort,
    is_template_only,
    sample_cohort,
    scrape_current_standings,
)
from fpl.storage import Storage

POOL_SIZE = 50_000


def _pool(n: int = POOL_SIZE) -> list[tuple[int, int]]:
    """(manager_id, rank) pairs, deliberately not pre-sorted by rank."""
    pairs = [(1_000_000 + rank, rank) for rank in range(1, n + 1)]
    return pairs[::-1]


def _storage(tmp_path: Path) -> Storage:
    return Storage(str(tmp_path / "test.duckdb"))


def _scalar(s: Storage, sql: str):
    row = s._conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gw,expected",
    [
        (0, "template"),
        (1, "template"),
        (2, "gw_2_4"),
        (3, "gw_2_4"),
        (4, "gw_2_4"),
        (5, "gw_5_9"),
        (7, "gw_5_9"),
        (9, "gw_5_9"),
        (10, "gw_10_plus"),
        (15, "gw_10_plus"),
        (38, "gw_10_plus"),
    ],
)
def test_strategy_boundaries(gw: int, expected: str):
    assert get_cohort_strategy(gw) == expected


def test_is_template_only():
    assert is_template_only(0) is True
    assert is_template_only(1) is True
    assert is_template_only(2) is False
    assert is_template_only(10) is False


def test_cohort_seed_formula():
    assert cohort_seed(7, 2026) == 72_026
    # Changes every gameweek by design.
    assert cohort_seed(7, 2026) != cohort_seed(8, 2026)


# ---------------------------------------------------------------------------
# sample_cohort
# ---------------------------------------------------------------------------


def test_sample_returns_exact_count():
    result = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=1)
    assert len(result) == 10_000


def test_top_slice_are_best_ranked():
    """The first 5,000 returned are ranks 1-5000, flagged is_top_slice."""
    result = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=1)
    top = result[:5_000]
    assert [rank for _, rank, _ in top] == list(range(1, 5_001))
    assert all(is_top for _, _, is_top in top)


def test_random_slice_drawn_from_remainder():
    """The other 5,000 come from ranks 5001-50000 and are not flagged."""
    result = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=1)
    rest = result[5_000:]
    assert len(rest) == 5_000
    assert all(not is_top for _, _, is_top in rest)
    assert all(5_000 < rank <= POOL_SIZE for _, rank, _ in rest)


def test_no_duplicates_across_slices():
    result = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=1)
    ids = [mid for mid, _, _ in result]
    assert len(set(ids)) == len(ids)


def test_pure_random_mode_gw_2_4():
    """top_slice=0 draws uniformly from the whole pool, nothing flagged top."""
    result = sample_cohort(_pool(100_000), top_slice=0, random_slice=10_000, seed=1)
    assert len(result) == 10_000
    assert not any(is_top for _, _, is_top in result)
    # A uniform draw over 100k should include managers from outside the top 5k.
    assert any(rank > 5_000 for _, rank, _ in result)
    assert max(rank for _, rank, _ in result) > 50_000


def test_same_seed_identical_sample():
    a = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=42)
    b = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=42)
    assert a == b


def test_different_seed_differs():
    a = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=42)
    b = sample_cohort(_pool(), top_slice=5_000, random_slice=5_000, seed=43)
    assert a != b
    # Only the random slice re-draws; the top slice is deterministic.
    assert a[:5_000] == b[:5_000]


def test_sampling_is_isolated_from_global_rng():
    """Seeding the global RNG must not shift the sample."""
    import random

    random.seed(1)
    a = sample_cohort(_pool(1_000), top_slice=100, random_slice=100, seed=7)
    random.seed(999)
    b = sample_cohort(_pool(1_000), top_slice=100, random_slice=100, seed=7)
    assert a == b


def test_random_slice_clamps_when_pool_smaller_than_request():
    """A short pool must not raise out of random.sample."""
    result = sample_cohort(_pool(100), top_slice=10, random_slice=5_000, seed=1)
    assert len(result) == 100
    assert sum(1 for _, _, is_top in result if is_top) == 10


def test_empty_pool():
    assert sample_cohort([], top_slice=5_000, random_slice=5_000, seed=1) == []


# ---------------------------------------------------------------------------
# Standings pagination
# ---------------------------------------------------------------------------


def _page(page_no: int, *, last_page: int | None = None) -> dict:
    """One 50-entry standings page.

    ``last_page`` models a league that runs out: pages beyond it are empty and
    the page itself reports ``has_next: False``.
    """
    base = (page_no - 1) * 50
    empty = last_page is not None and page_no > last_page
    return {
        "standings": {
            "has_next": last_page is None or page_no < last_page,
            "page": page_no,
            "results": []
            if empty
            else [
                {"entry": 1_000_000 + base + i, "rank": base + i, "total": 3_000 - base - i}
                for i in range(1, 51)
            ],
        }
    }


def _paginating_client(last_page: int | None = None) -> AsyncMock:
    """Returns a real page per requested path, honouring page_standings=N."""

    async def _get_many(paths: list[str]) -> list[dict]:
        pages = []
        for path in paths:
            match = re.search(r"page_standings=(\d+)", path)
            assert match, f"path missing page param: {path}"
            pages.append(_page(int(match.group(1)), last_page=last_page))
        return pages

    client = AsyncMock()
    client.get_many = AsyncMock(side_effect=_get_many)
    return client


def _requested_paths(client: AsyncMock) -> list[str]:
    """Flatten every path across all (chunked) get_many calls."""
    return [path for call in client.get_many.await_args_list for path in call.args[0]]


@pytest.mark.parametrize(
    "max_rank,expected_pages",
    [(25_000, 500), (50_000, 1_000), (100_000, 2_000)],
)
@pytest.mark.asyncio
async def test_page_count_for_pool_size(max_rank: int, expected_pages: int):
    client = _paginating_client()
    entries = await scrape_current_standings(client, gw=10, max_rank=max_rank)

    paths = _requested_paths(client)
    assert len(paths) == expected_pages
    # Contiguous 1..N, no gaps and no duplicates from the chunking.
    page_numbers = sorted(int(re.search(r"page_standings=(\d+)", p).group(1)) for p in paths)
    assert page_numbers == list(range(1, expected_pages + 1))
    assert len(entries) == expected_pages * 50


@pytest.mark.asyncio
async def test_scrape_targets_the_overall_league():
    client = _paginating_client()
    await scrape_current_standings(client, gw=10, max_rank=500)
    assert all("leagues-classic/314/standings/" in path for path in _requested_paths(client))


@pytest.mark.asyncio
async def test_scrape_is_chunked_not_one_gather():
    """2,000 pages must not be issued as a single get_many call."""
    client = _paginating_client()
    await scrape_current_standings(client, gw=10, max_rank=100_000)
    assert client.get_many.await_count > 1


@pytest.mark.asyncio
async def test_scrape_tolerates_empty_league():
    """Pre-season the overall league is empty and page 1 says has_next: False."""
    client = _paginating_client(last_page=0)
    assert await scrape_current_standings(client, gw=10, max_rank=500) == []


@pytest.mark.asyncio
async def test_scrape_stops_early_at_end_of_league():
    """A league shorter than max_rank must not cost the full page count.

    Requesting a 100k pool (2,000 pages) against a league that ends at page 30
    should stop after the first chunk, not fire 1,970 pointless requests at a
    Cloudflare-fronted API.
    """
    client = _paginating_client(last_page=30)
    entries = await scrape_current_standings(client, gw=10, max_rank=100_000)

    assert len(entries) == 30 * 50
    assert len(_requested_paths(client)) == 100  # One chunk, not 2,000.
    assert client.get_many.await_count == 1


# ---------------------------------------------------------------------------
# ingest_cohort orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_gw_writes_nothing_and_makes_no_requests(tmp_path: Path):
    client = _paginating_client()
    with _storage(tmp_path) as s:
        assert await ingest_cohort(client, s, gw=1) == []
        assert _scalar(s, "SELECT count(*) FROM cohort_manager") == 0
    client.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_persists_sample_with_total_points(tmp_path: Path):
    """total_points survives the sample/re-join and is never null."""
    client = _paginating_client()
    with _storage(tmp_path) as s:
        await ingest_cohort(client, s, gw=10, seed=1)
        rows = _scalar(s, "SELECT count(*) FROM cohort_manager WHERE gameweek = 10")
        nulls = _scalar(s, "SELECT count(*) FROM cohort_manager WHERE total_points IS NULL")
        distinct_totals = _scalar(s, "SELECT count(DISTINCT total_points) FROM cohort_manager")
    assert rows == 10_000
    assert nulls == 0
    assert distinct_totals > 1  # Real values, not a constant placeholder.


@pytest.mark.asyncio
async def test_uses_strategy_defaults_for_gw(tmp_path: Path):
    """GW 7 -> gw_5_9: 50k pool (1,000 pages), 5k top + 5k random."""
    client = _paginating_client()
    with _storage(tmp_path) as s:
        await ingest_cohort(client, s, gw=7, seed=1)
        top = _scalar(s, "SELECT count(*) FROM cohort_manager WHERE is_top_slice")
        rest = _scalar(s, "SELECT count(*) FROM cohort_manager WHERE NOT is_top_slice")
    assert len(_requested_paths(client)) == 1_000
    assert top == 5_000
    assert rest == 5_000


@pytest.mark.asyncio
async def test_gw_2_4_has_no_top_slice(tmp_path: Path):
    """Early-season rank is luck, so the sample is purely random over 100k."""
    client = _paginating_client()
    with _storage(tmp_path) as s:
        await ingest_cohort(client, s, gw=3, seed=1)
        top = _scalar(s, "SELECT count(*) FROM cohort_manager WHERE is_top_slice")
        total = _scalar(s, "SELECT count(*) FROM cohort_manager")
    assert len(_requested_paths(client)) == 2_000
    assert top == 0
    assert total == 10_000


@pytest.mark.asyncio
async def test_rerun_reproduces_identical_sample(tmp_path: Path):
    """Crash recovery: the same GW re-ingests to the same cohort."""
    with _storage(tmp_path) as s:
        first = await ingest_cohort(_paginating_client(), s, gw=10)
        after_first = _scalar(s, "SELECT count(*) FROM cohort_manager")
        second = await ingest_cohort(_paginating_client(), s, gw=10)
        after_second = _scalar(s, "SELECT count(*) FROM cohort_manager")
    assert first == second
    assert after_first == after_second == 10_000
