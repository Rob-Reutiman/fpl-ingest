"""The 1,001–10,000 rank sample: right size, no duplicates, well spread."""

from __future__ import annotations

from fpl.constants import (
    ENTRIES_PER_SAMPLED_PAGE,
    SAMPLE_PAGE_COUNT,
    SAMPLE_PAGE_END,
    SAMPLE_PAGE_START,
)
from fpl.sampling import seeded_rng, select_entries, select_pages

from .conftest import make_standings_page

RNG = lambda: seeded_rng("2026-27", 7)  # noqa: E731


def test_selects_the_configured_number_of_distinct_pages():
    pages = select_pages(RNG())
    assert len(pages) == SAMPLE_PAGE_COUNT
    assert len(set(pages)) == SAMPLE_PAGE_COUNT


def test_pages_stay_inside_the_target_rank_range():
    pages = select_pages(RNG())
    assert min(pages) >= SAMPLE_PAGE_START
    assert max(pages) <= SAMPLE_PAGE_END


def test_pages_are_spread_across_the_range_not_clustered():
    """40 of 180 pages: every quarter of the range should be represented."""
    pages = select_pages(RNG())
    width = SAMPLE_PAGE_END - SAMPLE_PAGE_START + 1
    quarters = {(p - SAMPLE_PAGE_START) * 4 // width for p in pages}
    assert quarters == {0, 1, 2, 3}


def test_selects_a_subset_of_each_page_in_standings_order():
    results = make_standings_page(50)["standings"]["results"]
    chosen = select_entries(results, RNG())

    assert len(chosen) == ENTRIES_PER_SAMPLED_PAGE
    assert len({e["entry"] for e in chosen}) == ENTRIES_PER_SAMPLED_PAGE
    assert all(e in results for e in chosen)
    assert [e["rank"] for e in chosen] == sorted(e["rank"] for e in chosen)


def test_short_page_is_taken_whole_rather_than_erroring():
    results = make_standings_page(200)["standings"]["results"][:10]
    assert len(select_entries(results, RNG())) == 10


def test_full_sample_has_no_duplicate_entries():
    rng = RNG()
    entries = [
        e["entry"]
        for page in select_pages(rng)
        for e in select_entries(make_standings_page(page)["standings"]["results"], rng)
    ]
    assert len(entries) == SAMPLE_PAGE_COUNT * ENTRIES_PER_SAMPLED_PAGE
    assert len(set(entries)) == len(entries)


def test_the_sample_never_overlaps_the_top_1000():
    """Entry ids encode rank in the fixtures, so this checks the strata are disjoint."""
    rng = RNG()
    ranks = [
        e["rank"]
        for page in select_pages(rng)
        for e in select_entries(make_standings_page(page)["standings"]["results"], rng)
    ]
    assert min(ranks) > 1000
    assert max(ranks) <= 10_000


def test_the_same_seed_reproduces_the_same_draw():
    assert select_pages(seeded_rng("2026-27", 7)) == select_pages(seeded_rng("2026-27", 7))


def test_different_gameweeks_draw_differently():
    assert select_pages(seeded_rng("2026-27", 7)) != select_pages(seeded_rng("2026-27", 8))
