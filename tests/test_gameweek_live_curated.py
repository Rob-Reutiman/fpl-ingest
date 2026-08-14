"""Job 2's curated output: grain rules, team pinning, double gameweeks.

`event/{gw}/live/` hands back every player in the game regardless of whether their
club played, so almost everything here is about what the transform *refuses* to
write.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from fpl import curated_schema, keys
from fpl.jobs import gameweek_live

from .conftest import (
    FakeAPI,
    FakeStore,
    make_bootstrap,
    make_element,
    make_event,
    make_fixture,
    make_history,
    make_live,
    make_live_stats,
    read_curated,
    relation_rows,
)

SEASON = "2026-27"


def _soon() -> str:
    """A kickoff close enough that the gameweek is still settling, not postponed."""
    return (datetime.now(UTC) + timedelta(hours=2)).isoformat()


def settled(gw: int = 1) -> list[dict]:
    return [make_event(gw, finished=True, data_checked=True)]


@pytest.fixture
def api_with_gameweek(api: FakeAPI) -> FakeAPI:
    """GW1: teams 1v2 play, team 3 blanks.

    Player 1 features for the home side, player 2 is an unused sub for the away
    side, player 3 is at a club with no fixture at all.
    """
    api.bootstrap = make_bootstrap(
        settled(),
        elements=[make_element(1, team=1), make_element(2, team=2), make_element(3, team=3)],
    )
    api.fixtures = [make_fixture(101, event=1, team_h=1, team_a=2, team_h_score=2, team_a_score=1)]
    api.live = {
        1: make_live(
            {
                1: make_live_stats(minutes=90, total_points=6, expected_goals="0.80"),
                2: make_live_stats(
                    minutes=0,
                    starts=0,
                    total_points=0,
                    bps=0,
                    expected_goals="0.00",
                    expected_assists="0.00",
                    expected_goals_conceded="0.00",
                ),
                3: make_live_stats(minutes=90, total_points=5),
            }
        )
    }
    return api


def read(store: FakeStore, table: str, tmp_path) -> duckdb.DuckDBPyRelation:
    return read_curated(store, keys.curated_key(SEASON, table), tmp_path)


rows = relation_rows


class TestGrainRules:
    def test_a_player_whose_club_blanked_gets_no_row(
        self, api_with_gameweek, store, client, tmp_path
    ):
        """The most important rule in the pipeline.

        A zero-minute row for a blank would be read by every rolling window as a
        genuine non-appearance.
        """
        gameweek_live.run(client, store)
        ids = [r["element_id"] for r in rows(read(store, "fact_player_fixture", tmp_path))]
        assert 3 not in ids

    def test_a_player_who_did_not_feature_gets_a_zero_minute_row(
        self, api_with_gameweek, store, client, tmp_path
    ):
        gameweek_live.run(client, store)
        row = next(
            r for r in rows(read(store, "fact_player_fixture", tmp_path)) if r["element_id"] == 2
        )
        assert row["minutes"] == 0
        assert row["fixture_id"] == 101

    def test_the_two_cases_are_distinguishable(self, api_with_gameweek, store, client, tmp_path):
        gameweek_live.run(client, store)
        ids = sorted(r["element_id"] for r in rows(read(store, "fact_player_fixture", tmp_path)))
        assert ids == [1, 2]

    def test_source_is_recorded_as_event_live(self, api_with_gameweek, store, client, tmp_path):
        gameweek_live.run(client, store)
        assert {r["source"] for r in rows(read(store, "fact_player_fixture", tmp_path))} == {
            "event_live"
        }


class TestDoubleGameweek:
    @pytest.fixture
    def doubled(self, api: FakeAPI) -> FakeAPI:
        """Team 1 plays twice in GW1; team 2 and team 3 play once each."""
        api.bootstrap = make_bootstrap(
            settled(), elements=[make_element(1, team=1), make_element(2, team=2)]
        )
        api.fixtures = [
            make_fixture(101, event=1, team_h=1, team_a=2),
            make_fixture(102, event=1, team_h=3, team_a=1),
        ]
        api.live = {
            1: make_live(
                {
                    # Aggregated across both fixtures — this is the problem.
                    1: make_live_stats(minutes=180, total_points=9, bps=40),
                    2: make_live_stats(minutes=90, total_points=2, bps=20),
                }
            )
        }
        api.summaries = {
            1: {
                "history": [
                    make_history(
                        101,
                        gw=1,
                        opponent_team=2,
                        was_home=True,
                        minutes=90,
                        total_points=6,
                        bps=25,
                    ),
                    make_history(
                        102,
                        gw=1,
                        opponent_team=3,
                        was_home=False,
                        minutes=90,
                        total_points=3,
                        bps=15,
                    ),
                ]
            }
        }
        return api

    def test_produces_two_rows_with_distinct_fixtures(self, doubled, store, client, tmp_path):
        gameweek_live.run(client, store)
        player = [
            r for r in rows(read(store, "fact_player_fixture", tmp_path)) if r["element_id"] == 1
        ]
        assert sorted(r["fixture_id"] for r in player) == [101, 102]

    def test_the_rows_carry_per_fixture_stats_not_the_aggregate(
        self, doubled, store, client, tmp_path
    ):
        """The whole reason for the element-summary fallback."""
        gameweek_live.run(client, store)
        player = [
            r for r in rows(read(store, "fact_player_fixture", tmp_path)) if r["element_id"] == 1
        ]
        assert sorted(r["minutes"] for r in player) == [90, 90]
        assert sum(r["total_points"] for r in player) == 9

    def test_source_distinguishes_the_two_endpoints(self, doubled, store, client, tmp_path):
        gameweek_live.run(client, store)
        by_element = {
            (r["element_id"], r["fixture_id"]): r["source"]
            for r in rows(read(store, "fact_player_fixture", tmp_path))
        }
        assert by_element[(1, 101)] == "element_summary"
        assert by_element[(1, 102)] == "element_summary"
        assert by_element[(2, 101)] == "event_live"

    def test_only_affected_players_cost_an_extra_request(self, doubled, store, client):
        gameweek_live.run(client, store)
        assert doubled.count("element-summary/1/") == 1
        assert doubled.count("element-summary/2/") == 0

    def test_a_single_gameweek_makes_no_element_summary_calls(
        self, api_with_gameweek, store, client
    ):
        gameweek_live.run(client, store)
        assert api_with_gameweek.count("element-summary") == 0

    def test_the_team_is_taken_from_the_fixture_not_the_current_club(
        self, doubled, store, client, tmp_path
    ):
        """element-summary gives `was_home`, so the club comes from the fixture."""
        gameweek_live.run(client, store)
        away = next(
            r
            for r in rows(read(store, "fact_player_fixture", tmp_path))
            if (r["element_id"], r["fixture_id"]) == (1, 102)
        )
        assert away["was_home"] is False
        assert away["team_id"] == 1
        assert away["opponent_team_id"] == 3

    def test_disagreement_with_the_live_aggregate_is_reported(self, doubled, store, client, caplog):
        """A silent mismatch would mean one of the two endpoints changed shape."""
        doubled.live[1]["elements"][0]["stats"]["total_points"] = 99
        with caplog.at_level("WARNING"):
            gameweek_live.run(client, store)
        assert "disagree with the gameweek aggregate" in caplog.text


class TestTeamFixture:
    def test_is_regenerated_with_two_rows_per_fixture(
        self, api_with_gameweek, store, client, tmp_path
    ):
        gameweek_live.run(client, store)
        team_rows = rows(read(store, "fact_team_fixture", tmp_path))
        assert sorted(r["team_master_id"] for r in team_rows) == ["ARS", "AVL"]

    def test_xg_against_is_the_opponents_xg_and_scores_come_from_the_fixture(
        self, api_with_gameweek, store, client, tmp_path
    ):
        gameweek_live.run(client, store)
        home = next(
            r
            for r in rows(read(store, "fact_team_fixture", tmp_path))
            if r["team_master_id"] == "ARS"
        )
        assert (home["goals_for"], home["goals_against"]) == (2, 1)
        assert home["result"] == "W"
        assert home["xg_for"] == pytest.approx(0.8)
        # The away side's only player was an unused sub, so their xG is zero.
        assert home["xg_against"] == pytest.approx(0.0)
        assert home["clean_sheet"] is False


class TestAccumulation:
    def test_a_later_gameweek_appends_to_the_earlier_one(
        self, api_with_gameweek, store, client, tmp_path
    ):
        gameweek_live.run(client, store)

        api_with_gameweek.bootstrap["events"].append(
            make_event(2, finished=True, data_checked=True)
        )
        api_with_gameweek.fixtures.append(
            make_fixture(102, event=2, team_h=2, team_a=1, team_h_score=0, team_a_score=0)
        )
        api_with_gameweek.live[2] = make_live({1: make_live_stats(), 2: make_live_stats()})
        gameweek_live.run(client, store)

        gameweeks = sorted(
            {r["gameweek"] for r in rows(read(store, "fact_player_fixture", tmp_path))}
        )
        assert gameweeks == [1, 2]

    def test_re_ingesting_replaces_that_gameweeks_rows_rather_than_duplicating(
        self, api_with_gameweek, store, client, tmp_path
    ):
        """How a partial gameweek gets corrected once its postponement resolves."""
        gameweek_live.run(client, store)
        before = rows(read(store, "fact_player_fixture", tmp_path))

        # Drop the idempotency marker so the same gameweek is processed again.
        del store.objects[keys.gameweek_live_key(SEASON, 1)]
        api_with_gameweek.live[1]["elements"][0]["stats"]["total_points"] = 12
        gameweek_live.run(client, store)

        after = rows(read(store, "fact_player_fixture", tmp_path))
        assert len(after) == len(before)
        assert next(r for r in after if r["element_id"] == 1)["total_points"] == 12


class TestOutputContract:
    @pytest.mark.parametrize("table", ["fact_player_fixture", "fact_team_fixture"])
    def test_matches_the_column_contract(self, api_with_gameweek, store, client, tmp_path, table):
        gameweek_live.run(client, store)
        relation = read(store, table, tmp_path)
        actual = tuple((c, str(t)) for c, t in zip(relation.columns, relation.types, strict=True))
        assert actual == curated_schema.COLUMNS[table]

    def test_stores_the_gameweek_fixtures_alongside_the_live_stats(
        self, api_with_gameweek, store, client
    ):
        gameweek_live.run(client, store)
        assert keys.gameweek_fixtures_key(SEASON, 1) in store.objects
        assert keys.gameweek_live_key(SEASON, 1) in store.objects

    def test_no_gameweek_ready_writes_nothing(self, api: FakeAPI, store, client):
        """Finished but unverified, with the last match imminent rather than
        postponed: the normal bonus-points settling window, so hold off."""
        api.bootstrap = make_bootstrap([make_event(1, finished=True, data_checked=False)])
        api.fixtures = [
            make_fixture(101, event=1, finished=False, kickoff_time=_soon()),
        ]
        assert gameweek_live.run(client, store) is None
        assert store.puts == []

    def test_a_settled_gameweek_is_not_re_ingested(self, api_with_gameweek, store, client):
        gameweek_live.run(client, store)
        writes = len(store.puts)
        assert gameweek_live.run(client, store) is None
        assert len(store.puts) == writes
