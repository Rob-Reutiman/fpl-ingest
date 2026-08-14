"""The validation suite and the job end to end, with the archive fetch stubbed.

No network: `archive.fetch_season` is monkeypatched to hand back fixture seasons
already on disk, and the store is the `FakeStore` the other job tests use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from fpl import curated_schema, keys
from fpl.backfill import transform, validate
from fpl.backfill.validate import BackfillValidationError, CheckResults
from fpl.identity import MasterRegistry
from fpl.jobs import backfill
from fpl.transforms import parquet, team_fixture
from tests import backfill_fixtures as fx
from tests.conftest import FakeStore

SEASONS = ("2023-24", "2024-25")


def season_data(season: str, *, code_offset: int = 0) -> dict[str, Any]:
    """A tiny but *complete* season: both sides of every fixture, one blank, one double."""
    # Season totals must reconcile with the appearances below (2 points each), or
    # the spot check correctly refuses the season.
    players = [
        fx.player(1, "Home", "One", code=1 + code_offset, team_id=1, total_points=6),
        fx.player(2, "Away", "One", code=2 + code_offset, team_id=2, total_points=6),
        fx.player(3, "Home", "Two", code=3 + code_offset, team_id=3, total_points=2),
        fx.player(4, "Away", "Two", code=4 + code_offset, team_id=4, total_points=2),
    ]
    fixtures = [
        fx.fixture(101, 1, home=1, away=2),
        fx.fixture(102, 1, home=3, away=4),
        # GW2 is a double for ARS/BRE and a blank for CHE/EVE.
        fx.fixture(103, 2, home=1, away=2),
        fx.fixture(104, 2, home=2, away=1),
    ]
    appearances = [
        fx.appearance(1, "Home One", 101, 1, team="Arsenal", opponent=2, was_home=True),
        fx.appearance(2, "Away One", 101, 1, team="Brentford", opponent=1, was_home=False),
        fx.appearance(3, "Home Two", 102, 1, team="Chelsea", opponent=4, was_home=True),
        fx.appearance(4, "Away Two", 102, 1, team="Everton", opponent=3, was_home=False),
        fx.appearance(1, "Home One", 103, 2, team="Arsenal", opponent=2, was_home=True),
        fx.appearance(2, "Away One", 103, 2, team="Brentford", opponent=1, was_home=False),
        fx.appearance(1, "Home One", 104, 2, team="Arsenal", opponent=2, was_home=False),
        fx.appearance(2, "Away One", 104, 2, team="Brentford", opponent=1, was_home=True),
    ]
    return {"players": players, "fixtures": fixtures, "appearances": appearances}


@pytest.fixture
def archive_root(tmp_path: Path, monkeypatch) -> Path:
    """Write the fixture seasons and stub out the download."""
    root = tmp_path / "archive"
    for index, season in enumerate(SEASONS):
        fx.write_season(
            root,
            season,
            with_defcon=season >= validate.DEFCON_FIRST_SEASON,
            **season_data(season, code_offset=index * 100 if index else 0),
        )

    def fake_fetch(season: str, cache_dir: Path, **_: Any):
        return fx.write_season(
            root,
            season,
            with_defcon=season >= validate.DEFCON_FIRST_SEASON,
            **season_data(season),
        )

    monkeypatch.setattr(backfill.archive, "fetch_season", fake_fetch)
    # The fixture seasons are four players, not 27,000.
    monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
    monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
    return root


def run_job(store: FakeStore, tmp_path: Path, **kwargs: Any) -> list[str]:
    return backfill.run(
        store,
        seasons=SEASONS,
        cache_dir=tmp_path / "cache",
        staging_dir=kwargs.pop("staging_dir", tmp_path / "staging"),
        **kwargs,
    )


class TestEndToEnd:
    def test_writes_curated_master_and_provenance(self, archive_root, tmp_path):
        store = FakeStore()
        written = run_job(store, tmp_path)

        for season in SEASONS:
            for table in curated_schema.SEASON_TABLES:
                assert keys.curated_key(season, table) in written
            assert keys.raw_archive_key(season, "merged_gw.csv") in written
        for table in curated_schema.MASTER_TABLES:
            assert keys.master_key(f"{table}.parquet") in written
        assert keys.master_key("player_match_review.csv") in written
        assert keys.master_key("backfill_report.md") in written

    def test_source_files_are_stored_unmodified(self, archive_root, tmp_path):
        store = FakeStore()
        run_job(store, tmp_path)
        stored = store.objects[keys.raw_archive_key("2023-24", "teams.csv")].body
        assert stored == (archive_root / "2023-24" / "teams.csv").read_bytes()

    def test_grain_rules_survive_the_whole_pipeline(self, archive_root, tmp_path):
        store = FakeStore()
        run_job(store, tmp_path, staging_dir=tmp_path / "staging")
        con = duckdb.connect()
        path = (tmp_path / "staging" / "2023-24" / "fact_player_fixture.parquet").as_posix()

        # GW2 is a blank for Chelsea and Everton: no rows at all.
        assert con.execute(
            f"SELECT count(*) FROM read_parquet('{path}') "
            "WHERE gameweek = 2 AND team_master_id IN ('CHE', 'EVE')"
        ).fetchone() == (0,)
        # And a double for Arsenal: two rows, distinct fixtures.
        assert con.execute(
            f"SELECT count(DISTINCT fixture_id) FROM read_parquet('{path}') "
            "WHERE gameweek = 2 AND element_id = 1"
        ).fetchone() == (2,)

    def test_multi_season_glob_unions(self, archive_root, tmp_path):
        store = FakeStore()
        run_job(store, tmp_path, staging_dir=tmp_path / "staging")
        con = duckdb.connect()
        glob = (tmp_path / "staging" / "*" / "fact_player_fixture.parquet").as_posix()
        seasons = con.execute(
            f"SELECT DISTINCT season FROM read_parquet('{glob}') ORDER BY season"
        ).fetchall()
        assert seasons == [("2023-24",), ("2024-25",)]


class TestIdempotency:
    def test_a_second_run_produces_identical_bytes(self, archive_root, tmp_path):
        first, second = FakeStore(), FakeStore()
        run_job(first, tmp_path, staging_dir=tmp_path / "first")
        run_job(second, tmp_path, staging_dir=tmp_path / "second")

        written = [key for key in first.keys if key.endswith(".parquet")]
        assert written
        for key in written:
            assert first.objects[key].body == second.objects[key].body, key

    def test_re_running_extends_master_ids_rather_than_duplicating_them(
        self, archive_root, tmp_path
    ):
        """The second run reads back the master table the first one wrote."""
        store = FakeStore()
        run_job(store, tmp_path, staging_dir=tmp_path / "first")
        before = store.objects[keys.master_key("dim_player_master.parquet")].body

        run_job(store, tmp_path, staging_dir=tmp_path / "second")
        after = store.objects[keys.master_key("dim_player_master.parquet")].body
        assert before == after

    def test_existing_masters_are_loaded_back_from_the_bucket(self, archive_root, tmp_path):
        store = FakeStore()
        run_job(store, tmp_path, staging_dir=tmp_path / "first")
        loaded = backfill.load_existing_masters(store)
        assert loaded
        assert all(master.player_code is not None for master in loaded)

    def test_no_existing_master_starts_from_scratch(self):
        assert backfill.load_existing_masters(FakeStore()) == []


class TestValidationFailsLoudly:
    """Each check gets fed data that should trip it, so a silently-passing check
    can't hide behind fixtures that happen to be well-formed."""

    def _build(self, tmp_path: Path, season: str, data: dict[str, Any]):
        sources = fx.write_season(tmp_path, season, **data)
        con = parquet.connect()
        transform.load_season_sources(con, sources)
        transform.build_dim_team(con, season)
        registry = MasterRegistry()
        transform.register_master_map(
            con, season, registry.resolve_season(transform.read_season_players(con, season))
        )
        transform.build_dim_player(con, season)
        transform.build_dim_fixture(con, season)
        transform.build_dim_gameweek(con, season)
        transform.build_fact_source(con, sources)
        transform.build_fact_player_fixture(con, season)
        transform.build_fact_player_gameweek_fpl(con, season)
        team_fixture.build_fact_team_fixture(con, season)
        return con

    def test_a_clean_season_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        con = self._build(tmp_path, "2023-24", season_data("2023-24"))
        validate.validate_season(con, "2023-24", CheckResults())

    def test_an_implausible_row_count_fails(self, tmp_path):
        con = self._build(tmp_path, "2023-24", season_data("2023-24"))
        with pytest.raises(BackfillValidationError, match="outside the plausible"):
            validate.validate_season(con, "2023-24", CheckResults())

    def test_conflicting_duplicate_source_rows_fail(self, tmp_path, monkeypatch):
        """Exact duplicates are collapsed; two different readings of one appearance
        must not be."""
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        data = season_data("2023-24")
        conflicting = dict(data["appearances"][0])
        conflicting["minutes"] = 45  # same (element, fixture), different reading
        data["appearances"] = [*data["appearances"], conflicting]

        con = self._build(tmp_path, "2023-24", data)
        with pytest.raises(BackfillValidationError, match="conflicting source rows"):
            validate.validate_season(con, "2023-24", CheckResults())

    def test_a_gameweek_disagreeing_with_round_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        data = season_data("2023-24")
        data["appearances"][0] = {**data["appearances"][0], "round": 7}
        con = self._build(tmp_path, "2023-24", data)
        with pytest.raises(BackfillValidationError, match="disagrees with"):
            validate.validate_season(con, "2023-24", CheckResults())

    def test_a_season_without_a_double_gameweek_fails(self, tmp_path, monkeypatch):
        """If a season's source collapsed doubles into one row, say so loudly."""
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        data = season_data("2023-24")
        data["fixtures"] = data["fixtures"][:3]
        data["appearances"] = [a for a in data["appearances"] if a["fixture"] != 104]
        con = self._build(tmp_path, "2023-24", data)
        with pytest.raises(BackfillValidationError, match="collapsed double gameweeks"):
            validate.validate_season(con, "2023-24", CheckResults())

    def test_a_row_for_a_blanked_team_fails(self, tmp_path, monkeypatch):
        """The check that catches zero-rows-for-blanks, the corruption this
        pipeline most wants to avoid."""
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        data = season_data("2023-24")
        # Chelsea blanks in GW2, but claim an appearance there anyway.
        data["appearances"].append(
            fx.appearance(
                3, "Home Two", 102, 2, team="Chelsea", opponent=4, was_home=True, minutes=0
            )
        )
        con = self._build(tmp_path, "2023-24", data)
        with pytest.raises(BackfillValidationError, match="no fixture that gameweek"):
            validate.validate_season(con, "2023-24", CheckResults())

    def test_defensive_contribution_present_before_it_existed_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        data = season_data("2023-24")
        data["appearances"] = [{**a, "defensive_contribution": 0} for a in data["appearances"]]
        con = self._build(tmp_path, "2023-24", {**data, "with_defcon": True})
        with pytest.raises(BackfillValidationError, match="predates defensive_contribution"):
            validate.validate_season(con, "2023-24", CheckResults())

    def test_a_season_total_that_does_not_reconcile_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validate, "MIN_FACT_ROWS", 1)
        monkeypatch.setattr(validate, "MAX_FACT_ROWS", 1_000)
        data = season_data("2023-24")
        data["players"][0] = {**data["players"][0], "total_points": 999}
        con = self._build(tmp_path, "2023-24", data)
        with pytest.raises(BackfillValidationError, match="season total mismatch"):
            validate.validate_season(con, "2023-24", CheckResults())


class TestReportContents:
    def test_the_report_records_exclusions_and_spot_checks(self, archive_root, tmp_path):
        store = FakeStore()
        run_job(store, tmp_path)
        body = store.objects[keys.master_key("backfill_report.md")].body.decode()

        assert "# FPL historical backfill report" in body
        assert "top scorer total points" in body
        assert "`xP`" in body
        for season in SEASONS:
            assert season in body
