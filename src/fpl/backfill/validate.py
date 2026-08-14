"""Checks that run before anything is uploaded.

The failure worth guarding against is plausible data at quietly the wrong grain,
so these raise. They run first against the built tables, then against the staged
files, and every one passes before an object reaches R2.

A warning marks something worth a human's attention that leaves the output good.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from fpl import curated_schema
from fpl.transforms.team_fixture import (
    XGC_DIVERGENCE_THRESHOLD,
    warn_on_xgc_divergence,
)

logger = logging.getLogger(__name__)

MIN_FACT_ROWS = 25_000
MAX_FACT_ROWS = 30_000

# FPL introduced `defensive_contribution` in this season. Earlier ones hold NULL.
DEFCON_FIRST_SEASON = "2025-26"

# Assistant manager rows the archive carries, by season. A mismatch warns,
# since it means upstream has edited the archive.
EXPECTED_MANAGER_ROWS = {"2024-25": 322}


class BackfillValidationError(RuntimeError):
    """Raised by a failing check, ending the run ahead of the upload."""


@dataclass
class CheckResults:
    warnings: list[str] = field(default_factory=list)
    notes: dict[str, object] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)

    def note(self, name: str, value: object, *, season: str | None = None) -> None:
        """Record a check's result for the report.

        Pass `season` for anything measured per season, or later seasons overwrite
        earlier ones and the report shows only the last.
        """
        self.notes[f"{season} {name}" if season else name] = value


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    """Run a count query. Absent or NULL reads as zero."""
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def validate_season(con: duckdb.DuckDBPyConnection, season: str, results: CheckResults) -> None:
    """Check one season against the tables built in memory for it."""
    failures: list[str] = []

    rows = _count(con, "SELECT count(*) FROM fact_player_fixture")
    if not MIN_FACT_ROWS <= rows <= MAX_FACT_ROWS:
        failures.append(
            f"fact_player_fixture has {rows:,} rows, outside the plausible "
            f"{MIN_FACT_ROWS:,}-{MAX_FACT_ROWS:,} range"
        )

    # Duplicates identical to the byte have already collapsed. A key still held
    # twice carries two readings of one appearance.
    conflicting = _count(
        con,
        "SELECT count(*) FROM (SELECT element_id, fixture_id FROM fact_source "
        "GROUP BY element_id, fixture_id HAVING count(*) > 1)",
    )
    if conflicting:
        sample = con.execute(
            "SELECT element_id, fixture_id, count(*) FROM fact_source "
            "GROUP BY element_id, fixture_id HAVING count(*) > 1 LIMIT 5"
        ).fetchall()
        failures.append(
            f"{conflicting} (element_id, fixture_id) keys have conflicting source rows, "
            f"e.g. {sample}"
        )

    duplicate_keys = _count(
        con,
        "SELECT count(*) FROM (SELECT element_id, fixture_id FROM fact_player_fixture "
        "GROUP BY element_id, fixture_id HAVING count(*) > 1)",
    )
    if duplicate_keys:
        failures.append(f"{duplicate_keys} duplicate (element_id, fixture_id) keys in the fact")

    mismatched_round = _count(
        con, "SELECT count(*) FROM fact_source WHERE source_round IS DISTINCT FROM gameweek"
    )
    if mismatched_round:
        failures.append(
            f"{mismatched_round} rows where merged_gw's `round` disagrees with `GW`; "
            "the gameweek attribution can't be trusted"
        )

    unmapped = _count(
        con,
        "SELECT count(*) FROM (SELECT element_id FROM fact_player_fixture "
        "GROUP BY element_id HAVING count(DISTINCT player_master_id) <> 1)",
    )
    if unmapped:
        failures.append(f"{unmapped} element_ids resolve to more than one player_master_id")

    multi_team = _count(
        con,
        "SELECT count(*) FROM (SELECT team_id FROM fact_player_fixture "
        "GROUP BY team_id HAVING count(DISTINCT team_master_id) <> 1)",
    )
    if multi_team:
        failures.append(f"{multi_team} team_ids resolve to more than one team_master_id")

    # Double gameweeks survive as two rows. A season whose source summed them
    # into one would read wrong in every rolling window downstream.
    doubles = _count(
        con,
        "SELECT count(*) FROM (SELECT element_id, gameweek FROM fact_player_fixture "
        "GROUP BY element_id, gameweek HAVING count(DISTINCT fixture_id) > 1)",
    )
    if not doubles:
        failures.append(
            "no (element_id, gameweek) has two fixtures — the source appears to have "
            "collapsed double gameweeks to one row per gameweek"
        )
    results.note("double gameweek player-rows", doubles, season=season)

    # A blank gameweek produces no row. A row carrying 0 minutes would read as
    # a player who was available and went unused.
    blank_rows = _count(
        con,
        """
        SELECT count(*) FROM fact_player_fixture p
        WHERE NOT EXISTS (
            SELECT 1 FROM dim_fixture f
            WHERE f.gameweek = p.gameweek
              AND (f.home_team_id = p.team_id OR f.away_team_id = p.team_id)
        )
        """,
    )
    if blank_rows:
        failures.append(f"{blank_rows} fact rows exist for a team with no fixture that gameweek")

    orphans = _count(
        con,
        """
        SELECT count(*) FROM fact_player_fixture p
        LEFT JOIN dim_fixture f ON p.fixture_id = f.fixture_id
        WHERE f.fixture_id IS NULL
           OR f.gameweek IS DISTINCT FROM p.gameweek
           OR p.team_id NOT IN (f.home_team_id, f.away_team_id)
        """,
    )
    if orphans:
        failures.append(f"{orphans} fact rows don't reconcile with their dim_fixture row")

    sides = _count(
        con,
        "SELECT count(*) FROM (SELECT fixture_id FROM fact_team_fixture "
        "GROUP BY fixture_id HAVING count(*) <> 2)",
    )
    if sides:
        failures.append(f"{sides} fixtures in fact_team_fixture don't have exactly two rows")

    # Price and ownership sit at gameweek grain, so both rows of a double
    # gameweek agree before DISTINCT collapses them.
    inconsistent = _count(
        con,
        "SELECT count(*) FROM (SELECT element_id, gameweek FROM fact_player_gameweek_fpl "
        "GROUP BY element_id, gameweek HAVING count(*) > 1)",
    )
    if inconsistent:
        failures.append(
            f"{inconsistent} (element_id, gameweek) keys carry conflicting price/ownership "
            "across their fixture rows"
        )

    defcon_rows = _count(
        con, "SELECT count(*) FROM fact_player_fixture WHERE defensive_contribution IS NOT NULL"
    )
    if season < DEFCON_FIRST_SEASON and defcon_rows:
        failures.append(
            f"{season} predates defensive_contribution but {defcon_rows} rows are non-NULL; "
            "a missing stat must never be coerced to 0"
        )
    if season >= DEFCON_FIRST_SEASON and not defcon_rows:
        failures.append(f"{season} should carry defensive_contribution but every row is NULL")

    _check_season_totals(con, season, failures, results)
    _check_xgc(con, season, results)
    _check_manager_rows(con, season, results)

    if failures:
        raise BackfillValidationError(
            f"{season} failed validation:\n  - " + "\n  - ".join(failures)
        )


def _check_season_totals(
    con: duckdb.DuckDBPyConnection, season: str, failures: list[str], results: CheckResults
) -> None:
    """Sum a top scorer's fixtures and compare against the reported season total.

    Proof end to end that the joins preserved every appearance exactly once. A
    join that fanned out passes every check on key uniqueness and fails this one.

    The join runs on name, since `cleaned_players` carries no element id, and the
    position filter runs against `players_raw`, which holds a numeric code.
    """
    row = con.execute(
        """
        SELECT c.first_name || ' ' || c.second_name,
               CAST(c.total_points AS INTEGER),
               (SELECT sum(f.total_points)
                  FROM fact_player_fixture f
                 WHERE f.element_id = CAST(p.id AS INTEGER))
        FROM src_cleaned_players c
        JOIN src_players_raw p
          ON p.first_name = c.first_name AND p.second_name = c.second_name
        WHERE CAST(p.element_type AS INTEGER) IN (1, 2, 3, 4)
        ORDER BY CAST(c.total_points AS INTEGER) DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        failures.append("could not identify a top scorer to spot-check season totals against")
        return

    name, expected, actual = row
    results.note(
        "top scorer total points", f"{name}: {actual} vs {expected} reported", season=season
    )
    if actual is None or int(actual) != int(expected):
        failures.append(
            f"season total mismatch for {name}: cleaned_players says {expected}, "
            f"summed fixtures give {actual}"
        )


def _check_xgc(con: duckdb.DuckDBPyConnection, season: str, results: CheckResults) -> None:
    """Compare the reported xGC against the opponent's summed xG."""
    diverging = warn_on_xgc_divergence(con, season)
    results.note("team-fixtures with divergent xGC", diverging, season=season)
    if diverging:
        results.warnings.append(
            f"{season}: {diverging} team-fixtures where reported xGC and opponent xG differ by "
            f"more than {XGC_DIVERGENCE_THRESHOLD}"
        )


def _check_manager_rows(con: duckdb.DuckDBPyConnection, season: str, results: CheckResults) -> None:
    excluded = ", ".join(f"'{p}'" for p in sorted(curated_schema.EXCLUDED_POSITIONS))
    actual = _count(con, f"SELECT count(*) FROM src_merged_gw WHERE position IN ({excluded})")
    expected = EXPECTED_MANAGER_ROWS.get(season, 0)
    if actual != expected:
        results.warn(
            f"{season}: dropped {actual} assistant-manager rows, expected {expected} — "
            "the archive changed, check the exclusion still makes sense"
        )


def validate_output(staging_dir: Path, seasons: Sequence[str], results: CheckResults) -> None:
    """Check the staged files, across every season, once they have been written.

    The union guarantee is a property of the files themselves. A table can look
    right in memory and still write a column out as a different physical type.
    """
    failures: list[str] = []
    con = duckdb.connect(":memory:")
    try:
        for table in curated_schema.SEASON_TABLES:
            expected = curated_schema.COLUMNS[table]
            for season in seasons:
                path = staging_dir / season / f"{table}.parquet"
                if not path.exists():
                    failures.append(f"{season}/{table}.parquet was not written")
                    continue
                described = con.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')"
                ).fetchall()
                actual = tuple((row[0], row[1]) for row in described)
                if actual != expected:
                    failures.append(
                        f"{season}/{table}.parquet schema drifted from the contract:\n"
                        f"      expected {expected}\n      got      {actual}"
                    )

            # What the identical column set buys. The glob unions.
            glob = (staging_dir / "*" / f"{table}.parquet").as_posix()
            try:
                con.execute(f"SELECT count(*) FROM read_parquet('{glob}')").fetchone()
            except duckdb.Error as exc:
                failures.append(f"multi-season glob of {table} does not union: {exc}")

        failures.extend(_validate_master(con, staging_dir))

        if failures:
            raise BackfillValidationError(
                "staged output failed validation:\n  - " + "\n  - ".join(failures)
            )
        results.note("seasons whose output unions cleanly", ", ".join(seasons))
    finally:
        con.close()


def _validate_master(con: duckdb.DuckDBPyConnection, staging_dir: Path) -> list[str]:
    failures: list[str] = []
    master_dir = staging_dir / "master"

    for table, expected in (
        (name, curated_schema.COLUMNS[name]) for name in curated_schema.MASTER_TABLES
    ):
        path = master_dir / f"{table}.parquet"
        if not path.exists():
            failures.append(f"master/{table}.parquet was not written")
            continue
        described = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')"
        ).fetchall()
        actual = tuple((row[0], row[1]) for row in described)
        if actual != expected:
            failures.append(f"master/{table}.parquet schema drifted: got {actual}")

    players = (master_dir / "map_player_season.parquet").as_posix()
    if (master_dir / "map_player_season.parquet").exists():
        # (season, element_id) is the primary key. (season, player_master_id) must
        # be unique too, or two of a season's players share one career.
        for key_columns in (("season", "element_id"), ("season", "player_master_id")):
            dupes = _count(
                con,
                f"SELECT count(*) FROM (SELECT {', '.join(key_columns)} "
                f"FROM read_parquet('{players}') GROUP BY ALL HAVING count(*) > 1)",
            )
            if dupes:
                failures.append(f"map_player_season has {dupes} duplicate {key_columns} keys")

    teams = (master_dir / "map_team_season.parquet").as_posix()
    if (master_dir / "map_team_season.parquet").exists():
        dupes = _count(
            con,
            "SELECT count(*) FROM (SELECT season, team_id "
            f"FROM read_parquet('{teams}') GROUP BY ALL HAVING count(*) > 1)",
        )
        if dupes:
            failures.append(f"map_team_season has {dupes} duplicate (season, team_id) keys")

    return failures
