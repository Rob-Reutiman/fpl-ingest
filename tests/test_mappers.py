"""Tests for the pure mapping layer — no mocks, no database."""

from __future__ import annotations

from datetime import datetime

from fpl.ingest.mappers import (
    StandingsEntry,
    detect_current_gw,
    map_fixtures,
    map_my_picks,
    map_player_gw_stats,
    map_players,
    map_standings_page,
    map_teams,
    standings_has_next,
)

# ---------------------------------------------------------------------------
# Raw-API shaped fixtures
# ---------------------------------------------------------------------------

RAW_TEAMS = [
    {"id": 12, "name": "Liverpool", "short_name": "LIV"},
    {"id": 13, "name": "Man City", "short_name": "MCI"},
]

RAW_ELEMENT = {
    "id": 328,
    "code": 118748,
    "web_name": "Salah",
    "first_name": "Mohamed",
    "second_name": "Salah",
    "team": 12,
    "element_type": 3,
    "now_cost": 130,
    "news": "",
    "total_points": 211,
    "minutes": 3103,
    "goals_scored": 29,
    "assists": 18,
    "clean_sheets": 12,
    "bps": 967,
    "selected_by_percent": "24.1",  # String in the real API.
    "transfers_in_event": 41234,
    "transfers_out_event": 9876,
    "ep_next": "5.4",  # Also a string.
}


# ---------------------------------------------------------------------------
# Teams / players
# ---------------------------------------------------------------------------


def test_map_teams():
    assert map_teams(RAW_TEAMS) == [
        {"id": 12, "name": "Liverpool", "short_name": "LIV"},
        {"id": 13, "name": "Man City", "short_name": "MCI"},
    ]


def test_map_players_key_mapping():
    """code and fpl_id are distinct IDs; full_name joins; team_name resolves."""
    (row,) = map_players([RAW_ELEMENT], RAW_TEAMS)
    assert row["code"] == 118748
    assert row["fpl_id"] == 328
    assert row["code"] != row["fpl_id"]
    assert row["web_name"] == "Salah"
    assert row["full_name"] == "Mohamed Salah"
    assert row["team"] == 12
    assert row["team_name"] == "Liverpool"
    assert row["position"] == 3
    assert row["now_cost"] == 130
    assert row["news"] == ""


def test_map_players_null_news_becomes_empty_string():
    (row,) = map_players([{**RAW_ELEMENT, "news": None}], RAW_TEAMS)
    assert row["news"] == ""


def test_map_players_unknown_team_does_not_raise():
    """A player on a team missing from the teams list still maps."""
    (row,) = map_players([{**RAW_ELEMENT, "team": 99}], RAW_TEAMS)
    assert row["team"] == 99
    assert row["team_name"] == ""


# ---------------------------------------------------------------------------
# Per-GW stats — string coercion
# ---------------------------------------------------------------------------


def test_map_player_gw_stats_coerces_string_floats():
    """selected_by_percent and ep_next arrive as strings; columns are FLOAT."""
    (row,) = map_player_gw_stats([RAW_ELEMENT])
    assert row["selected_by_percent"] == 24.1
    assert isinstance(row["selected_by_percent"], float)
    assert row["ep_next"] == 5.4
    assert isinstance(row["ep_next"], float)


def test_map_player_gw_stats_omits_gameweek():
    """Storage.upsert_player_gw_stats stamps the gameweek — don't duplicate it."""
    (row,) = map_player_gw_stats([RAW_ELEMENT])
    assert "gameweek" not in row


def test_map_player_gw_stats_handles_empty_and_null_floats():
    (row,) = map_player_gw_stats([{**RAW_ELEMENT, "ep_next": "", "selected_by_percent": None}])
    assert row["ep_next"] is None
    assert row["selected_by_percent"] is None


def test_map_player_gw_stats_carries_transfer_counters():
    (row,) = map_player_gw_stats([RAW_ELEMENT])
    assert row["transfers_in_event"] == 41234
    assert row["transfers_out_event"] == 9876


# ---------------------------------------------------------------------------
# Fixtures — timestamp parsing
# ---------------------------------------------------------------------------


def test_map_fixtures_parses_kickoff_time_as_naive_utc():
    """The Z suffix must survive py310 parsing and stay in UTC.

    A tz-aware datetime handed to DuckDB gets converted to local time on write,
    silently shifting kickoff by the UTC offset — so the result must be naive
    and still read 19:00.
    """
    (row,) = map_fixtures(
        [
            {
                "id": 1,
                "event": 1,
                "team_h": 12,
                "team_a": 13,
                "team_h_score": 2,
                "team_a_score": 1,
                "kickoff_time": "2025-08-16T19:00:00Z",
                "finished": True,
            }
        ]
    )
    assert row["kickoff_time"] == datetime(2025, 8, 16, 19, 0)
    assert row["kickoff_time"].tzinfo is None
    assert row["gameweek"] == 1
    assert row["finished"] is True


def test_map_fixtures_handles_nulls():
    """Pre-season: no scores, and an unscheduled fixture has no kickoff or GW."""
    (row,) = map_fixtures(
        [
            {
                "id": 2,
                "event": None,
                "team_h": 12,
                "team_a": 13,
                "team_h_score": None,
                "team_a_score": None,
                "kickoff_time": None,
                "finished": False,
            }
        ]
    )
    assert row["gameweek"] is None
    assert row["team_h_score"] is None
    assert row["kickoff_time"] is None
    assert row["finished"] is False


# ---------------------------------------------------------------------------
# Current gameweek detection
# ---------------------------------------------------------------------------


def _events(current: int | None, next_: int | None) -> list[dict]:
    return [{"id": gw, "is_current": gw == current, "is_next": gw == next_} for gw in range(1, 39)]


def test_detect_current_gw_prefers_is_current():
    assert detect_current_gw(_events(current=7, next_=8)) == 7


def test_detect_current_gw_falls_back_to_is_next():
    """Pre-season no event is current."""
    assert detect_current_gw(_events(current=None, next_=1)) == 1


def test_detect_current_gw_defaults_to_one():
    assert detect_current_gw(_events(current=None, next_=None)) == 1
    assert detect_current_gw([]) == 1


# ---------------------------------------------------------------------------
# Picks / standings
# ---------------------------------------------------------------------------


def test_map_my_picks_drops_columns_the_table_lacks():
    raw = [
        {
            "element": 328,
            "position": 11,
            "multiplier": 2,
            "is_captain": True,
            "is_vice_captain": False,
        }
    ]
    (row,) = map_my_picks(raw)
    assert row == {"fpl_id": 328, "multiplier": 2, "is_captain": True}


def test_map_standings_page():
    page = {
        "standings": {
            "results": [
                {"entry": 1001, "rank": 1, "total": 2534, "player_name": "A"},
                {"entry": 1002, "rank": 2, "total": 2530, "player_name": "B"},
            ]
        }
    }
    assert map_standings_page(page) == [
        StandingsEntry(manager_id=1001, rank=1, total_points=2534),
        StandingsEntry(manager_id=1002, rank=2, total_points=2530),
    ]


def test_map_standings_page_tolerates_empty_page():
    """A page past the end of the league returns empty results, not an error."""
    assert map_standings_page({"standings": {"results": []}}) == []
    assert map_standings_page({}) == []


def test_standings_has_next():
    assert standings_has_next({"standings": {"has_next": True, "results": []}}) is True
    assert standings_has_next({"standings": {"has_next": False, "results": []}}) is False


def test_standings_has_next_defaults_false():
    """Absent key must stop the crawl, not run it to the full page count."""
    assert standings_has_next({"standings": {}}) is False
    assert standings_has_next({}) is False
