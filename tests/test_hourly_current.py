"""Job 1 — the hourly live snapshot and the season's dimension tables."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import duckdb
import pytest

from fpl import curated_schema, keys
from fpl.jobs import hourly_current

from .conftest import (
    FakeAPI,
    FakeStore,
    column_types,
    make_bootstrap,
    make_element,
    make_event,
    make_fixture,
    one_row,
    read_curated,
    relation_rows,
)

DAY = date(2026, 8, 15)
NOW = datetime(2026, 8, 15, 9, 5, tzinfo=UTC)
SEASON = "2026-27"


@pytest.fixture
def loaded(api: FakeAPI) -> FakeAPI:
    api.bootstrap = make_bootstrap(
        [make_event(1, finished=True, data_checked=True), make_event(2)],
        elements=[make_element(1), make_element(2, team=2), make_element(3, team=3)],
    )
    api.fixtures = [
        make_fixture(1, event=1, team_h=1, team_a=2),
        make_fixture(2, event=1, team_h=3, team_a=4),
        make_fixture(
            3, event=2, team_h=2, team_a=1, finished=False, team_h_score=None, team_a_score=None
        ),
    ]
    return api


def read(store: FakeStore, table: str, tmp_path) -> duckdb.DuckDBPyRelation:
    return read_curated(store, keys.curated_key(SEASON, table), tmp_path)


class TestRawLayer:
    def test_writes_the_fixed_current_keys_and_a_dated_copy(self, loaded, store, client):
        written = hourly_current.run(client, store, on=DAY, now=NOW)

        assert keys.current_bootstrap_key(SEASON) in written
        assert keys.current_fixtures_key(SEASON) in written
        assert keys.bootstrap_key(SEASON, DAY) in written
        assert keys.fixtures_key(SEASON, DAY) in written

    def test_stores_the_api_response_verbatim(self, loaded, store, client):
        hourly_current.run(client, store, on=DAY, now=NOW)
        stored = store.objects[keys.current_bootstrap_key(SEASON)]
        assert stored.json == loaded.bootstrap

    def test_fixtures_are_fetched_unfiltered(self, loaded, store, client):
        """The snapshot is the whole season's schedule, not one gameweek."""
        hourly_current.run(client, store, on=DAY, now=NOW)
        assert not any("event=" in url for url in loaded.calls)
        assert len(store.objects[keys.current_fixtures_key(SEASON)].json) == 3

    def test_next_season_lands_under_a_new_prefix_with_no_code_change(self, loaded, store, client):
        loaded.bootstrap = make_bootstrap([make_event(1)], season="2027_28")
        written = hourly_current.run(client, store, on=DAY, now=NOW)
        assert any(key.startswith("raw/2027-28/current/") for key in written)
        assert any(key.startswith("curated/2027-28/") for key in written)


class TestAlwaysOverwrites:
    def test_a_second_run_replaces_rather_than_skipping(self, loaded, store, client):
        """Job 1 is the one job that must not be short-circuited by idempotency:
        the whole point is that the snapshot is fresh."""
        hourly_current.run(client, store, on=DAY, now=NOW)
        before = len(store.objects)

        loaded.bootstrap["elements"][0]["now_cost"] = 99
        hourly_current.run(client, store, on=DAY, now=NOW)

        assert len(store.objects) == before
        assert (
            store.objects[keys.current_bootstrap_key(SEASON)].json["elements"][0]["now_cost"] == 99
        )

    def test_it_never_consults_an_existence_check(self, loaded, store, client):
        hourly_current.run(client, store, on=DAY, now=NOW)
        assert store.exists_calls == []


class TestCuratedTables:
    def test_writes_every_table_it_owns(self, loaded, store, client):
        written = hourly_current.run(client, store, on=DAY, now=NOW)
        for table in ("fpl_current", "dim_player", "dim_team", "dim_fixture", "dim_gameweek"):
            assert keys.curated_key(SEASON, table) in written

    @pytest.mark.parametrize(
        "table", ["fpl_current", "dim_player", "dim_team", "dim_fixture", "dim_gameweek"]
    )
    def test_each_table_matches_the_column_contract(self, loaded, store, client, tmp_path, table):
        hourly_current.run(client, store, on=DAY, now=NOW)
        assert column_types(read(store, table, tmp_path)) == curated_schema.COLUMNS[table]

    def test_fpl_current_carries_the_live_state_and_a_fetched_at(
        self, loaded, store, client, tmp_path
    ):
        hourly_current.run(client, store, on=DAY, now=NOW)
        columns = one_row(read(store, "fpl_current", tmp_path).filter("element_id = 1"))
        assert columns["now_cost"] == 51
        assert columns["selected_by_percent"] == pytest.approx(12.5)
        assert columns["form"] == pytest.approx(3.4)
        assert columns["team_master_id"] == "ARS"
        assert columns["fetched_at"] == NOW.replace(tzinfo=None)

    def test_dim_gameweek_is_fully_populated_unlike_the_backfill(
        self, loaded, store, client, tmp_path
    ):
        """The archive has no events file, so backfilled seasons leave these NULL.
        Live, they all come straight off `events[]`."""
        hourly_current.run(client, store, on=DAY, now=NOW)
        row = one_row(read(store, "dim_gameweek", tmp_path).filter("gameweek = 1"))
        assert row["name"] == "Gameweek 1"
        assert row["deadline_time"] is not None
        assert row["deadline_time_epoch"] is not None
        assert row["data_checked"] is True
        assert row["fixture_count"] == 2

    def test_dim_fixture_keeps_unplayed_fixtures_with_null_scores(
        self, loaded, store, client, tmp_path
    ):
        hourly_current.run(client, store, on=DAY, now=NOW)
        row = one_row(read(store, "dim_fixture", tmp_path).filter("fixture_id = 3"))
        assert (row["home_score"], row["away_score"], row["finished"]) == (None, None, False)
        assert row["home_team_master_id"] == "AVL"

    def test_managers_are_excluded_from_the_player_tables(self, loaded, store, client, tmp_path):
        loaded.bootstrap["elements"].append(make_element(9, element_type=5))
        hourly_current.run(client, store, on=DAY, now=NOW)
        ids = [r["element_id"] for r in relation_rows(read(store, "dim_player", tmp_path))]
        assert 9 not in ids


class TestMasterTables:
    def test_new_players_get_master_ids_and_the_tables_are_written(self, loaded, store, client):
        written = hourly_current.run(client, store, on=DAY, now=NOW)
        for table in ("dim_player_master", "map_player_season", "map_team_season"):
            assert keys.master_key(f"{table}.parquet") in written

    def test_master_ids_are_stable_across_runs(self, loaded, store, client, tmp_path):
        hourly_current.run(client, store, on=DAY, now=NOW)
        first = (
            read(store, "dim_player", tmp_path).project("element_id, player_master_id").fetchall()
        )

        hourly_current.run(client, store, on=DAY, now=NOW)
        second = (
            read(store, "dim_player", tmp_path).project("element_id, player_master_id").fetchall()
        )
        assert first == second

    def test_an_unchanged_squad_does_not_rewrite_the_master_tables(self, loaded, store, client):
        """Hourly runs shouldn't churn files that nothing has changed."""
        hourly_current.run(client, store, on=DAY, now=NOW)
        second = hourly_current.run(client, store, on=DAY, now=NOW)
        assert keys.master_key("dim_player_master.parquet") not in second

    def test_a_new_signing_extends_rather_than_reassigns(self, loaded, store, client, tmp_path):
        hourly_current.run(client, store, on=DAY, now=NOW)
        before = dict(
            read(store, "dim_player", tmp_path).project("element_id, player_master_id").fetchall()
        )

        loaded.bootstrap["elements"].append(make_element(4, team=4, code=999_999))
        hourly_current.run(client, store, on=DAY, now=NOW)
        after = dict(
            read(store, "dim_player", tmp_path).project("element_id, player_master_id").fetchall()
        )

        assert {k: v for k, v in after.items() if k in before} == before
        assert after[4] == max(before.values()) + 1


def test_the_curated_layer_is_rebuildable_from_the_raw_snapshot(loaded, store, client):
    """Raw is the replayable source of truth: what we stored is what we transformed."""
    hourly_current.run(client, store, on=DAY, now=NOW)
    raw = json.loads(store.objects[keys.current_bootstrap_key(SEASON)].body)
    assert [e["id"] for e in raw["elements"]] == [1, 2, 3]
