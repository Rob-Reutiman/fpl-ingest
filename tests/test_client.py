"""Tests for Module 2 — FPL HTTP Client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from fpl.client import FPLClient, _should_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=data,
        request=httpx.Request("GET", "https://example.com"),
    )


BOOTSTRAP_STUB = {"elements": [], "teams": [], "events": []}
FIXTURES_STUB = [{"id": 1}]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_parsed_json(tmp_path: Path):
    """get() returns parsed JSON from a 200 response."""
    client = FPLClient(cache_dir=tmp_path / "cache", delay=0)
    async with client:
        client.set_transport(AsyncMock(return_value=_mock_response(BOOTSTRAP_STUB)))
        result = await client.get("bootstrap-static/")

    assert result == BOOTSTRAP_STUB
    assert "elements" in result
    assert "teams" in result
    assert "events" in result


@pytest.mark.asyncio
async def test_cache_hit_skips_network(tmp_path: Path):
    """Second call to a cacheable path reads from disk, not the network."""
    cache_dir = tmp_path / "cache"
    client = FPLClient(cache_dir=cache_dir, delay=0)

    async with client:
        mock_get = AsyncMock(return_value=_mock_response({"pick": 1}))
        client.set_transport(mock_get)

        # Picks paths are cacheable.
        path = "entry/12345/event/1/picks/"
        first = await client.get(path)
        second = await client.get(path)

    assert first == second == {"pick": 1}
    assert mock_get.call_count == 1  # Only one network call.


@pytest.mark.asyncio
async def test_non_cacheable_paths_always_fetch(tmp_path: Path):
    """bootstrap-static is NOT cacheable — always hits the network."""
    client = FPLClient(cache_dir=tmp_path / "cache", delay=0)

    async with client:
        mock_get = AsyncMock(return_value=_mock_response(BOOTSTRAP_STUB))
        client.set_transport(mock_get)

        await client.get("bootstrap-static/")
        await client.get("bootstrap-static/")

    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_404_raises_without_retry(tmp_path: Path):
    """A 404 should raise immediately — no retries for missing managers."""
    client = FPLClient(cache_dir=tmp_path / "cache", delay=0, retry_attempts=3)

    async with client:
        mock_get = AsyncMock(return_value=_mock_response({}, status=404))
        client.set_transport(mock_get)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get("entry/99999999999/event/1/picks/")

    assert exc_info.value.response.status_code == 404
    assert mock_get.call_count == 1  # No retries.


@pytest.mark.asyncio
async def test_5xx_retries(tmp_path: Path):
    """5xx responses should be retried up to retry_attempts times."""
    client = FPLClient(cache_dir=tmp_path / "cache", delay=0, retry_attempts=3)

    async with client:
        mock_get = AsyncMock(return_value=_mock_response({}, status=503))
        client.set_transport(mock_get)

        with pytest.raises(httpx.HTTPStatusError):
            await client.get("bootstrap-static/")

    assert mock_get.call_count == 3


@pytest.mark.asyncio
async def test_get_many_concurrent(tmp_path: Path):
    """get_many returns results for all paths."""
    client = FPLClient(cache_dir=tmp_path / "cache", delay=0)

    call_count = 0

    async def _route(url: str, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if "fixtures" in str(url):
            return _mock_response(FIXTURES_STUB)
        return _mock_response(BOOTSTRAP_STUB)

    async with client:
        client.set_transport(AsyncMock(side_effect=_route))
        results = await client.get_many(["fixtures/", "bootstrap-static/"])

    assert len(results) == 2
    assert call_count == 2


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency(tmp_path: Path):
    """Under max_concurrent=2, no more than 2 requests run simultaneously."""
    max_concurrent = 2
    client = FPLClient(cache_dir=tmp_path / "cache", delay=0, max_concurrent=max_concurrent)

    peak = 0
    active = 0
    lock = asyncio.Lock()

    async def _slow_get(url: str, **kwargs) -> httpx.Response:
        nonlocal peak, active
        async with lock:
            active += 1
            if active > peak:
                peak = active
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return _mock_response({"ok": True})

    async with client:
        client.set_transport(AsyncMock(side_effect=_slow_get))
        # Use non-cacheable paths so every request hits the network.
        paths = [f"test/{i}/" for i in range(10)]
        await client.get_many(paths)

    assert peak <= max_concurrent


@pytest.mark.asyncio
async def test_clear_cache(tmp_path: Path):
    """clear_cache removes all cached files."""
    cache_dir = tmp_path / "cache"
    client = FPLClient(cache_dir=cache_dir, delay=0)

    async with client:
        client.set_transport(AsyncMock(return_value=_mock_response({"x": 1})))
        await client.get("entry/1/event/1/picks/")
        await client.get("entry/2/event/1/picks/")

    assert len(list(cache_dir.glob("*.json"))) == 2
    removed = client.clear_cache()
    assert removed == 2
    assert len(list(cache_dir.glob("*.json"))) == 0


def test_should_cache_logic():
    """Verify which URL patterns are considered cacheable."""
    assert _should_cache("entry/123/event/1/picks/") is True
    assert _should_cache("entry/123/transfers/") is True
    assert _should_cache("bootstrap-static/") is False
    assert _should_cache("fixtures/") is False
