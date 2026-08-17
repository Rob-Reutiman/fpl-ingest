"""Test doubles for the FPL API and the R2 bucket.

Nothing here touches the network: `FakeAPI` is served through an
`httpx.MockTransport`, so the jobs exercise the real `FPLClient` — throttling,
retries and all — against canned responses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from tenacity import wait_none

from fpl.constants import ENTRIES_PER_PAGE
from fpl.fpl_client import FPLClient
from fpl.r2_client import JSON_CONTENT_TYPE, NDJSON_CONTENT_TYPE, _to_ndjson

SEASON = "2026-27"

_LIVE_RE = re.compile(r"/event/(\d+)/live/$")
_PICKS_RE = re.compile(r"/entry/(\d+)/event/(\d+)/picks/$")
_SUMMARY_RE = re.compile(r"/element-summary/(\d+)/$")


# -- Response builders --------------------------------------------------------


# Default deadlines count forward from here, a fixed point safely before any
# `now` a test might use and before the real clock this suite ever runs under.
# A gameweek reads as already started unless a test overrides `deadline_time`.
_DEADLINE_ANCHOR = datetime(2000, 1, 1, 17, 30, tzinfo=UTC)


def make_event(
    gw: int,
    *,
    finished: bool = False,
    data_checked: bool = False,
    deadline_time: str | None = None,
) -> dict[str, Any]:
    """An `events[]` entry. The scoring fields are null until a gameweek starts,
    which is exactly how FPL sends them."""
    if deadline_time is None:
        deadline = _DEADLINE_ANCHOR + timedelta(weeks=gw)
        deadline_time = deadline.isoformat().replace("+00:00", "Z")
    else:
        parsed = datetime.fromisoformat(deadline_time)
        deadline = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return {
        "id": gw,
        "name": f"Gameweek {gw}",
        "finished": finished,
        "data_checked": data_checked,
        "deadline_time": deadline_time,
        "deadline_time_epoch": int(deadline.timestamp()),
        "average_entry_score": 50 if finished else 0,
        "highest_score": 120 if finished else None,
        "most_selected": 1 if finished else None,
        "most_transferred_in": 2 if finished else None,
        "most_captained": 1 if finished else None,
    }


# Three-letter codes are the stable team master ids, so the fakes use real ones.
TEAM_SHORT_NAMES = (
    "ARS", "AVL", "BOU", "BRE", "BHA", "BUR", "CHE", "CRY", "EVE", "FUL",
    "LEE", "LIV", "MCI", "MUN", "NEW", "NFO", "SUN", "TOT", "WHU", "WOL",
)  # fmt: skip


def make_team(team_id: int) -> dict[str, Any]:
    return {
        "id": team_id,
        "code": 100 + team_id,
        "name": f"Team {team_id}",
        "short_name": TEAM_SHORT_NAMES[team_id - 1],
        "strength": 3,
        "strength_overall_home": 1200,
        "strength_overall_away": 1200,
        "strength_attack_home": 1200,
        "strength_attack_away": 1200,
        "strength_defence_home": 1200,
        "strength_defence_away": 1200,
    }


def make_element(
    element_id: int,
    *,
    team: int = 1,
    element_type: int = 3,
    code: int | None = None,
    web_name: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A `bootstrap-static.elements[]` entry.

    Several numeric fields arrive as strings from the real API — `form`,
    `points_per_game`, `ep_this`, `selected_by_percent` — so the fake sends them
    that way too, or the casts in the transform would go untested.
    """
    element = {
        "id": element_id,
        "code": 500_000 + element_id if code is None else code,
        "first_name": f"First{element_id}",
        "second_name": f"Last{element_id}",
        "web_name": web_name or f"Player{element_id}",
        "element_type": element_type,
        "team": team,
        "now_cost": 50 + element_id,
        "cost_change_event": 0,
        "cost_change_start": 1,
        "selected_by_percent": "12.5",
        "transfers_in_event": 100,
        "transfers_out_event": 50,
        "status": "a",
        "news": "",
        "news_added": None,
        "chance_of_playing_this_round": None,
        "chance_of_playing_next_round": None,
        "form": "3.4",
        "points_per_game": "4.1",
        "ep_this": "5.2",
        "ep_next": "5.8",
        "total_points": 40,
        "minutes": 900,
    }
    element.update(overrides)
    return element


def make_bootstrap(
    events: list[dict[str, Any]],
    *,
    season: str = "2026_27",
    elements: list[dict[str, Any]] | None = None,
    teams: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "game_config": {
            "settings": {
                "static_content_url": f"https://fantasy.premierleague.com/img/static/{season}/"
            }
        },
        "events": events,
        "teams": teams if teams is not None else [make_team(i) for i in range(1, 21)],
        "elements": elements
        if elements is not None
        else [make_element(1), make_element(2, team=2)],
    }


def make_fixture(
    fixture_id: int,
    *,
    event: int | None = 1,
    finished: bool = True,
    kickoff_time: str | None = None,
    team_h: int = 1,
    team_a: int = 2,
    team_h_score: int | None = 1,
    team_a_score: int | None = 0,
) -> dict[str, Any]:
    return {
        "id": fixture_id,
        "event": event,
        "finished": finished,
        "finished_provisional": finished,
        "kickoff_time": kickoff_time,
        "team_h": team_h,
        "team_a": team_a,
        "team_h_score": team_h_score,
        "team_a_score": team_a_score,
        "team_h_difficulty": 3,
        "team_a_difficulty": 2,
    }


def make_standings_page(page: int) -> dict[str, Any]:
    """A standings page whose entry ids encode their rank, so tests can assert
    which slice of the leaderboard a sample was drawn from."""
    first_rank = (page - 1) * ENTRIES_PER_PAGE + 1
    return {
        "league": {"id": 314, "name": "Overall"},
        "standings": {
            "has_next": page < 200,
            "page": page,
            "results": [
                {
                    "entry": 100_000 + rank,
                    "rank": rank,
                    "total": 2000 - rank,
                    "entry_name": f"Team {rank}",
                }
                for rank in range(first_rank, first_rank + ENTRIES_PER_PAGE)
            ],
        },
    }


def make_live_stats(**overrides: Any) -> dict[str, Any]:
    """A player's `stats` object from `event/{gw}/live/`.

    The xG family and the ICT family arrive as decimal *strings*; the counting
    stats as integers. The fake mirrors that so the casts get exercised.
    """
    stats = {
        "minutes": 90,
        "starts": 1,
        "goals_scored": 0,
        "assists": 0,
        "expected_goals": "0.35",
        "expected_assists": "0.12",
        "expected_goal_involvements": "0.47",
        "clean_sheets": 0,
        "goals_conceded": 1,
        "expected_goals_conceded": "1.10",
        "saves": 0,
        "penalties_saved": 0,
        "defensive_contribution": 8,
        "yellow_cards": 0,
        "red_cards": 0,
        "own_goals": 0,
        "penalties_missed": 0,
        "bps": 20,
        "bonus": 0,
        "total_points": 2,
        "influence": "10.2",
        "creativity": "5.5",
        "threat": "12.0",
        "ict_index": "2.8",
    }
    stats.update(overrides)
    return stats


def make_live(elements: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """`event/{gw}/live/` — every player in the game, whether their club played
    or not. Filtering the blanks out is the transform's job."""
    return {
        "elements": [
            {"id": element_id, "stats": stats, "explain": []}
            for element_id, stats in sorted(elements.items())
        ]
    }


def make_history(
    fixture_id: int,
    *,
    gw: int,
    opponent_team: int,
    was_home: bool,
    value: int = 50,
    **stats: Any,
) -> dict[str, Any]:
    """One `element-summary.history[]` entry: genuinely per-fixture, with xG."""
    return {
        "fixture": fixture_id,
        "round": gw,
        "opponent_team": opponent_team,
        "was_home": was_home,
        "value": value,
        "kickoff_time": f"2026-08-{10 + gw:02d}T14:00:00Z",
        **make_live_stats(**stats),
    }


def make_picks(entry_id: int, gw: int) -> dict[str, Any]:
    return {
        "active_chip": None,
        "entry_history": {"event": gw, "points": 50, "rank": 1},
        "picks": [
            {"element": i, "position": i, "multiplier": 1, "is_captain": i == 1}
            for i in range(1, 16)
        ],
        "_entry": entry_id,
    }


# -- Fake API -----------------------------------------------------------------


class FakeAPI:
    """Routes FPL request paths to canned JSON and records every call."""

    def __init__(self, bootstrap: dict[str, Any] | None = None) -> None:
        self.bootstrap = bootstrap if bootstrap is not None else make_bootstrap([])
        # A real season always has a schedule, so the default is a coherent one:
        # tests about settlement logic shouldn't have to invent fixtures they
        # don't care about, and an empty list isn't a state that can occur.
        self.fixtures: list[dict[str, Any]] = [
            make_fixture(100 + gw, event=gw, team_h=1, team_a=2) for gw in (1, 2, 3)
        ]
        self.live: dict[int, Any] = {}
        # element_id -> element-summary body, for the double-gameweek fallback.
        self.summaries: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []
        # League 314 reads empty until the season's first gameweek settles.
        self.empty_standings = False
        # Paths matching a key here fail with the queued statuses first.
        self.transient_failures: dict[str, list[int]] = {}
        self.permanent_failures: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(str(request.url))

        for fragment, status in self.permanent_failures.items():
            if fragment in str(request.url):
                return httpx.Response(status, json={"detail": "nope"})

        for fragment, statuses in self.transient_failures.items():
            if fragment in str(request.url) and statuses:
                return httpx.Response(statuses.pop(0), json={"detail": "transient"})

        return httpx.Response(200, json=self._body(path, request.url.params))

    def _body(self, path: str, params: httpx.QueryParams) -> Any:
        if path.endswith("/bootstrap-static/"):
            return self.bootstrap
        if path.endswith("/fixtures/"):
            event = params.get("event")
            if event is None:
                return self.fixtures
            return [f for f in self.fixtures if f.get("event") == int(event)]
        if path.endswith("/standings/"):
            page = int(params.get("page_standings", 1))
            if self.empty_standings:
                return {"standings": {"has_next": False, "page": page, "results": []}}
            return make_standings_page(page)
        if match := _LIVE_RE.search(path):
            gw = int(match.group(1))
            return self.live.get(gw, {"elements": [], "_gw": gw})
        if match := _SUMMARY_RE.search(path):
            element_id = int(match.group(1))
            return self.summaries.get(element_id, {"history": [], "fixtures": []})
        if match := _PICKS_RE.search(path):
            return make_picks(int(match.group(1)), int(match.group(2)))
        raise AssertionError(f"unrouted request: {path}")

    def count(self, fragment: str) -> int:
        return sum(1 for url in self.calls if fragment in url)


# -- Fake object store --------------------------------------------------------


class Put:
    def __init__(self, key: str, body: bytes, content_type: str, metadata: dict[str, str]) -> None:
        self.key = key
        self.body = body
        self.content_type = content_type
        self.metadata = metadata

    @property
    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def lines(self) -> list[Any]:
        return [json.loads(line) for line in self.body.decode().splitlines() if line]


class FakeStore:
    """Records writes and answers existence checks from what it holds."""

    def __init__(self, existing: Iterable[str] = ()) -> None:
        self.objects: dict[str, Put] = {}
        self.puts: list[Put] = []
        self.exists_calls: list[str] = []
        for key in existing:
            self.objects[key] = Put(key, b"{}", JSON_CONTENT_TYPE, {})

    def exists(self, key: str) -> bool:
        self.exists_calls.append(key)
        return key in self.objects

    def get_bytes(self, key: str) -> bytes | None:
        put = self.objects.get(key)
        return put.body if put else None

    def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = JSON_CONTENT_TYPE,
        metadata: dict[str, str] | None = None,
    ) -> None:
        put = Put(key, body, content_type, metadata or {})
        self.objects[key] = put
        self.puts.append(put)

    def put_json(self, key: str, obj: Any, *, metadata: dict[str, str] | None = None) -> None:
        self.put_bytes(key, json.dumps(obj).encode(), metadata=metadata)

    def put_ndjson(
        self, key: str, records: Iterable[Any], *, metadata: dict[str, str] | None = None
    ) -> None:
        self.put_bytes(
            key, _to_ndjson(records), content_type=NDJSON_CONTENT_TYPE, metadata=metadata
        )

    @property
    def keys(self) -> list[str]:
        return [put.key for put in self.puts]


# -- Reading curated output ---------------------------------------------------


def relation_rows(relation: duckdb.DuckDBPyRelation) -> list[dict[str, Any]]:
    """A DuckDB relation as dicts, so assertions name columns rather than index them."""
    columns = relation.columns
    return [dict(zip(columns, row, strict=True)) for row in relation.fetchall()]


def one_row(relation: duckdb.DuckDBPyRelation) -> dict[str, Any]:
    rows = relation_rows(relation)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


def column_types(relation: duckdb.DuckDBPyRelation) -> tuple[tuple[str, str], ...]:
    """The relation's schema in the shape `curated_schema.COLUMNS` uses."""
    return tuple(
        (name, str(dtype)) for name, dtype in zip(relation.columns, relation.types, strict=True)
    )


def read_curated(store: FakeStore, key: str, tmp_path: Path) -> duckdb.DuckDBPyRelation:
    """Read a Parquet object back out of the fake bucket."""
    path = tmp_path / key.replace("/", "_")
    body = store.objects[key].body
    path.write_bytes(body)
    return duckdb.connect().sql(f"SELECT * FROM read_parquet('{path.as_posix()}')")


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(api: FakeAPI):
    """A real FPLClient wired to the fake API, with throttling and backoff off."""
    with FPLClient(
        delay=0,
        retry_wait=wait_none(),
        transport=httpx.MockTransport(api.handler),
    ) as fpl_client:
        yield fpl_client
