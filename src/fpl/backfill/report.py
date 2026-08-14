"""Renders `backfill_report.md`, describing what the run loaded."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class SeasonStats:
    season: str
    table_rows: dict[str, int] = field(default_factory=dict)
    null_filled_columns: list[str] = field(default_factory=list)
    manager_rows_dropped: int = 0
    duplicate_rows_collapsed: int = 0
    players: int = 0
    new_master_ids: int = 0


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def render(
    seasons: Sequence[SeasonStats],
    *,
    master_totals: Mapping[str, int],
    review_by_method: Mapping[str, int],
    warnings: Sequence[str],
    notes: Mapping[str, object],
) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    table_names = sorted({name for season in seasons for name in season.table_rows})

    parts = [
        "# FPL historical backfill report",
        "",
        f"Generated {generated}. Source: "
        "[vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League).",
        "",
        "## Rows per table per season",
        "",
        _table(
            ["season", *table_names],
            [
                [stats.season, *(f"{stats.table_rows.get(name, 0):,}" for name in table_names)]
                for stats in seasons
            ],
        ),
        "",
        "## Source handling per season",
        "",
        _table(
            [
                "season",
                "players",
                "new master ids",
                "columns NULL-filled",
                "manager rows dropped",
                "exact duplicates collapsed",
            ],
            [
                [
                    stats.season,
                    stats.players,
                    stats.new_master_ids,
                    ", ".join(stats.null_filled_columns) or "—",
                    stats.manager_rows_dropped,
                    stats.duplicate_rows_collapsed,
                ]
                for stats in seasons
            ],
        ),
        "",
        "A NULL-filled column is one the spec defines but that season's source predates. It is "
        "written NULL, never 0 — null means the stat wasn't measured, zero means it was measured "
        "and was zero.",
        "",
        "## Cross-season identity",
        "",
        _table(
            ["metric", "value"],
            [[name, f"{value:,}"] for name, value in sorted(master_totals.items())],
        ),
        "",
        "Matches not made on the stable `player_code` are recorded in `player_match_review.csv` "
        "whether or not the job accepted them:",
        "",
        _table(
            ["match method", "rows"],
            [[method, count] for method, count in sorted(review_by_method.items())] or [["—", 0]],
        ),
        "",
        "## Validation",
        "",
    ]

    if notes:
        parts += [
            _table(["check", "result"], [[name, value] for name, value in sorted(notes.items())]),
            "",
        ]

    if warnings:
        parts += ["### Warnings", ""] + [f"- {warning}" for warning in warnings] + [""]
    else:
        parts += ["No warnings. All checks passed.", ""]

    parts += [
        "## Known exclusions",
        "",
        "- **`xP`** is not loaded. It is scraped from FPL's `ep_this` *after* a gameweek ends, so "
        "it may reflect post-match information rather than the pre-deadline prediction managers "
        "saw — a target-leakage risk in any ML feature set.",
        "- **Assistant managers** (2024-25's `AM` asset, `element_type` 5) are dropped. They are "
        "not players, the schema defines positions 1-4 only, and FPL retired the asset.",
        "- **`clearances_blocks_interceptions`, `recoveries`, `tackles`** (2025-26 only) are not "
        "in the schema spec and are dropped rather than invented into it.",
        "- **`dim_gameweek`** carries NULL for `deadline_time`, `deadline_time_epoch`, "
        "`average_entry_score`, `highest_score` and the `most_*` columns: the archive has no "
        "events file and these are unobtainable after the fact.",
        "",
    ]
    return "\n".join(parts)
