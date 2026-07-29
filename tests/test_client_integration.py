"""Live integration tests for FPLClient — hit the real FPL API.

These are EXCLUDED from the default test run and CI. Run manually with:

    pytest -m integration

They verify that the client works against the real Cloudflare-fronted API:
the User-Agent is accepted, the endpoints still return the expected shapes,
and the disk cache round-trips real responses. Keep them few and light —
they make real network calls and should not hammer the API.
"""

from __future__ import annotations

import httpx
import pytest

from fpl.client import FPLClient

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_bootstrap_static_shape():
    """The bootstrap endpoint returns the core keys we depend on."""
    async with FPLClient() as client:
        data = await client.get("bootstrap-static/")

    assert isinstance(data, dict)
    for key in ("elements", "teams", "events"):
        assert key in data, f"missing key: {key}"
    assert len(data["elements"]) > 0
    assert len(data["teams"]) == 20


@pytest.mark.asyncio
async def test_fixtures_shape():
    """The fixtures endpoint returns a non-empty list of fixture dicts."""
    async with FPLClient() as client:
        data = await client.get("fixtures/")

    assert isinstance(data, list)
    assert len(data) > 0
    assert "team_h" in data[0]
    assert "team_a" in data[0]


@pytest.mark.asyncio
async def test_get_many_concurrent_live():
    """get_many fetches multiple live endpoints concurrently."""
    async with FPLClient() as client:
        bootstrap, fixtures = await client.get_many(["bootstrap-static/", "fixtures/"])

    assert "elements" in bootstrap
    assert isinstance(fixtures, list)


@pytest.mark.asyncio
async def test_picks_cache_round_trip(tmp_path):
    """A real picks response is cached and served from disk on the second call.

    Skips when picks aren't available yet — pre-season the current GW has no
    picks, so the endpoint 404s. This isn't a client failure, so we skip
    rather than fail.
    """
    path = "entry/1/event/1/picks/"
    async with FPLClient(cache_dir=tmp_path / "cache") as client:
        try:
            first = await client.get(path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                pytest.skip("picks not available (likely pre-season)")
            raise

        cache_files = list((tmp_path / "cache").glob("*.json"))
        assert cache_files, "expected a cache file to be written"

        second = await client.get(path)

    assert first == second
    assert "picks" in first
