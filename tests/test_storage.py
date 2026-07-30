"""Acceptance tests for Module 3 — Storage Layer."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from fpl.storage import Storage

# ---------------------------------------------------------------------------
# Stubs (schema-keyed dicts — keys match column names)
# ---------------------------------------------------------------------------

PLAYERS = [
    {
        "code": 111,
        "fpl_id": 1,
        "web_name": "Salah",
        "full_name": "Mohamed Salah",
        "team": 12,
        "team_name": "Liverpool",
        "position": 3,
        "now_cost": 130,
        "news": "",
    },
    {
        "code": 222,
        "fpl_id": 2,
        "web_name": "Haaland",
        "full_name": "Erling Haaland",
        "team": 13,
        "team_name": "Man City",
        "position": 4,
        "now_cost": 150,
        "news": "",
    },
]

FIXTURES = [
    {
        "id": 1,
        "gameweek": 1,
        "team_h": 12,
        "team_a": 13,
        "team_h_score": 2,
        "team_a_score": 1,
        "kickoff_time": None,
        "finished": True,
    },
]

PICKS = [
    {
        "gameweek": 1,
        "manager_id": 500,
        "fpl_id": 1,
        "squad_position": 11,
        "multiplier": 2,
        "is_captain": True,
        "is_vice_captain": False,
        "active_chip": None,
    },
    {
        "gameweek": 1,
        "manager_id": 500,
        "fpl_id": 2,
        "squad_position": 12,
        "multiplier": 1,
        "is_captain": False,
        "is_vice_captain": True,
        "active_chip": None,
    },
]

EXPECTED_TABLES = {
    "dim_player",
    "dim_team",
    "dim_fixture",
    "cohort_manager",
    "cohort_pick",
    "cohort_transfer",
    "fact_player_gw",
    "my_pick",
}


def _storage(tmp_path: Path) -> Storage:
    return Storage(str(tmp_path / "test.duckdb"))


def _scalar(s: Storage, sql: str, params: list | None = None):
    """Fetch a single scalar from a query (asserts a row came back)."""
    row = s._conn.execute(sql, params or []).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_init_schema_creates_all_tables(tmp_path: Path):
    """A fresh DB has all eight tables after construction."""
    with _storage(tmp_path) as s:
        rows = s._conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
    names = {r[0] for r in rows}
    assert EXPECTED_TABLES <= names


def test_upsert_players_is_idempotent(tmp_path: Path):
    """Upserting the same players twice does not create duplicates."""
    with _storage(tmp_path) as s:
        s.upsert_players(PLAYERS)
        s.upsert_players(PLAYERS)
        count = _scalar(s, "SELECT count(*) FROM dim_player")
    assert count == len(PLAYERS)


def test_insert_or_replace_updates_existing_row(tmp_path: Path):
    """Re-upserting a player with a changed field overwrites the old value."""
    with _storage(tmp_path) as s:
        s.upsert_players(PLAYERS)
        s.upsert_players([{**PLAYERS[0], "now_cost": 125}])
        cost = _scalar(s, "SELECT now_cost FROM dim_player WHERE code = ?", [PLAYERS[0]["code"]])
        count = _scalar(s, "SELECT count(*) FROM dim_player")
    assert cost == 125
    assert count == len(PLAYERS)  # no duplicate row


def test_insert_picks_and_get_cohort_picks(tmp_path: Path):
    """insert_picks then get_cohort_picks returns a DataFrame with the right shape."""
    with _storage(tmp_path) as s:
        s.insert_picks(PICKS)
        df = s.get_cohort_picks(1)
    assert isinstance(df, pl.DataFrame)
    assert df.height == len(PICKS)
    assert {
        "gameweek",
        "manager_id",
        "fpl_id",
        "squad_position",
        "multiplier",
        "is_captain",
        "is_vice_captain",
        "active_chip",
    } <= set(df.columns)


def test_get_player_map(tmp_path: Path):
    """get_player_map returns a dict keyed by fpl_id with full player dicts."""
    with _storage(tmp_path) as s:
        s.upsert_players(PLAYERS)
        pmap = s.get_player_map()
    assert set(pmap) == {1, 2}
    assert pmap[1]["code"] == 111
    assert pmap[1]["web_name"] == "Salah"
    assert pmap[2]["team_name"] == "Man City"


def test_fixture_round_trip(tmp_path: Path):
    """Fixtures written then read back match the input."""
    with _storage(tmp_path) as s:
        s.upsert_fixtures(FIXTURES)
        cur = s._conn.execute("SELECT id, gameweek, team_h, team_a, finished FROM dim_fixture")
        rows = cur.fetchall()
    assert rows == [(1, 1, 12, 13, True)]


def test_get_player_by_fpl_id(tmp_path: Path):
    """Lookup returns the matching player dict, or None when absent."""
    with _storage(tmp_path) as s:
        s.upsert_players(PLAYERS)
        found = s.get_player_by_fpl_id(2)
        missing = s.get_player_by_fpl_id(999)
    assert found is not None
    assert found["web_name"] == "Haaland"
    assert missing is None


def test_gw_stamping_and_state_helpers(tmp_path: Path):
    """gw-param writers stamp gameweek; has_* / get_latest reflect written data."""
    stats = [{"fpl_id": 1, "total_points": 12, "minutes": 90}]
    managers = [{"manager_id": 500, "rank": 1, "total_points": 60, "is_top_slice": True}]
    with _storage(tmp_path) as s:
        assert s.has_player_gw_stats(3) is False
        assert s.has_cohort_picks(3) is False
        assert s.get_latest_gameweek() == 0

        s.upsert_player_gw_stats(3, stats)
        s.upsert_cohort_managers(3, managers)
        s.insert_picks([{**PICKS[0], "gameweek": 3}])

        assert s.has_player_gw_stats(3) is True
        assert s.has_cohort_picks(3) is True
        assert s.get_latest_gameweek() == 3
        assert s.get_cohort_manager_ids(3) == [500]


def test_my_picks_round_trip(tmp_path: Path):
    """upsert_my_picks stamps gw; get_my_picks returns the fpl_ids."""
    my = [
        {"fpl_id": 1, "multiplier": 2, "is_captain": True},
        {"fpl_id": 2, "multiplier": 1, "is_captain": False},
    ]
    with _storage(tmp_path) as s:
        s.upsert_my_picks(7, my)
        ids = s.get_my_picks(7)
    assert sorted(ids) == [1, 2]
