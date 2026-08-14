"""Derive the season identifier from a bootstrap-static response."""

from __future__ import annotations

import re
from typing import Any

# e.g. "https://fantasy.premierleague.com/img/static/2026_27/" -> ("2026", "27")
_SEASON_RE = re.compile(r"/(\d{4})_(\d{2})(?:/|$)")

_STATIC_URL_PATH = ("game_config", "settings", "static_content_url")


def derive_season(bootstrap: dict[str, Any]) -> str:
    """Return the season string (e.g. ``"2026-27"``) for this bootstrap.

    Deriving rather than hardcoding is what makes season rollover a non-event:
    the jobs simply start writing under a new prefix the day FPL flips over.

    The `YYYY-YY` form is the schema contract's, and matches the archive repo's
    directory naming — so a season ingested live and a season loaded by the
    backfill land under identical prefixes and glob together.
    """
    node: Any = bootstrap
    for key in _STATIC_URL_PATH:
        if not isinstance(node, dict) or key not in node:
            raise ValueError(
                f"bootstrap-static is missing {'.'.join(_STATIC_URL_PATH)}; "
                "cannot derive the season prefix"
            )
        node = node[key]

    if not isinstance(node, str):
        raise ValueError(f"{'.'.join(_STATIC_URL_PATH)} is not a string: {node!r}")

    match = _SEASON_RE.search(node)
    if match is None:
        raise ValueError(f"cannot parse a season out of static_content_url: {node!r}")

    start_year, end_year = match.groups()
    return f"{start_year}-{end_year}"
