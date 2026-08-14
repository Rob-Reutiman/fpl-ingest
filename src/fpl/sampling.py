"""Choosing which standings pages and entries make up the manager sample."""

from __future__ import annotations

import random
from typing import Any

from fpl.constants import (
    ENTRIES_PER_SAMPLED_PAGE,
    SAMPLE_PAGE_COUNT,
    SAMPLE_PAGE_END,
    SAMPLE_PAGE_START,
)


def seeded_rng(season: str, gw: int) -> random.Random:
    """Return a generator seeded on the season and gameweek, so a rerun repeats it."""
    return random.Random(f"{season}:{gw}")


def select_pages(rng: random.Random) -> list[int]:
    """Choose the standings pages covering ranks 1,001 to 10,000."""
    population = range(SAMPLE_PAGE_START, SAMPLE_PAGE_END + 1)
    return sorted(rng.sample(population, SAMPLE_PAGE_COUNT))


def select_entries(entries: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    """Choose a subset of one page's entries, keeping standings order."""
    count = min(ENTRIES_PER_SAMPLED_PAGE, len(entries))
    chosen = rng.sample(range(len(entries)), count)
    return [entries[i] for i in sorted(chosen)]
