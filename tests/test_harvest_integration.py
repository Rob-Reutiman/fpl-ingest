"""Live integration tests for cohort + harvest ingestion (Modules 5 & 6).

EXCLUDED from the default run and CI. Run manually with:

    pytest -m integration

Unit tests for these modules mock the client with hand-built payloads, so they
can't catch the one class of bug that actually breaks a scraper: a wrong
assumption about the real, undocumented API. These tests validate exactly that
surface — the field names the mappers read, the 404 semantics harvest relies
on, and the cache-bust query param appended to the transfers URL — against the
live endpoints. Keep them few and light; they make real network calls.

Most picks/standings/transfers checks need a live season (a gameweek past its
deadline) and SKIP pre-season, so treat them as a season-start smoke check. The
cache-bust-param and invalid-manager tests are season-independent and run
year-round.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fpl.client import FPLClient
from fpl.ingest.cohort import scrape_current_standings
from fpl.ingest.harvest import harvest_picks
from fpl.ingest.mappers import detect_current_gw, map_cohort_picks, map_cohort_transfers
from fpl.storage import Storage

pytestmark = pytest.mark.integration

# Manager 1 is the oldest FPL account and reliably exists; used read-only.
REAL_MANAGER_ID = 1
# Far outside any plausible entry range — the API 404s these at any gameweek.
INVALID_MANAGER_ID = 999_999_999

PICK_COLUMNS = {
    "gameweek",
    "manager_id",
    "fpl_id",
    "squad_position",
    "multiplier",
    "is_captain",
    "is_vice_captain",
    "active_chip",
}
TRANSFER_COLUMNS = {
    "manager_id",
    "gameweek",
    "fpl_id_in",
    "fpl_id_out",
    "cost_in",
    "cost_out",
    "transfer_time",
}


async def _current_gw(client: FPLClient) -> int:
    data = await client.get("bootstrap-static/")
    return detect_current_gw(data["events"])


def _scalar(s: Storage, sql: str):
    row = s._conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# Cache-bust param — season-independent, checkable today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfers_endpoint_accepts_cache_bust_param(tmp_path: Path):
    """The ``?h=`` param harvest_transfers appends must not break the request.

    harvest_transfers relies on FPL ignoring this unknown query param to scope
    the disk cache per gameweek. If the API 400s on it instead, transfer
    harvesting is entirely broken — and no unit test would show it. The
    transfers endpoint responds year-round, so this is validatable pre-season.
    """
    async with FPLClient(cache_dir=tmp_path / "cache") as client:
        data = await client.get(f"entry/{REAL_MANAGER_ID}/transfers/?h=gw5")
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Invalid manager — season-independent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_manager_counted_as_skipped(tmp_path: Path):
    """A nonexistent manager 404s and is tallied as skipped, not failed.

    Confirms the 404 classification in ``_classify`` against the real status
    code. Season-independent: a manager that doesn't exist 404s at any GW.
    """
    async with FPLClient(cache_dir=tmp_path / "cache") as client:
        with Storage(str(tmp_path / "test.duckdb")) as s:
            result = await harvest_picks(client, s, [INVALID_MANAGER_ID], gw=1)
            count = _scalar(s, "SELECT count(*) FROM cohort_pick")

    assert result.attempted == 1
    assert result.skipped == 1
    assert result.failed == 0
    assert result.succeeded == 0
    assert count == 0


# ---------------------------------------------------------------------------
# Real picks / transfers / standings — skip pre-season
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_picks_map_and_store(tmp_path: Path):
    """A real picks response maps to 15 schema-valid rows and stores cleanly.

    Exercises the full picks path against live data — mapper field names and
    DuckDB write. Skips pre-season, when the current GW has no picks (404).
    """
    async with FPLClient(cache_dir=tmp_path / "cache") as client:
        gw = await _current_gw(client)
        try:
            response = await client.get(f"entry/{REAL_MANAGER_ID}/event/{gw}/picks/")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                pytest.skip("picks not available (likely pre-season)")
            raise

    rows = map_cohort_picks(response, manager_id=REAL_MANAGER_ID, gameweek=gw)
    assert len(rows) == 15
    assert all(set(row) == PICK_COLUMNS for row in rows)
    assert sum(row["is_captain"] for row in rows) == 1
    assert sum(row["is_vice_captain"] for row in rows) == 1
    assert all(row["multiplier"] in (0, 1, 2, 3) for row in rows)

    with Storage(str(tmp_path / "test.duckdb")) as s:
        s.insert_picks(rows)
        count = _scalar(s, "SELECT count(*) FROM cohort_pick")
    assert count == 15


@pytest.mark.asyncio
async def test_real_transfers_map_and_store(tmp_path: Path):
    """Real transfer rows carry the fields the mapper reads and store cleanly.

    Field validation is conditional: a manager with no transfers yet returns an
    empty list, which still confirms the endpoint shape. Skips in that case.
    """
    async with FPLClient(cache_dir=tmp_path / "cache") as client:
        response = await client.get(f"entry/{REAL_MANAGER_ID}/transfers/?h=all")
    assert isinstance(response, list)

    rows = map_cohort_transfers(response)
    if not rows:
        pytest.skip("manager has no transfers this season")

    assert all(set(row) == TRANSFER_COLUMNS for row in rows)
    assert all(isinstance(row["gameweek"], int) for row in rows)
    # Same DuckDB tz trap as fixtures: the parsed timestamp must be naive UTC.
    assert all(row["transfer_time"].tzinfo is None for row in rows)

    with Storage(str(tmp_path / "test.duckdb")) as s:
        s.insert_transfers(rows)
        count = _scalar(s, "SELECT count(*) FROM cohort_transfer")
    assert count == len(rows)


@pytest.mark.asyncio
async def test_real_standings_page_maps(tmp_path: Path):
    """The overall-league standings page carries the fields Module 5 reads.

    Validates ``map_standings_page`` field names against the live league-314
    response. ``max_rank=50`` keeps it to a single page. Skips pre-season, when
    the league is empty and reports no entries.
    """
    async with FPLClient(cache_dir=tmp_path / "cache") as client:
        entries = await scrape_current_standings(client, gw=1, max_rank=50)

    if not entries:
        pytest.skip("standings empty (likely pre-season)")

    top = entries[0]
    assert top.rank == 1
    assert top.manager_id > 0
    assert top.total_points >= 0
