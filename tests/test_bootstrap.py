"""Acceptance tests for Module 4 — Bootstrap Ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fpl.ingest.bootstrap import ingest_bootstrap, ingest_my_team
from fpl.storage import Storage

N_PLAYERS = 700
N_TEAMS = 20
N_FIXTURES = 380
CURRENT_GW = 5


# ---------------------------------------------------------------------------
# Stub builders — shaped like the real API, sized like a real season
# ---------------------------------------------------------------------------


def _bootstrap_stub(
    n_players: int = N_PLAYERS,
    n_teams: int = N_TEAMS,
    current_gw: int | None = CURRENT_GW,
) -> dict:
    teams = [
        {"id": i, "name": f"Team {i}", "short_name": f"T{i:02d}"} for i in range(1, n_teams + 1)
    ]
    elements = [
        {
            "id": i,
            "code": 100_000 + i,  # Distinct from fpl_id on purpose.
            "web_name": f"Player{i}",
            "first_name": f"First{i}",
            "second_name": f"Last{i}",
            "team": (i % n_teams) + 1,
            "element_type": (i % 4) + 1,
            "now_cost": 40 + (i % 100),
            "news": "",
            "total_points": i,
            "minutes": i * 2,
            "goals_scored": i % 20,
            "assists": i % 15,
            "clean_sheets": i % 10,
            "bps": i * 3,
            "selected_by_percent": f"{(i % 500) / 10:.1f}",
            "transfers_in_event": i * 11,
            "transfers_out_event": i * 7,
            "ep_next": f"{(i % 90) / 10:.1f}",
        }
        for i in range(1, n_players + 1)
    ]
    events = [
        {"id": gw, "is_current": gw == current_gw, "is_next": gw == (current_gw or 0) + 1}
        for gw in range(1, 39)
    ]
    return {"elements": elements, "teams": teams, "events": events}


def _fixtures_stub(n: int = N_FIXTURES, *, played: bool = True) -> list[dict]:
    return [
        {
            "id": i,
            "event": ((i - 1) // 10) + 1,
            "team_h": ((i * 2) % N_TEAMS) + 1,
            "team_a": ((i * 3) % N_TEAMS) + 1,
            "team_h_score": (i % 4) if played else None,
            "team_a_score": (i % 3) if played else None,
            "kickoff_time": f"2025-08-{(i % 28) + 1:02d}T19:00:00Z" if played else None,
            "finished": played,
        }
        for i in range(1, n + 1)
    ]


def _client(bootstrap: dict | None = None, fixtures: list[dict] | None = None) -> AsyncMock:
    """An FPLClient stand-in that routes by path, like the real endpoints."""
    bootstrap = _bootstrap_stub() if bootstrap is None else bootstrap
    fixtures = _fixtures_stub() if fixtures is None else fixtures

    async def _route(path: str) -> Any:
        if "fixtures" in path:
            return fixtures
        if "bootstrap-static" in path:
            return bootstrap
        raise AssertionError(f"unexpected path: {path}")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_route)
    return client


def _storage(tmp_path: Path) -> Storage:
    return Storage(str(tmp_path / "test.duckdb"))


def _scalar(s: Storage, sql: str):
    row = s._conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# ingest_bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dim_player_row_count(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        assert _scalar(s, "SELECT count(*) FROM dim_player") == N_PLAYERS


@pytest.mark.asyncio
async def test_dim_team_row_count(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        assert _scalar(s, "SELECT count(*) FROM dim_team") == N_TEAMS


@pytest.mark.asyncio
async def test_every_player_has_code_and_fpl_id(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        nulls = _scalar(s, "SELECT count(*) FROM dim_player WHERE code IS NULL OR fpl_id IS NULL")
    assert nulls == 0


@pytest.mark.asyncio
async def test_fpl_id_is_unique(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        distinct = _scalar(s, "SELECT count(DISTINCT fpl_id) FROM dim_player")
        total = _scalar(s, "SELECT count(*) FROM dim_player")
    assert distinct == total == N_PLAYERS


@pytest.mark.asyncio
async def test_fact_player_gw_one_row_per_player_for_current_gw(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        rows = _scalar(s, f"SELECT count(*) FROM fact_player_gw WHERE gameweek = {CURRENT_GW}")
        gws = _scalar(s, "SELECT count(DISTINCT gameweek) FROM fact_player_gw")
    assert rows == N_PLAYERS
    assert gws == 1


@pytest.mark.asyncio
async def test_dim_fixture_row_count(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        assert _scalar(s, "SELECT count(*) FROM dim_fixture") == N_FIXTURES


@pytest.mark.asyncio
async def test_returns_detected_current_gw(tmp_path: Path):
    with _storage(tmp_path) as s:
        assert await ingest_bootstrap(_client(), s) == CURRENT_GW


@pytest.mark.asyncio
async def test_falls_back_to_is_next_preseason(tmp_path: Path):
    """No event is current before the season starts — use is_next."""
    stub = _bootstrap_stub(current_gw=None)
    for event in stub["events"]:
        event["is_next"] = event["id"] == 1
    with _storage(tmp_path) as s:
        assert await ingest_bootstrap(_client(bootstrap=stub), s) == 1


@pytest.mark.asyncio
async def test_preseason_fixtures_all_null_scores(tmp_path: Path):
    """Before a ball is kicked every score and kickoff is null.

    Polars infers a Null dtype for a column that is None in every row; this is
    the regression guard that such a frame still lands in typed INTEGER and
    TIMESTAMP columns.
    """
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(fixtures=_fixtures_stub(played=False)), s)
        total = _scalar(s, "SELECT count(*) FROM dim_fixture")
        scored = _scalar(s, "SELECT count(*) FROM dim_fixture WHERE team_h_score IS NOT NULL")
        kicked = _scalar(s, "SELECT count(*) FROM dim_fixture WHERE kickoff_time IS NOT NULL")
    assert total == N_FIXTURES
    assert scored == 0
    assert kicked == 0


@pytest.mark.asyncio
async def test_kickoff_time_stored_in_utc(tmp_path: Path):
    """A tz-aware datetime would be shifted to local time on write."""
    fixtures = [
        {
            "id": 1,
            "event": 1,
            "team_h": 1,
            "team_a": 2,
            "team_h_score": None,
            "team_a_score": None,
            "kickoff_time": "2025-08-16T19:00:00Z",
            "finished": False,
        }
    ]
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(fixtures=fixtures), s)
        hour = _scalar(s, "SELECT hour(kickoff_time) FROM dim_fixture WHERE id = 1")
    assert hour == 19


@pytest.mark.asyncio
async def test_player_team_name_is_denormalised(tmp_path: Path):
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        blanks = _scalar(s, "SELECT count(*) FROM dim_player WHERE team_name = ''")
    assert blanks == 0


@pytest.mark.asyncio
async def test_ingest_bootstrap_is_idempotent(tmp_path: Path):
    """Re-running duplicates nothing — every write is an upsert."""
    with _storage(tmp_path) as s:
        await ingest_bootstrap(_client(), s)
        await ingest_bootstrap(_client(), s)
        assert _scalar(s, "SELECT count(*) FROM dim_player") == N_PLAYERS
        assert _scalar(s, "SELECT count(*) FROM dim_team") == N_TEAMS
        assert _scalar(s, "SELECT count(*) FROM dim_fixture") == N_FIXTURES
        assert _scalar(s, "SELECT count(*) FROM fact_player_gw") == N_PLAYERS


# ---------------------------------------------------------------------------
# ingest_my_team
# ---------------------------------------------------------------------------


def _picks_payload(n: int = 15) -> dict:
    return {
        "active_chip": None,
        "picks": [
            {
                "element": i,
                "position": i,
                "multiplier": 2 if i == 1 else (1 if i <= 11 else 0),
                "is_captain": i == 1,
                "is_vice_captain": i == 2,
            }
            for i in range(1, n + 1)
        ],
    }


@pytest.mark.asyncio
async def test_ingest_my_team_stores_15_picks(tmp_path: Path):
    client = AsyncMock()
    client.get = AsyncMock(return_value=_picks_payload())
    with _storage(tmp_path) as s:
        await ingest_my_team(client, s, manager_id=12345, gw=7)
        assert _scalar(s, "SELECT count(*) FROM my_pick WHERE gameweek = 7") == 15
        assert sorted(s.get_my_picks(7)) == list(range(1, 16))


@pytest.mark.asyncio
async def test_ingest_my_team_round_trips_captain_and_multiplier(tmp_path: Path):
    client = AsyncMock()
    client.get = AsyncMock(return_value=_picks_payload())
    with _storage(tmp_path) as s:
        await ingest_my_team(client, s, manager_id=12345, gw=7)
        captains = _scalar(s, "SELECT count(*) FROM my_pick WHERE is_captain")
        benched = _scalar(s, "SELECT count(*) FROM my_pick WHERE multiplier = 0")
        cap_mult = _scalar(s, "SELECT multiplier FROM my_pick WHERE is_captain")
    assert captains == 1
    assert cap_mult == 2
    assert benched == 4


@pytest.mark.asyncio
async def test_ingest_my_team_requests_correct_path(tmp_path: Path):
    client = AsyncMock()
    client.get = AsyncMock(return_value=_picks_payload())
    with _storage(tmp_path) as s:
        await ingest_my_team(client, s, manager_id=98765, gw=3)
    client.get.assert_awaited_once_with("entry/98765/event/3/picks/")
