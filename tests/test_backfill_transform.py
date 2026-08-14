"""Transform behaviour, run for real against DuckDB on tiny fixture seasons.

These cover the rules that are cheap to break and expensive to notice: the grain
of `fact_player_fixture`, which club a fixture is attributed to, and the difference
between "not measured" and "measured as zero".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fpl import curated_schema
from fpl.backfill import transform
from fpl.backfill.archive import SeasonSources
from fpl.identity import MasterRegistry
from fpl.transforms import parquet, team_fixture
from tests import backfill_fixtures as fx

SEASON = "2024-25"


def build(
    tmp_path: Path,
    *,
    players: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    appearances: list[dict[str, Any]],
    season: str = SEASON,
    with_defcon: bool = False,
    registry: MasterRegistry | None = None,
) -> Any:
    """Write a fixture season, run the whole per-season transform, return the con."""
    sources = fx.write_season(
        tmp_path,
        season,
        players=players,
        fixtures=fixtures,
        appearances=appearances,
        with_defcon=with_defcon,
    )
    con = parquet.connect()
    con.execute(curated_schema.create_table_ddl("dim_team", name="all_dim_team"))
    transform.load_season_sources(con, sources)
    transform.build_dim_team(con, season)
    con.execute("INSERT INTO all_dim_team SELECT * FROM dim_team")

    registry = registry or MasterRegistry()
    assigned = registry.resolve_season(transform.read_season_players(con, season))
    transform.register_master_map(con, season, assigned)

    transform.build_dim_player(con, season)
    transform.build_dim_fixture(con, season)
    transform.build_dim_gameweek(con, season)
    transform.build_fact_source(con, sources)
    transform.build_fact_player_fixture(con, season)
    transform.build_fact_player_gameweek_fpl(con, season)
    team_fixture.build_fact_team_fixture(con, season)
    return con, sources


@pytest.fixture
def simple(tmp_path: Path):
    """GW1: ARS-BRE and CHE-EVE. GW2: only ARS-BRE, so CHE and EVE blank."""
    players = [
        fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=4),
        fx.player(2, "Idle", "Sub", code=8, team_id=1),
        fx.player(3, "Cole", "Palmer", code=9, team_id=3, total_points=2),
    ]
    fixtures = [
        fx.fixture(101, 1, home=1, away=2),
        fx.fixture(102, 1, home=3, away=4),
        fx.fixture(103, 2, home=1, away=2),
    ]
    appearances = [
        fx.appearance(1, "Bukayo Saka", 101, 1, team="Arsenal", opponent=2, was_home=True),
        # Team played, this player didn't feature: a row must exist with minutes 0.
        fx.appearance(
            2,
            "Idle Sub",
            101,
            1,
            team="Arsenal",
            opponent=2,
            was_home=True,
            minutes=0,
            total_points=0,
            expected_goals=0.0,
            expected_assists=0.0,
            expected_goals_conceded=0.0,
            starts=0,
        ),
        fx.appearance(3, "Cole Palmer", 102, 1, team="Chelsea", opponent=4, was_home=True),
        fx.appearance(1, "Bukayo Saka", 103, 2, team="Arsenal", opponent=2, was_home=True),
        fx.appearance(
            2,
            "Idle Sub",
            103,
            2,
            team="Arsenal",
            opponent=2,
            was_home=True,
            minutes=0,
            total_points=0,
            expected_goals=0.0,
            expected_assists=0.0,
            expected_goals_conceded=0.0,
            starts=0,
        ),
    ]
    con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)
    return con


class TestGrain:
    def test_non_featuring_player_on_an_active_team_gets_a_zero_minute_row(self, simple):
        row = simple.execute(
            "SELECT minutes FROM fact_player_fixture WHERE element_id = 2 AND fixture_id = 101"
        ).fetchone()
        assert row == (0,)

    def test_a_blank_gameweek_produces_no_row_at_all(self, simple):
        """The single most important rule in the pipeline.

        A zero-minute row for a blank would be counted by every rolling window as a
        genuine non-appearance, quietly corrupting "minutes over the last 5".
        """
        rows = simple.execute(
            "SELECT count(*) FROM fact_player_fixture WHERE element_id = 3 AND gameweek = 2"
        ).fetchone()
        assert rows == (0,)

    def test_the_two_cases_are_distinguishable(self, simple):
        """Same gameweek: an idle Arsenal player has a row, a blank Chelsea one doesn't."""
        by_player = dict(
            simple.execute(
                "SELECT element_id, count(*) FROM fact_player_fixture "
                "WHERE gameweek = 2 GROUP BY element_id"
            ).fetchall()
        )
        assert by_player == {1: 1, 2: 1}

    def test_a_double_gameweek_produces_two_rows_with_distinct_fixtures(self, tmp_path):
        players = [fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=4)]
        fixtures = [fx.fixture(101, 1, home=1, away=2), fx.fixture(102, 1, home=3, away=1)]
        appearances = [
            fx.appearance(1, "Bukayo Saka", 101, 1, team="Arsenal", opponent=2, was_home=True),
            fx.appearance(1, "Bukayo Saka", 102, 1, team="Arsenal", opponent=3, was_home=False),
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)
        rows = con.execute(
            "SELECT fixture_id FROM fact_player_fixture WHERE element_id = 1 AND gameweek = 1 "
            "ORDER BY fixture_id"
        ).fetchall()
        assert rows == [(101,), (102,)]

    def test_exact_duplicate_source_rows_are_collapsed(self, tmp_path):
        """2025-26's archive carries byte-identical duplicate rows."""
        players = [fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=2)]
        fixtures = [fx.fixture(101, 1, home=1, away=2)]
        appearance = fx.appearance(
            1, "Bukayo Saka", 101, 1, team="Arsenal", opponent=2, was_home=True
        )
        con, _ = build(
            tmp_path, players=players, fixtures=fixtures, appearances=[appearance, dict(appearance)]
        )
        assert con.execute("SELECT count(*) FROM fact_player_fixture").fetchone() == (1,)


class TestTeamAttribution:
    def test_team_is_pinned_to_the_fixture_not_the_end_of_season_club(self, tmp_path):
        """A January transfer must not reattribute the player's earlier fixtures.

        `dim_player.team_id` is their final club; the fact table has to disagree
        with it for the first half of the season, or every team-level aggregate is
        silently wrong.
        """
        players = [fx.player(1, "Mid Season", "Mover", code=7, team_id=3, total_points=4)]
        fixtures = [fx.fixture(101, 1, home=1, away=2), fx.fixture(102, 2, home=3, away=4)]
        appearances = [
            fx.appearance(1, "Mid Season Mover", 101, 1, team="Arsenal", opponent=2, was_home=True),
            fx.appearance(1, "Mid Season Mover", 102, 2, team="Chelsea", opponent=4, was_home=True),
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)

        assert con.execute(
            "SELECT gameweek, team_master_id FROM fact_player_fixture ORDER BY gameweek"
        ).fetchall() == [(1, "ARS"), (2, "CHE")]
        # The dimension still records only their final club.
        assert con.execute("SELECT team_master_id FROM dim_player").fetchone() == ("CHE",)


class TestSchemaDrift:
    def test_defensive_contribution_is_null_not_zero_when_the_season_predates_it(self, simple):
        """Null means "not measured"; zero means "measured and was zero"."""
        nulls, zeros = simple.execute(
            "SELECT count(*) FILTER (WHERE defensive_contribution IS NULL), "
            "count(*) FILTER (WHERE defensive_contribution = 0) FROM fact_player_fixture"
        ).fetchone()
        assert (nulls, zeros) == (5, 0)

    def test_defensive_contribution_is_carried_when_the_season_has_it(self, tmp_path):
        players = [fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=2)]
        fixtures = [fx.fixture(101, 1, home=1, away=2)]
        appearances = [
            fx.appearance(
                1,
                "Bukayo Saka",
                101,
                1,
                team="Arsenal",
                opponent=2,
                was_home=True,
                defensive_contribution=12,
            )
        ]
        con, _ = build(
            tmp_path,
            players=players,
            fixtures=fixtures,
            appearances=appearances,
            season="2025-26",
            with_defcon=True,
        )
        assert con.execute("SELECT defensive_contribution FROM fact_player_fixture").fetchone() == (
            12,
        )

    def test_assistant_managers_are_excluded_from_every_table(self, tmp_path):
        players = [
            fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=2),
            fx.player(2, "Some", "Manager", code=8, team_id=1, element_type=5),
        ]
        fixtures = [fx.fixture(101, 1, home=1, away=2)]
        appearances = [
            fx.appearance(1, "Bukayo Saka", 101, 1, team="Arsenal", opponent=2, was_home=True),
            fx.appearance(
                2, "Some Manager", 101, 1, team="Arsenal", opponent=2, was_home=True, position="AM"
            ),
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)
        assert con.execute("SELECT count(*) FROM fact_player_fixture").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM dim_player").fetchone() == (1,)
        assert transform.count_excluded_rows(con) == (1, 0)

    def test_xp_is_never_loaded(self, simple):
        """`xP` is scraped post-gameweek, so it leaks the outcome into a feature."""
        columns = curated_schema.column_names("fact_player_fixture")
        assert not [c for c in columns if c.lower() in {"xp", "expected_points"}]
        described = {row[0] for row in simple.execute("DESCRIBE fact_player_fixture").fetchall()}
        assert "xP" not in described


class TestTeamFixture:
    def test_xgc_reported_uses_max_not_sum(self, tmp_path):
        """Eleven players each carrying the same team xGC: SUM gives ~11x.

        This is the one aggregation in the spec that looks wrong at a glance and
        isn't — `expected_goals_conceded` is a team figure recorded per player.
        """
        players = [
            fx.player(i, f"P{i}", f"Player{i}", code=100 + i, team_id=1) for i in range(1, 12)
        ] + [fx.player(99, "Opp", "Striker", code=999, team_id=2)]
        fixtures = [fx.fixture(101, 1, home=1, away=2)]
        appearances = [
            fx.appearance(
                i,
                f"P{i} Player{i}",
                101,
                1,
                team="Arsenal",
                opponent=2,
                was_home=True,
                expected_goals_conceded=1.5,
                expected_goals=0.1,
            )
            for i in range(1, 12)
        ] + [
            fx.appearance(
                99,
                "Opp Striker",
                101,
                1,
                team="Brentford",
                opponent=1,
                was_home=False,
                expected_goals_conceded=1.1,
                expected_goals=1.4,
            )
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)

        xgc, xga = con.execute(
            "SELECT xgc_reported, xg_against FROM fact_team_fixture WHERE team_master_id = 'ARS'"
        ).fetchone()
        assert xgc == pytest.approx(1.5)  # not 16.5
        assert xga == pytest.approx(1.4)  # the opponent's summed xG

    def test_xg_against_is_the_opponents_xg_for(self, tmp_path):
        players = [
            fx.player(1, "Home", "Player", code=1, team_id=1),
            fx.player(2, "Away", "Player", code=2, team_id=2),
        ]
        fixtures = [fx.fixture(101, 1, home=1, away=2, home_score=2, away_score=1)]
        appearances = [
            fx.appearance(
                1,
                "Home Player",
                101,
                1,
                team="Arsenal",
                opponent=2,
                was_home=True,
                expected_goals=1.25,
            ),
            fx.appearance(
                2,
                "Away Player",
                101,
                1,
                team="Brentford",
                opponent=1,
                was_home=False,
                expected_goals=0.75,
            ),
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)
        rows = con.execute(
            "SELECT team_master_id, xg_for, xg_against, goals_for, goals_against, result, "
            "clean_sheet FROM fact_team_fixture ORDER BY team_master_id"
        ).fetchall()
        assert rows == [
            ("ARS", pytest.approx(1.25), pytest.approx(0.75), 2, 1, "W", False),
            ("BRE", pytest.approx(0.75), pytest.approx(1.25), 1, 2, "L", False),
        ]

    def test_every_fixture_gets_exactly_two_rows(self, tmp_path):
        """Needs both sides present, which is what a real season always has."""
        players = [
            fx.player(1, "Home", "One", code=1, team_id=1),
            fx.player(2, "Away", "One", code=2, team_id=2),
            fx.player(3, "Home", "Two", code=3, team_id=3),
            fx.player(4, "Away", "Two", code=4, team_id=4),
        ]
        fixtures = [fx.fixture(101, 1, home=1, away=2), fx.fixture(102, 1, home=3, away=4)]
        appearances = [
            fx.appearance(1, "Home One", 101, 1, team="Arsenal", opponent=2, was_home=True),
            fx.appearance(2, "Away One", 101, 1, team="Brentford", opponent=1, was_home=False),
            fx.appearance(3, "Home Two", 102, 1, team="Chelsea", opponent=4, was_home=True),
            fx.appearance(4, "Away Two", 102, 1, team="Everton", opponent=3, was_home=False),
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)
        assert con.execute(
            "SELECT count(*) FROM (SELECT fixture_id FROM fact_team_fixture "
            "GROUP BY fixture_id HAVING count(*) <> 2)"
        ).fetchone() == (0,)


class TestGameweekTable:
    def test_price_and_ownership_collapse_to_one_row_per_gameweek(self, tmp_path):
        """A double gameweek repeats gameweek-level values on both fixture rows."""
        players = [fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=4)]
        fixtures = [fx.fixture(101, 1, home=1, away=2), fx.fixture(102, 1, home=3, away=1)]
        appearances = [
            fx.appearance(
                1, "Bukayo Saka", 101, 1, team="Arsenal", opponent=2, was_home=True, value=55
            ),
            fx.appearance(
                1, "Bukayo Saka", 102, 1, team="Arsenal", opponent=3, was_home=False, value=55
            ),
        ]
        con, _ = build(tmp_path, players=players, fixtures=fixtures, appearances=appearances)
        assert con.execute(
            "SELECT count(*), max(value) FROM fact_player_gameweek_fpl"
        ).fetchone() == (1, 55)

    def test_dim_gameweek_leaves_unobtainable_columns_null(self, simple):
        row = simple.execute(
            "SELECT name, fixture_count, finished, deadline_time, average_entry_score, "
            "most_captained FROM dim_gameweek WHERE gameweek = 1"
        ).fetchone()
        assert row == ("Gameweek 1", 2, True, None, None, None)


class TestOutputContract:
    def test_every_table_matches_the_column_contract(self, simple, tmp_path):
        for table, order_by in curated_schema.SEASON_TABLES.items():
            path = parquet.write_parquet(
                simple, table, tmp_path / "out" / f"{table}.parquet", order_by
            )
            described = simple.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')"
            ).fetchall()
            assert tuple((r[0], r[1]) for r in described) == curated_schema.COLUMNS[table]

    def test_extra_source_columns_are_dropped_not_invented_into_the_schema(self, tmp_path):
        """2025-26 adds tackles/recoveries/CBI, which the spec doesn't define."""
        players = [fx.player(1, "Bukayo", "Saka", code=7, team_id=1, total_points=2)]
        fixtures = [fx.fixture(101, 1, home=1, away=2)]
        appearance = fx.appearance(
            1, "Bukayo Saka", 101, 1, team="Arsenal", opponent=2, was_home=True
        )
        appearance["tackles"] = 4
        sources: SeasonSources = fx.write_season(
            tmp_path,
            "2025-26",
            players=players,
            fixtures=fixtures,
            appearances=[appearance],
            with_defcon=True,
        )
        # Append the undeclared column to the written header.
        raw = sources.path("merged_gw.csv").read_text().splitlines()
        sources.path("merged_gw.csv").write_text(
            raw[0] + ",tackles\n" + "\n".join(line + ",4" for line in raw[1:]) + "\n"
        )

        con = parquet.connect()
        transform.load_season_sources(con, sources)
        transform.build_dim_team(con, "2025-26")
        registry = MasterRegistry()
        transform.register_master_map(
            con, "2025-26", registry.resolve_season(transform.read_season_players(con, "2025-26"))
        )
        transform.build_fact_source(con, sources)
        transform.build_fact_player_fixture(con, "2025-26")

        described = {r[0] for r in con.execute("DESCRIBE fact_player_fixture").fetchall()}
        assert "tackles" not in described
