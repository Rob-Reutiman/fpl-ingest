"""Live, read-only checks against the real FPL API.

Excluded from default runs — `pytest -m integration`. Nothing here writes to R2.
Their job is to catch the API drifting away from the assumptions the mocked
suite encodes, above all the `static_content_url` field the whole season
prefix depends on.
"""

from __future__ import annotations

import json
import re

import pytest

from fpl.constants import ENTRIES_PER_PAGE
from fpl.fpl_client import FPLClient
from fpl.gameweek import settled_gameweeks
from fpl.season import derive_season

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with FPLClient() as fpl_client:
        yield fpl_client


@pytest.fixture(scope="module")
def bootstrap(client: FPLClient) -> dict:
    return json.loads(client.bootstrap_static())


def test_season_is_still_derivable_from_the_live_response(bootstrap: dict):
    assert re.fullmatch(r"\d{2}-\d{2}", derive_season(bootstrap))


def test_events_carry_the_fields_the_detector_relies_on(bootstrap: dict):
    events = bootstrap["events"]
    assert len(events) >= 38
    for event in events:
        assert {"id", "finished", "data_checked"} <= event.keys()
    settled_gameweeks(events)  # must not raise


def test_fixtures_expose_finished_and_kickoff_time(client: FPLClient):
    fixtures = json.loads(client.fixtures())
    assert isinstance(fixtures, list) and fixtures
    for fixture in fixtures[:20]:
        assert {"id", "event", "finished", "kickoff_time"} <= fixture.keys()


def test_overall_league_standings_page_shape(client: FPLClient):
    page = json.loads(client.standings_page(1))
    results = page["standings"]["results"]
    if not results:
        # League 314 is empty until the first gameweek settles — which is
        # exactly why the manager sample waits for `data_checked`.
        pytest.skip("overall standings are empty pre-season")
    assert len(results) == ENTRIES_PER_PAGE
    assert {"entry", "rank"} <= results[0].keys()
    assert results[0]["rank"] == 1


def test_live_stats_for_a_settled_gameweek(client: FPLClient, bootstrap: dict):
    settled = settled_gameweeks(bootstrap["events"])
    if not settled:
        pytest.skip("no gameweek has settled yet this season")
    live = json.loads(client.event_live(settled[0]))
    assert live["elements"]
    assert "stats" in live["elements"][0]
