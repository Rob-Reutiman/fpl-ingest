"""Job 2 — settlement detection, the no-op path, and partial tagging.

The curated output this job also writes is covered in
`test_gameweek_live_curated.py`; here the assertions are about *when* it runs and
what lands in the raw layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl.jobs import gameweek_live

from .conftest import FakeAPI, FakeStore, make_bootstrap, make_event, make_fixture

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LIVE_KEY = "raw/2026-27/gw{}/gameweek-live.json"

# A following gameweek's deadline this close means the settlement lead time has
# started; this far away means it hasn't.
NEAR_DEADLINE = (NOW + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
FAR_DEADLINE = (NOW + timedelta(days=6)).isoformat().replace("+00:00", "Z")


def test_ingests_the_earliest_settled_gameweek(api: FakeAPI, store: FakeStore, client):
    api.bootstrap = make_bootstrap(
        [
            make_event(1, finished=True, data_checked=True),
            make_event(2, finished=True, data_checked=True),
            make_event(3),
        ]
    )
    api.live = {1: {"elements": [{"id": 1, "stats": {"minutes": 90}}]}}

    assert gameweek_live.run(client, store, now=NOW) == 1

    put = store.objects[LIVE_KEY.format(1)]
    assert put.json == api.live[1]
    assert put.metadata == {}


def test_no_op_when_nothing_has_settled(api: FakeAPI, store: FakeStore, client):
    api.bootstrap = make_bootstrap(
        [make_event(1, finished=True), make_event(2, deadline_time=FAR_DEADLINE)]
    )
    api.fixtures = [make_fixture(i, event=1, finished=True) for i in range(1, 11)]

    assert gameweek_live.run(client, store, now=NOW) is None
    assert store.puts == []


def test_already_ingested_gameweek_is_not_refetched_or_rewritten(api: FakeAPI, client):
    api.bootstrap = make_bootstrap([make_event(1, finished=True, data_checked=True)])
    store = FakeStore(existing=[LIVE_KEY.format(1)])

    assert gameweek_live.run(client, store, now=NOW) is None
    assert store.puts == []
    assert api.count("/event/1/live/") == 0


def test_a_backlog_is_worked_off_one_gameweek_per_run(api: FakeAPI, store: FakeStore, client):
    api.bootstrap = make_bootstrap(
        [make_event(gw, finished=True, data_checked=True) for gw in (1, 2, 3)]
    )

    assert [gameweek_live.run(client, store, now=NOW) for _ in range(4)] == [1, 2, 3, None]
    assert [LIVE_KEY.format(gw) in store.objects for gw in (1, 2, 3)] == [True] * 3


def test_postponed_fixture_is_ingested_as_partial_at_the_unchanged_key(
    api: FakeAPI, store: FakeStore, client
):
    api.bootstrap = make_bootstrap(
        [
            make_event(3, finished=True, data_checked=False),
            make_event(4, deadline_time=NEAR_DEADLINE),
        ]
    )
    api.fixtures = [
        *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
        make_fixture(99, event=3, finished=False, kickoff_time="2026-11-01T15:00:00Z"),
    ]

    assert gameweek_live.run(client, store, now=NOW) == 3

    put = store.objects[LIVE_KEY.format(3)]  # same key a fully-checked GW would use
    assert put.metadata == {"partial": "true", "pending-fixtures": "99"}
    # The tag travels with the curated rows too, so a consumer can spot them.
    assert store.objects["curated/2026-27/fact_player_fixture.parquet"].metadata == put.metadata


def test_a_partial_ingest_is_not_repeated_on_the_next_run(api: FakeAPI, client):
    api.bootstrap = make_bootstrap([make_event(3, finished=True, data_checked=False)])
    api.fixtures = [
        make_fixture(1, event=3, finished=True),
        make_fixture(99, event=3, finished=False, kickoff_time=None),
    ]
    store = FakeStore(existing=[LIVE_KEY.format(3)])

    assert gameweek_live.run(client, store, now=NOW) is None
    assert store.puts == []
