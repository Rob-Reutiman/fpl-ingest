"""Pure mapping from raw FPL API JSON onto storage's column shapes.

``Storage`` accepts *schema-keyed* dicts and deliberately does no translation —
that job lives here. Everything in this module is synchronous and side-effect
free: no HTTP client, no database. That boundary is what lets the mapping tests
run on plain dicts with no mocks, and it is where the picks/transfers harvest
keeps its mappers.

Keys named ``gameweek`` are omitted from the row dicts wherever the
corresponding ``Storage`` method already stamps the gameweek itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "StandingsEntry",
    "detect_current_gw",
    "map_cohort_picks",
    "map_cohort_transfers",
    "map_fixtures",
    "map_my_picks",
    "map_player_gw_stats",
    "map_players",
    "map_standings_page",
    "map_teams",
    "standings_has_next",
]


@dataclass(frozen=True)
class StandingsEntry:
    """One row of a classic-league standings page.

    ``total_points`` is carried alongside the rank because ``cohort_manager``
    declares it NOT NULL and the value is free at scrape time — refetching it
    later would mean a second pass over every page.
    """

    manager_id: int
    rank: int
    total_points: int


# ---------------------------------------------------------------------------
# Coercion helpers
#
# The FPL API is loosely typed: several numeric fields arrive as strings, and
# optional ones arrive as empty strings rather than null.
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Coerce an API value to float. ``None``/``""``/unparseable become None.

    ``selected_by_percent`` and ``ep_next`` are delivered as strings ("24.1",
    "5.4") even though the columns are FLOAT.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_timestamp(value: Any) -> datetime | None:
    """Parse an FPL ISO-8601 timestamp into a *naive UTC* datetime.

    Two things make this fiddly:

    - The API emits a ``Z`` suffix, which ``datetime.fromisoformat`` cannot
      parse before Python 3.11. This project declares ``requires-python >=
      3.10``, so ``Z`` is rewritten to ``+00:00`` explicitly.
    - The target column is a naive ``TIMESTAMP``. Handing DuckDB a tz-aware
      datetime makes it convert the instant to the *local* timezone on write —
      silently shifting kickoff times by the UTC offset. Dropping the tzinfo
      after parsing keeps the stored value in UTC.

    Unscheduled fixtures have a null ``kickoff_time``.
    """
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# bootstrap-static/
# ---------------------------------------------------------------------------


def map_teams(teams: list[dict]) -> list[dict]:
    """Map ``bootstrap-static.teams`` onto ``dim_team`` rows."""
    return [
        {
            "id": team["id"],
            "name": team["name"],
            "short_name": team["short_name"],
        }
        for team in teams
    ]


def map_players(elements: list[dict], teams: list[dict]) -> list[dict]:
    """Map ``bootstrap-static.elements`` onto ``dim_player`` rows.

    ``teams`` is used to denormalise the club name onto each player, saving a
    join in the query layer. Note ``code`` (permanent, cross-season) and
    ``fpl_id`` (this season only) are different identifiers — picks join on
    ``fpl_id``, cross-season history joins on ``code``.
    """
    team_names = {team["id"]: team["name"] for team in teams}
    return [
        {
            "code": element["code"],
            "fpl_id": element["id"],
            "web_name": element["web_name"],
            "full_name": f"{element['first_name']} {element['second_name']}".strip(),
            "team": element["team"],
            "team_name": team_names.get(element["team"], ""),
            "position": element["element_type"],
            "now_cost": element["now_cost"],
            "news": element.get("news") or "",
        }
        for element in elements
    ]


def map_player_gw_stats(elements: list[dict]) -> list[dict]:
    """Map ``bootstrap-static.elements`` onto ``fact_player_gw`` rows.

    These counters are season-cumulative snapshots taken at run time, not
    per-fixture figures. ``gameweek`` is stamped by
    ``Storage.upsert_player_gw_stats``.
    """
    return [
        {
            "fpl_id": element["id"],
            "total_points": element["total_points"],
            "minutes": element["minutes"],
            "goals_scored": element["goals_scored"],
            "assists": element["assists"],
            "clean_sheets": element["clean_sheets"],
            "bps": element["bps"],
            "selected_by_percent": _as_float(element.get("selected_by_percent")),
            "transfers_in_event": element["transfers_in_event"],
            "transfers_out_event": element["transfers_out_event"],
            "ep_next": _as_float(element.get("ep_next")),
        }
        for element in elements
    ]


def detect_current_gw(events: list[dict]) -> int:
    """Return the gameweek to attribute this run's data to.

    The first event flagged ``is_current`` wins. Pre-season no event is current,
    so fall back to ``is_next``; if the season hasn't been published at all,
    fall back to GW 1.
    """
    for event in events:
        if event.get("is_current"):
            return event["id"]
    for event in events:
        if event.get("is_next"):
            return event["id"]
    return 1


# ---------------------------------------------------------------------------
# fixtures/
# ---------------------------------------------------------------------------


def map_fixtures(fixtures: list[dict]) -> list[dict]:
    """Map the ``fixtures/`` payload onto ``dim_fixture`` rows.

    ``event`` is null for fixtures not yet assigned to a gameweek (postponements
    awaiting a reschedule), and both scores are null until a match is played.
    """
    return [
        {
            "id": fixture["id"],
            "gameweek": fixture.get("event"),
            "team_h": fixture["team_h"],
            "team_a": fixture["team_a"],
            "team_h_score": fixture.get("team_h_score"),
            "team_a_score": fixture.get("team_a_score"),
            "kickoff_time": _as_timestamp(fixture.get("kickoff_time")),
            "finished": bool(fixture.get("finished", False)),
        }
        for fixture in fixtures
    ]


# ---------------------------------------------------------------------------
# entry/{id}/event/{gw}/picks/
# ---------------------------------------------------------------------------


def map_my_picks(picks: list[dict]) -> list[dict]:
    """Map a picks payload's ``picks`` list onto ``my_pick`` rows.

    ``my_pick`` is a slim table for the gap report: squad slot, vice-captaincy
    and the response's active chip have no columns and are dropped. ``gameweek``
    is stamped by ``Storage.upsert_my_picks``.
    """
    return [
        {
            "fpl_id": pick["element"],
            "multiplier": pick["multiplier"],
            "is_captain": bool(pick["is_captain"]),
        }
        for pick in picks
    ]


def map_cohort_picks(response: dict, manager_id: int, gameweek: int) -> list[dict]:
    """Map a full picks response onto ``cohort_pick`` rows.

    Unlike :func:`map_my_picks` this keeps the squad slot and vice-captaincy,
    and tags every row with ``active_chip`` from the response root — the chip
    applies to the whole squad, but denormalising it onto each row saves the
    query layer a join. ``gameweek`` and ``manager_id`` are stamped here
    because ``Storage.insert_picks`` writes rows verbatim.
    """
    active_chip = response.get("active_chip")
    return [
        {
            "gameweek": gameweek,
            "manager_id": manager_id,
            "fpl_id": pick["element"],
            "squad_position": pick["position"],
            "multiplier": pick["multiplier"],
            "is_captain": bool(pick["is_captain"]),
            "is_vice_captain": bool(pick["is_vice_captain"]),
            "active_chip": active_chip,
        }
        for pick in response.get("picks", [])
    ]


# ---------------------------------------------------------------------------
# entry/{id}/transfers/
# ---------------------------------------------------------------------------


def map_cohort_transfers(response: list[dict], target_gw: int | None = None) -> list[dict]:
    """Map a transfers response onto ``cohort_transfer`` rows.

    The endpoint returns every transfer this season; ``target_gw`` filters on
    the transfer's ``event`` (the gameweek it takes effect) so a harvest can
    store just the week it's responsible for. The manager is already present
    in each transfer as ``entry``, so no stamping argument is needed.
    """
    return [
        {
            "manager_id": row["entry"],
            "gameweek": row["event"],
            "fpl_id_in": row["element_in"],
            "fpl_id_out": row["element_out"],
            "cost_in": row["element_in_cost"],
            "cost_out": row["element_out_cost"],
            "transfer_time": _as_timestamp(row["time"]),
        }
        for row in response
        if target_gw is None or row["event"] == target_gw
    ]


# ---------------------------------------------------------------------------
# leagues-classic/{id}/standings/
# ---------------------------------------------------------------------------


def map_standings_page(page: dict) -> list[StandingsEntry]:
    """Extract the entries from one standings page.

    Pages past the end of the league return an empty ``results`` list rather
    than erroring, so a short or empty page is normal and yields no entries.
    """
    results = page.get("standings", {}).get("results", [])
    return [
        StandingsEntry(
            manager_id=row["entry"],
            rank=row["rank"],
            total_points=row["total"],
        )
        for row in results
    ]


def standings_has_next(page: dict) -> bool:
    """True if the API says another standings page follows this one.

    Lets the crawl stop at the end of a short league instead of requesting the
    full ``max_rank // 50`` pages regardless. Pre-season the overall league is
    empty and reports ``has_next: False`` on page 1 — without this a 100k pool
    would fire 2,000 requests to retrieve nothing.
    """
    return bool(page.get("standings", {}).get("has_next", False))
