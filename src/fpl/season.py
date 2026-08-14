"""Derive the season identifier from a bootstrap response."""

from __future__ import annotations

import re
from typing import Any

# Matches the season in a static asset URL, as in ".../static/2026_27/".
_SEASON_RE = re.compile(r"/(\d{4})_(\d{2})(?:/|$)")

_STATIC_URL_PATH = ("game_config", "settings", "static_content_url")


def derive_season(bootstrap: dict[str, Any]) -> str:
    """Return the season identifier for this bootstrap.

    Deriving the season makes rollover free. The jobs start writing under a new
    prefix the day FPL flips over. The format matches the archive's directory
    naming, so live and historical seasons glob together.
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
