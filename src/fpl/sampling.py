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
    """Deterministic per-gameweek RNG, so a re-run repeats the same draw."""
    return random.Random(f"{season}:{gw}")


def select_pages(rng: random.Random) -> list[int]:
    """Pick the standings pages to sample from the 1,001–10,000 rank range."""
    population = range(SAMPLE_PAGE_START, SAMPLE_PAGE_END + 1)
    return sorted(rng.sample(population, SAMPLE_PAGE_COUNT))


def select_entries(entries: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    """Pick a subset of one page's entries, preserving standings order."""
    count = min(ENTRIES_PER_SAMPLED_PAGE, len(entries))
    chosen = rng.sample(range(len(entries)), count)
    return [entries[i] for i in sorted(chosen)]
