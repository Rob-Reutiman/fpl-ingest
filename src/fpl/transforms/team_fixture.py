"""`fact_team_fixture` — fully derived, never ingested.

The spec regenerates this from `fact_player_fixture` + `dim_fixture` on every
pipeline run, so both pipelines must derive it identically or a season loaded live
won't be comparable with one loaded from the archive. Hence one implementation.
"""

from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger(__name__)

# `expected_goals_conceded` and the opponent's summed `expected_goals` measure the
# same thing by different routes, so they should track closely. A gap this wide on
# a single fixture means either an aggregation bug or a bad source row.
XGC_DIVERGENCE_THRESHOLD = 1.0


def build_fact_team_fixture(
    con: duckdb.DuckDBPyConnection,
    season: str,
    *,
    player_fixtures: str = "fact_player_fixture",
    fixtures: str = "dim_fixture",
    table: str = "fact_team_fixture",
) -> None:
    """Two rows per fixture, one per side.

    `xgc_reported` uses MAX, never SUM. `expected_goals_conceded` is recorded per
    player and every player who played the full match carries the *same* value, so
    summing it gives roughly 10x the truth (~11x measured on real fixtures). The
    primary xGA measure is the opponent's summed `expected_goals`; the reported
    figure is kept only as a cross-check.

    The sums are ordered by `element_id`. Floating-point addition isn't associative,
    so DuckDB's parallel aggregation otherwise returns last-bit-different totals from
    one run to the next — enough that a re-run rewrites hundreds of rows with
    cosmetically different values. Ordering pins the addition order without changing
    the arithmetic. The carry-along columns use `min` for the same reason: every row
    in a group holds the same value, but `any_value` doesn't promise which it returns.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        WITH side AS (
            SELECT
                fixture_id,
                team_id,
                min(team_master_id) AS team_master_id,
                min(gameweek) AS gameweek,
                min(opponent_team_id) AS opponent_team_id,
                min(opponent_team_master_id) AS opponent_team_master_id,
                min(was_home) AS was_home,
                min(kickoff_time) AS kickoff_time,
                sum(expected_goals ORDER BY element_id) AS xg_for,
                sum(expected_assists ORDER BY element_id) AS xa_for,
                max(expected_goals_conceded) AS xgc_reported
            FROM {player_fixtures}
            GROUP BY fixture_id, team_id
        )
        SELECT
            '{season}' AS season,
            s.team_id,
            s.team_master_id,
            s.fixture_id,
            s.gameweek,
            s.opponent_team_id,
            s.opponent_team_master_id,
            s.was_home,
            s.kickoff_time,
            CASE WHEN s.was_home THEN f.home_score ELSE f.away_score END AS goals_for,
            CASE WHEN s.was_home THEN f.away_score ELSE f.home_score END AS goals_against,
            s.xg_for,
            opp.xg_for AS xg_against,
            s.xgc_reported,
            s.xa_for,
            (CASE WHEN s.was_home THEN f.away_score ELSE f.home_score END) = 0 AS clean_sheet,
            CASE
                WHEN (CASE WHEN s.was_home THEN f.home_score ELSE f.away_score END)
                   > (CASE WHEN s.was_home THEN f.away_score ELSE f.home_score END) THEN 'W'
                WHEN (CASE WHEN s.was_home THEN f.home_score ELSE f.away_score END)
                   < (CASE WHEN s.was_home THEN f.away_score ELSE f.home_score END) THEN 'L'
                ELSE 'D'
            END AS result
        FROM side s
        JOIN {fixtures} f ON s.fixture_id = f.fixture_id
        LEFT JOIN side opp
               ON s.fixture_id = opp.fixture_id AND s.team_id <> opp.team_id
        """
    )


def warn_on_xgc_divergence(
    con: duckdb.DuckDBPyConnection, season: str, table: str = "fact_team_fixture"
) -> int:
    """Log where the reported xGC and the opponent's summed xG disagree materially.

    A warning, not a failure: they are two different measurements and some drift is
    expected. A large gap points at a bad source row — 2023-24's archive has a
    handful where one substitute carries a wildly different value from his
    ninety-minute team-mates.
    """
    row = con.execute(
        f"SELECT count(*) FROM {table} "
        f"WHERE abs(xgc_reported - xg_against) > {XGC_DIVERGENCE_THRESHOLD}"
    ).fetchone()
    diverging = int(row[0]) if row and row[0] is not None else 0
    if diverging:
        worst = con.execute(
            f"SELECT team_master_id, fixture_id, round(xgc_reported, 2), round(xg_against, 2) "
            f"FROM {table} ORDER BY abs(xgc_reported - xg_against) DESC LIMIT 3"
        ).fetchall()
        logger.warning(
            "%s: %d team-fixtures where reported xGC and opponent xG differ by more than %s; "
            "worst (team, fixture, xgc, xga): %s",
            season,
            diverging,
            XGC_DIVERGENCE_THRESHOLD,
            worst,
        )
    return diverging
