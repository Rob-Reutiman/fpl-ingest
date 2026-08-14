"""Checks that run before anything is uploaded.

The job's failure mode to avoid is not "crashes" — it's "writes plausible-looking
data that is quietly wrong grain". These checks are the guard, so they raise rather
than log, and they run against the built tables and then against the staged Parquet
before a single object reaches R2.

Warnings are for things worth a human's attention that don't mean the data is wrong
(the archive changing its manager row count, xGC diverging from summed opponent xG).
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

# Seasons before this had no defcon stat at all; it must be NULL, never 0.
DEFCON_FIRST_SEASON = "2025-26"

# Rows the archive is known to carry for assistant managers, by season. A mismatch
# is a warning, not a failure: it means upstream edited the archive, which is worth
# noticing but doesn't make our output wrong.
EXPECTED_MANAGER_ROWS = {"2024-25": 322}


class BackfillValidationError(RuntimeError):
    """Raised when a check fails. Nothing is uploaded."""


@dataclass
class CheckResults:
    warnings: list[str] = field(default_factory=list)
    notes: dict[str, object] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)

    def note(self, name: str, value: object, *, season: str | None = None) -> None:
        """Record a check's result. Season-scoped notes are kept per season —
        a bare name would leave only the last season's value in the report."""
        self.notes[f"{season} {name}" if season else name] = value


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    """Run a scalar-returning count query. Absent or NULL reads as zero."""
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def validate_season(con: duckdb.DuckDBPyConnection, season: str, results: CheckResults) -> None:
    """Per-season checks against the in-memory tables for that season."""
    failures: list[str] = []

    rows = _count(con, "SELECT count(*) FROM fact_player_fixture")
    if not MIN_FACT_ROWS <= rows <= MAX_FACT_ROWS:
        failures.append(
            f"fact_player_fixture has {rows:,} rows, outside the plausible "
            f"{MIN_FACT_ROWS:,}-{MAX_FACT_ROWS:,} range"
        )

    # A conflicting duplicate is two different readings of one appearance. Exact
    # duplicates were already collapsed by DISTINCT; anything left is real trouble.
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

    # Double gameweeks must survive as two rows. If a season's source collapsed them
    # into a gameweek-level aggregate, every rolling window downstream is wrong.
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

    # A blank gameweek must produce no row at all. A zero-minute row for a blank
    # would be counted as a genuine non-appearance by every rolling window.
    blanks = _count(
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
    if blanks:
        failures.append(f"{blanks} fact rows exist for a team with no fixture that gameweek")

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

    # Price/ownership is gameweek-level, so both rows of a double gameweek must agree
    # before DISTINCT collapses them.
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

    defcon_nulls = _count(
        con, "SELECT count(*) FROM fact_player_fixture WHERE defensive_contribution IS NOT NULL"
    )
    if season < DEFCON_FIRST_SEASON and defcon_nulls:
        failures.append(
            f"{season} predates defensive_contribution but {defcon_nulls} rows are non-NULL; "
            "a missing stat must never be coerced to 0"
        )
    if season >= DEFCON_FIRST_SEASON and not defcon_nulls:
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
    """Sum a top scorer's fixtures and compare to the archive's season total.

    An end-to-end check that the joins didn't drop or duplicate appearances: this
    catches a fanned-out join that every key-uniqueness check would pass.
    """
    # cleaned_players.csv has no element id, so the join is on name; its own
    # `element_type` is a label rather than the numeric code, hence the filter on
    # players_raw instead.
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
    """Cross-check the reported xGC against the opponent's summed xG."""
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
    """Cross-season checks against the staged Parquet, not the in-memory tables.

    The union guarantee is a property of the *files*, so it has to be checked on
    the files: a per-season table can look right and still write a column as a
    different physical type.
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

            # The point of the identical column set: a multi-season glob must union.
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
        # (season, element_id) is the primary key; (season, player_master_id) must
        # also be unique, or two of a season's players share one career.
        for keys in (("season", "element_id"), ("season", "player_master_id")):
            dupes = _count(
                con,
                f"SELECT count(*) FROM (SELECT {', '.join(keys)} "
                f"FROM read_parquet('{players}') GROUP BY ALL HAVING count(*) > 1)",
            )
            if dupes:
                failures.append(f"map_player_season has {dupes} duplicate {keys} keys")

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
