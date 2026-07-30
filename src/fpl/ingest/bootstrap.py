"""Module 4 — bootstrap ingestion.

Fetches the static reference data every other module depends on: players,
teams, fixtures, and a snapshot of per-player stats for the current gameweek.
Run once at the top of the weekly pre-deadline workflow.

This module is deliberately thin — all raw-JSON translation lives in
:mod:`fpl.ingest.mappers`, and all persistence in :class:`fpl.storage.Storage`.
What's left here is fetch order and orchestration.
"""

from __future__ import annotations

import logging

from fpl.client import FPLClient
from fpl.ingest.mappers import (
    detect_current_gw,
    map_fixtures,
    map_my_picks,
    map_player_gw_stats,
    map_players,
    map_teams,
)
from fpl.storage import Storage

logger = logging.getLogger(__name__)


async def ingest_bootstrap(client: FPLClient, storage: Storage) -> int:
    """Fetch bootstrap-static and fixtures, write them to storage.

    Returns the detected current gameweek. The spec sketches this as returning
    ``None``, but callers need the gameweek to drive :func:`ingest_my_team` and
    cohort ingestion, and ``bootstrap-static/`` is not disk-cached — re-deriving
    it would mean a second full fetch of a large payload.

    Safe to re-run: every write is an upsert keyed on a primary key.
    """
    data: dict = await client.get("bootstrap-static/")

    teams: list[dict] = data["teams"]
    elements: list[dict] = data["elements"]

    # Teams first, so dim_player.team always has a referent present.
    storage.upsert_teams(map_teams(teams))
    storage.upsert_players(map_players(elements, teams))

    current_gw = detect_current_gw(data["events"])
    storage.upsert_player_gw_stats(current_gw, map_player_gw_stats(elements))

    # Unlike every other endpoint, fixtures/ returns a bare JSON array.
    fixtures: list[dict] = await client.get("fixtures/")
    storage.upsert_fixtures(map_fixtures(fixtures))

    logger.info(
        "bootstrap: gw=%d players=%d teams=%d fixtures=%d",
        current_gw,
        len(elements),
        len(teams),
        len(fixtures),
    )
    return current_gw


async def ingest_my_team(
    client: FPLClient,
    storage: Storage,
    manager_id: int,
    gw: int,
) -> None:
    """Fetch my picks for ``gw`` and store them.

    The picks endpoint is disk-cached by the client, so re-running a completed
    gameweek costs no network call.
    """
    data: dict = await client.get(f"entry/{manager_id}/event/{gw}/picks/")
    picks = map_my_picks(data["picks"])
    storage.upsert_my_picks(gw, picks)
    logger.info("my team: gw=%d manager=%d picks=%d", gw, manager_id, len(picks))
