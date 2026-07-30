"""Acceptance tests for Module 6 — Picks & Transfer Harvesting."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from fpl.ingest.harvest import HarvestResult, harvest_picks, harvest_transfers
from fpl.storage import Storage

MANAGER_IDS = [555_001, 555_002, 555_003, 555_004, 555_005]
GW = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _storage(tmp_path: Path) -> Storage:
    return Storage(str(tmp_path / "test.duckdb"))


def _scalar(s: Storage, sql: str):
    row = s._conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _picks_payload(active_chip: str | None = None, n: int = 15) -> dict:
    return {
        "active_chip": active_chip,
        "entry_history": {"points": 61, "rank": 12345},
        "picks": [
            {
                "element": 100 + i,
                "position": i,
                "multiplier": 2 if i == 1 else (1 if i <= 11 else 0),
                "is_captain": i == 1,
                "is_vice_captain": i == 2,
            }
            for i in range(1, n + 1)
        ],
    }


def _transfers_payload(manager_id: int, events: list[int]) -> list[dict]:
    return [
        {
            "element_in": 328,
            "element_in_cost": 130,
            "element_out": 400 + i,  # Distinct per row so the PK doesn't collapse them.
            "element_out_cost": 75,
            "entry": manager_id,
            "event": event,
            "time": "2025-09-12T10:30:00Z",
        }
        for i, event in enumerate(events)
    ]


def _harvest_client(
    payloads: dict[int, Any],
    errors: dict[int, BaseException] | None = None,
) -> AsyncMock:
    """An FPLClient stand-in routing entry/{id}/... paths by manager ID.

    ``payloads`` maps manager_id -> response; ``errors`` maps manager_id -> the
    exception that manager's request raises. Works for both the picks and the
    transfers path shapes.
    """
    errors = errors or {}

    async def _get_many(paths: list[str], *, return_exceptions: bool = False) -> list[Any]:
        results: list[Any] = []
        for path in paths:
            match = re.search(r"entry/(\d+)/", path)
            assert match, f"path missing manager id: {path}"
            mid = int(match.group(1))
            if mid in errors:
                if not return_exceptions:
                    raise errors[mid]
                results.append(errors[mid])
            else:
                assert mid in payloads, f"unexpected manager: {mid}"
                results.append(payloads[mid])
        return results

    client = AsyncMock()
    client.get_many = AsyncMock(side_effect=_get_many)
    return client


def _requested_paths(client: AsyncMock) -> list[str]:
    """Flatten every path across all (chunked) get_many calls."""
    return [path for call in client.get_many.await_args_list for path in call.args[0]]


# ---------------------------------------------------------------------------
# harvest_picks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_picks_writes_all_rows(tmp_path: Path):
    client = _harvest_client({mid: _picks_payload() for mid in MANAGER_IDS})
    with _storage(tmp_path) as s:
        result = await harvest_picks(client, s, MANAGER_IDS, gw=GW)
        count = _scalar(s, "SELECT count(*) FROM cohort_pick")
    assert count == 75  # 5 managers x 15 picks.
    assert result.succeeded == 5
    assert result.attempted == 5


@pytest.mark.asyncio
async def test_harvest_picks_requests_correct_paths(tmp_path: Path):
    client = _harvest_client({mid: _picks_payload() for mid in MANAGER_IDS})
    with _storage(tmp_path) as s:
        await harvest_picks(client, s, MANAGER_IDS, gw=GW)
    assert _requested_paths(client) == [f"entry/{mid}/event/{GW}/picks/" for mid in MANAGER_IDS]


@pytest.mark.asyncio
async def test_harvest_picks_tags_freehit_on_every_row(tmp_path: Path):
    payloads: dict[int, Any] = {mid: _picks_payload() for mid in MANAGER_IDS}
    payloads[555_003] = _picks_payload(active_chip="freehit")
    client = _harvest_client(payloads)
    with _storage(tmp_path) as s:
        await harvest_picks(client, s, MANAGER_IDS, gw=GW)
        chipped = _scalar(s, "SELECT count(*) FROM cohort_pick WHERE active_chip = 'freehit'")
        unchipped = _scalar(s, "SELECT count(*) FROM cohort_pick WHERE active_chip IS NULL")
    assert chipped == 15
    assert unchipped == 60


@pytest.mark.asyncio
async def test_harvest_picks_skips_404_managers(tmp_path: Path):
    client = _harvest_client(
        {mid: _picks_payload() for mid in MANAGER_IDS},
        errors={555_002: _http_error(404)},
    )
    with _storage(tmp_path) as s:
        result = await harvest_picks(client, s, MANAGER_IDS, gw=GW)
        count = _scalar(s, "SELECT count(*) FROM cohort_pick")
    assert result.skipped == 1
    assert result.failed == 0
    assert result.succeeded == 4
    assert count == 60


@pytest.mark.asyncio
async def test_harvest_picks_is_idempotent(tmp_path: Path):
    client = _harvest_client({mid: _picks_payload() for mid in MANAGER_IDS})
    with _storage(tmp_path) as s:
        await harvest_picks(client, s, MANAGER_IDS, gw=GW)
        await harvest_picks(client, s, MANAGER_IDS, gw=GW)
        count = _scalar(s, "SELECT count(*) FROM cohort_pick")
    assert count == 75


@pytest.mark.asyncio
async def test_harvest_picks_no_managers_no_requests(tmp_path: Path):
    client = _harvest_client({})
    with _storage(tmp_path) as s:
        result = await harvest_picks(client, s, [], gw=GW)
        count = _scalar(s, "SELECT count(*) FROM cohort_pick")
    client.get_many.assert_not_awaited()
    assert count == 0
    assert (result.attempted, result.succeeded, result.failed, result.skipped) == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# harvest_transfers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_transfers_filters_by_target_gw(tmp_path: Path):
    client = _harvest_client({555_001: _transfers_payload(555_001, events=[3, 5, 5, 7])})
    with _storage(tmp_path) as s:
        result = await harvest_transfers(client, s, [555_001], target_gw=5)
        count = _scalar(s, "SELECT count(*) FROM cohort_transfer")
        off_gw = _scalar(s, "SELECT count(*) FROM cohort_transfer WHERE gameweek != 5")
    assert result.succeeded == 1
    assert count == 2
    assert off_gw == 0


@pytest.mark.asyncio
async def test_harvest_transfers_unfiltered_stores_all_gws(tmp_path: Path):
    client = _harvest_client({555_001: _transfers_payload(555_001, events=[3, 5, 5, 7])})
    with _storage(tmp_path) as s:
        await harvest_transfers(client, s, [555_001])
        count = _scalar(s, "SELECT count(*) FROM cohort_transfer")
    assert count == 4


@pytest.mark.asyncio
async def test_harvest_transfers_is_idempotent(tmp_path: Path):
    """Verifies the cohort_transfer primary key: a re-run must not append."""
    client = _harvest_client({mid: _transfers_payload(mid, events=[5, 5]) for mid in MANAGER_IDS})
    with _storage(tmp_path) as s:
        await harvest_transfers(client, s, MANAGER_IDS, target_gw=5)
        await harvest_transfers(client, s, MANAGER_IDS, target_gw=5)
        count = _scalar(s, "SELECT count(*) FROM cohort_transfer")
    assert count == 10  # 5 managers x 2 transfers, once.


@pytest.mark.asyncio
async def test_harvest_transfers_paths_include_cache_bust(tmp_path: Path):
    """The transfers URL is season-static while its response grows; without a
    per-GW cache key, week N's harvest would read week N-1's cached snapshot."""
    client = _harvest_client({mid: _transfers_payload(mid, events=[5]) for mid in MANAGER_IDS})
    with _storage(tmp_path) as s:
        await harvest_transfers(client, s, MANAGER_IDS, target_gw=5)
    assert _requested_paths(client) == [f"entry/{mid}/transfers/?h=gw5" for mid in MANAGER_IDS]


@pytest.mark.asyncio
async def test_harvest_transfers_skips_404_managers(tmp_path: Path):
    client = _harvest_client(
        {mid: _transfers_payload(mid, events=[5]) for mid in MANAGER_IDS},
        errors={555_005: _http_error(404)},
    )
    with _storage(tmp_path) as s:
        result = await harvest_transfers(client, s, MANAGER_IDS, target_gw=5)
        count = _scalar(s, "SELECT count(*) FROM cohort_transfer")
    assert result.skipped == 1
    assert result.succeeded == 4
    assert count == 4


# ---------------------------------------------------------------------------
# HarvestResult accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_result_counts_add_up(tmp_path: Path):
    """attempted == succeeded + failed + skipped, with 404 vs 5xx classified."""
    client = _harvest_client(
        {mid: _picks_payload() for mid in MANAGER_IDS},
        errors={555_002: _http_error(404), 555_004: _http_error(500)},
    )
    with _storage(tmp_path) as s:
        result = await harvest_picks(client, s, MANAGER_IDS, gw=GW)
    assert result == HarvestResult(
        attempted=5,
        succeeded=3,
        failed=1,
        skipped=1,
        duration_seconds=result.duration_seconds,
    )
    assert result.attempted == result.succeeded + result.failed + result.skipped
    assert result.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_harvest_chunks_large_batches(tmp_path: Path):
    """More managers than HARVEST_CHUNK_SIZE means multiple get_many calls,
    with no manager dropped or duplicated across chunks."""
    manager_ids = list(range(600_000, 600_250))
    client = _harvest_client({mid: _picks_payload() for mid in manager_ids})
    with _storage(tmp_path) as s:
        result = await harvest_picks(client, s, manager_ids, gw=GW)
    assert client.get_many.await_count == 3  # 250 managers / 100 per chunk.
    paths = _requested_paths(client)
    assert len(paths) == 250
    assert len(set(paths)) == 250
    assert result.succeeded == 250
