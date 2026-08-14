"""Which gameweek is ready to ingest, and when a postponement overrides that."""

from __future__ import annotations

from datetime import UTC, datetime

from fpl.gameweek import is_effectively_complete, partial_metadata, resolve_target

from .conftest import make_event, make_fixture

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SOON = "2026-09-01T19:00:00Z"
NEXT_MONTH = "2026-10-14T19:00:00Z"


def _resolve(events, *, ingested=(), fixtures=None):
    calls: list[int] = []

    def fetch(gw: int):
        calls.append(gw)
        return (fixtures or {}).get(gw, [])

    target = resolve_target(
        events,
        already_ingested=lambda gw: gw in ingested,
        fetch_fixtures=fetch,
        now=NOW,
    )
    return target, calls


# -- resolve_target -----------------------------------------------------------


def test_picks_the_earliest_settled_gameweek():
    events = [
        make_event(1, finished=True, data_checked=True),
        make_event(2, finished=True, data_checked=True),
    ]
    target, _ = _resolve(events)
    assert target is not None
    assert (target.gw, target.partial) == (1, False)


def test_skips_gameweeks_already_in_the_bucket():
    events = [
        make_event(1, finished=True, data_checked=True),
        make_event(2, finished=True, data_checked=True),
    ]
    target, _ = _resolve(events, ingested={1})
    assert target is not None and target.gw == 2


def test_no_op_when_everything_is_ingested():
    events = [make_event(1, finished=True, data_checked=True)]
    target, _ = _resolve(events, ingested={1})
    assert target is None


def test_no_op_before_any_gameweek_finishes():
    events = [make_event(1), make_event(2)]
    target, calls = _resolve(events)
    assert target is None
    assert calls == []  # unfinished events never cost a fixtures request


def test_finished_but_unchecked_is_not_ingested_during_the_settling_window():
    """All matches played, bonus points not yet verified — wait."""
    events = [make_event(1, finished=True, data_checked=False)]
    fixtures = {1: [make_fixture(i, event=1, finished=True) for i in range(1, 11)]}
    target, calls = _resolve(events, fixtures=fixtures)
    assert target is None
    assert calls == [1]


def test_fixtures_are_only_fetched_for_the_unchecked_candidate():
    events = [
        make_event(1, finished=True, data_checked=True),
        make_event(2, finished=True, data_checked=False),
    ]
    target, calls = _resolve(events, fixtures={2: []})
    assert target is not None and target.gw == 1
    assert calls == []


# -- Postponed-fixture override -----------------------------------------------


def test_postponed_fixture_makes_the_gameweek_partially_ingestable():
    events = [make_event(3, finished=True, data_checked=False)]
    fixtures = {
        3: [
            *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
            make_fixture(99, event=3, finished=False, kickoff_time=NEXT_MONTH),
        ]
    }
    target, _ = _resolve(events, fixtures=fixtures)
    assert target is not None
    assert (target.gw, target.partial, target.pending_fixture_ids) == (3, True, [99])


def test_null_kickoff_counts_as_postponed():
    fixtures = [
        *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
        make_fixture(99, event=3, finished=False, kickoff_time=None),
    ]
    assert is_effectively_complete(fixtures, NOW) == (True, [99])


def test_an_imminent_fixture_is_not_a_postponement():
    fixtures = [
        *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
        make_fixture(99, event=3, finished=False, kickoff_time=SOON),
    ]
    assert is_effectively_complete(fixtures, NOW) == (False, [])


def test_a_stuck_gameweek_does_not_block_a_later_settled_one():
    events = [
        make_event(3, finished=True, data_checked=False),
        make_event(4, finished=True, data_checked=True),
    ]
    fixtures = {
        3: [
            *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
            make_fixture(99, event=3, finished=False, kickoff_time=SOON),
        ]
    }
    target, _ = _resolve(events, fixtures=fixtures)
    assert target is not None and target.gw == 4


def test_all_fixtures_finished_is_not_effectively_complete():
    fixtures = [make_fixture(i, event=3, finished=True) for i in range(1, 11)]
    assert is_effectively_complete(fixtures, NOW) == (False, [])


def test_naive_kickoff_times_are_treated_as_utc():
    fixtures = [
        make_fixture(1, event=3, finished=True),
        make_fixture(99, event=3, finished=False, kickoff_time="2026-10-14T19:00:00"),
    ]
    assert is_effectively_complete(fixtures, NOW) == (True, [99])


# -- Metadata tagging ---------------------------------------------------------


def test_partial_metadata_is_empty_for_a_complete_gameweek():
    events = [make_event(1, finished=True, data_checked=True)]
    target, _ = _resolve(events)
    assert target is not None
    assert partial_metadata(target) == {}


def test_partial_metadata_lists_the_pending_fixtures():
    events = [make_event(3, finished=True, data_checked=False)]
    fixtures = {
        3: [
            make_fixture(1, event=3, finished=True),
            make_fixture(88, event=3, finished=False, kickoff_time=None),
            make_fixture(99, event=3, finished=False, kickoff_time=NEXT_MONTH),
        ]
    }
    target, _ = _resolve(events, fixtures=fixtures)
    assert target is not None
    assert partial_metadata(target) == {"partial": "true", "pending-fixtures": "88,99"}
