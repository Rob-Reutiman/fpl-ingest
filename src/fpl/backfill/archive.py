"""Fetches season CSVs from the vaastav/Fantasy-Premier-League archive.

The only I/O in the backfill besides the R2 writes. Files are cached on disk so a
re-run — or an iteration on the transform — costs nothing, and the exact bytes
fetched are copied to `raw/{season}/archive/` so a transform bug is fixable without
re-fetching from a third-party repo that may change or disappear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from fpl.constants import REQUEST_TIMEOUT_SECONDS, USER_AGENT

logger = logging.getLogger(__name__)

REPO_BASE_URL = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master"
ARCHIVE_BASE_URL = f"{REPO_BASE_URL}/data"

# Source path within a season directory -> the local/R2 filename we store it under.
# `merged_gw.csv` is nested, so it gets flattened for the provenance copy.
SOURCE_FILES: dict[str, str] = {
    "gws/merged_gw.csv": "merged_gw.csv",
    "players_raw.csv": "players_raw.csv",
    "cleaned_players.csv": "cleaned_players.csv",
    "teams.csv": "teams.csv",
    "fixtures.csv": "fixtures.csv",
    "DATA_DICTIONARY.md": "DATA_DICTIONARY.md",
}

# The data dictionary is one file at the repo root covering every season. It's
# copied into each season's provenance prefix anyway, so each is self-contained.
REPO_ROOT_FILES = frozenset({"DATA_DICTIONARY.md"})


@dataclass(frozen=True)
class SeasonSources:
    """Local paths to one season's source files, keyed by stored filename."""

    season: str
    paths: dict[str, Path]

    def path(self, filename: str) -> Path:
        return self.paths[filename]


def _url(season: str, source_path: str) -> str:
    if source_path in REPO_ROOT_FILES:
        return f"{REPO_BASE_URL}/{source_path}"
    return f"{ARCHIVE_BASE_URL}/{season}/{source_path}"


def fetch_season(
    season: str,
    cache_dir: Path,
    *,
    client: httpx.Client | None = None,
    refresh: bool = False,
) -> SeasonSources:
    """Download (or reuse cached) source files for one season."""
    season_dir = cache_dir / season
    season_dir.mkdir(parents=True, exist_ok=True)

    owned = client is None
    http = client or httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    try:
        paths: dict[str, Path] = {}
        for source_path, filename in SOURCE_FILES.items():
            destination = season_dir / filename
            if refresh or not destination.exists():
                url = _url(season, source_path)
                logger.info("fetching %s", url)
                response = http.get(url)
                response.raise_for_status()
                destination.write_bytes(response.content)
            else:
                logger.info("cached %s", destination)
            paths[filename] = destination
        return SeasonSources(season=season, paths=paths)
    finally:
        if owned:
            http.close()
