"""Derives `fact_team_fixture` from the player facts.

One implementation, shared by both pipelines, so that a season built live and a
season built from the archive stay comparable.
"""

from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger(__name__)

# Reported xGC and the opponent's summed xG measure one quantity by two routes,
# so they track closely. A gap this wide on one fixture points at an aggregation
# bug or a bad source row.
XGC_DIVERGENCE_THRESHOLD = 1.0


def build_fact_team_fixture(
    con: duckdb.DuckDBPyConnection,
    season: str,
    *,
    player_fixtures: str = "fact_player_fixture",
    fixtures: str = "dim_fixture",
    table: str = "fact_team_fixture",
) -> None:
    """Build two rows per fixture, one per side.

    `xgc_reported` takes MAX. `expected_goals_conceded` is a team figure copied
    onto every player row, so a SUM would return roughly eleven times the value.
    It serves as a cross check on `xg_against`, which sums the opponent's xG.

    The sums carry an ORDER BY and the group constants use `min`, which together
    fix the output bytes. Floating point addition is not associative, so parallel
    aggregation over an arbitrary order returns totals differing in the last bit
    from run to run, enough to rewrite hundreds of rows.
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
    """Log the team fixtures where reported xGC and opponent xG diverge.

    Returns the count. Two measurements drift a little as a matter of course, so
    this warns and continues. The 2023 season holds a handful of source rows
    where a substitute carries a wildly different value from their teammates.
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
