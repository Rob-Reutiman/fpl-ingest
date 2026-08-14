"""Throttling, retry policy and raw-body fidelity."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from tenacity import wait_none

from fpl.constants import RETRY_ATTEMPTS, USER_AGENT
from fpl.fpl_client import FPLClient

from .conftest import FakeAPI, make_bootstrap


def _client(handler, **kwargs) -> FPLClient:
    kwargs.setdefault("delay", 0)
    kwargs.setdefault("retry_wait", wait_none())
    return FPLClient(transport=httpx.MockTransport(handler), **kwargs)


def test_returns_the_response_body_byte_for_byte():
    api = FakeAPI(make_bootstrap([], season="2026_27"))
    with _client(api.handler) as client:
        raw = client.bootstrap_static()

    assert isinstance(raw, bytes)
    assert json.loads(raw) == api.bootstrap


def test_sends_a_descriptive_user_agent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json={})

    with _client(handler) as client:
        client.bootstrap_static()

    assert seen == [USER_AGENT]
    assert "fpl-ingest" in USER_AGENT


def test_endpoints_build_the_documented_urls():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    with _client(handler) as client:
        client.fixtures()
        client.fixtures(event=3)
        client.event_live(3)
        client.standings_page(4)
        client.entry_picks(999, 3)

    assert seen == [
        "https://fantasy.premierleague.com/api/fixtures/",
        "https://fantasy.premierleague.com/api/fixtures/?event=3",
        "https://fantasy.premierleague.com/api/event/3/live/",
        "https://fantasy.premierleague.com/api/leagues-classic/314/standings/?page_standings=4",
        "https://fantasy.premierleague.com/api/entry/999/event/3/picks/",
    ]


# -- Retry policy -------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_are_retried_to_the_attempt_limit(status: int):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={})

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.bootstrap_static()

    assert calls == RETRY_ATTEMPTS


def test_a_404_fails_immediately_without_retrying():
    """A missing entry or a gameweek that hasn't opened is not transient."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"detail": "Not found."})

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        client.entry_picks(1, 1)

    assert calls == 1
    assert excinfo.value.response.status_code == 404


def test_recovers_when_a_transient_failure_clears():
    statuses = [503, 500]

    def handler(request: httpx.Request) -> httpx.Response:
        if statuses:
            return httpx.Response(statuses.pop(0), json={})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        assert json.loads(client.bootstrap_static()) == {"ok": True}


def test_connection_errors_are_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < RETRY_ATTEMPTS:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        assert json.loads(client.bootstrap_static()) == {"ok": True}
    assert calls == RETRY_ATTEMPTS


# -- Throttle -----------------------------------------------------------------


def test_requests_are_spaced_by_the_configured_delay():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    delay = 0.05
    with _client(handler, delay=delay) as client:
        start = time.monotonic()
        for _ in range(3):
            client.bootstrap_static()
        elapsed = time.monotonic() - start

    # Three requests means two enforced gaps; the first is free.
    assert elapsed >= 2 * delay


def test_using_the_client_outside_its_context_manager_is_an_error():
    with pytest.raises(RuntimeError, match="context manager"):
        _client(lambda request: httpx.Response(200, json={})).bootstrap_static()
