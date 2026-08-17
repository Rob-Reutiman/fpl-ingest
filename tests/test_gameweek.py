"""Which gameweek is ready to ingest, and when a stuck one overrides that."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl.gameweek import is_effectively_complete, partial_metadata, resolve_target

from .conftest import make_event, make_fixture

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# A gameweek with no override reads as already started, per `make_event`'s
# default. Tests wanting the opposite pass this instead.
NOT_STARTED = (NOW + timedelta(days=365)).isoformat().replace("+00:00", "Z")


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


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


# A deadline this close is inside the settlement lead time; this far away is
# comfortably outside it.
NEAR_DEADLINE = _iso(NOW + timedelta(hours=6))
FAR_DEADLINE = _iso(NOW + timedelta(days=6))


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


def test_no_op_before_a_gameweeks_own_deadline():
    events = [
        make_event(1, deadline_time=NOT_STARTED),
        make_event(2, deadline_time=NOT_STARTED),
    ]
    target, calls = _resolve(events)
    assert target is None
    assert calls == []  # a gameweek that hasn't started never costs a fixtures request


def test_data_checked_is_trusted_the_moment_a_gameweeks_own_deadline_passes():
    events = [make_event(1, data_checked=True)]
    target, calls = _resolve(events)
    assert target is not None and target.gw == 1
    assert calls == []


def test_a_normal_settling_window_does_not_fetch_fixtures():
    """All matches played, bonus points not yet verified, next deadline days
    away — wait, and don't bother checking fixtures yet."""
    events = [
        make_event(1, finished=True, data_checked=False),
        make_event(2, deadline_time=FAR_DEADLINE),
    ]
    target, calls = _resolve(events)
    assert target is None
    assert calls == []


def test_fixtures_are_only_fetched_for_the_unchecked_candidate():
    events = [
        make_event(1, finished=True, data_checked=True),
        make_event(2, finished=True, data_checked=False, deadline_time=NEAR_DEADLINE),
    ]
    target, calls = _resolve(events, fixtures={2: []})
    assert target is not None and target.gw == 1
    assert calls == []


def test_the_final_gameweek_of_a_season_has_no_lead_time_fallback():
    """No following deadline to measure against, so it waits on data_checked."""
    events = [make_event(1, finished=True, data_checked=False)]
    target, calls = _resolve(events)
    assert target is None
    assert calls == []


# -- Stuck-gameweek override ---------------------------------------------------


def test_a_stuck_fixture_makes_the_gameweek_partially_ingestable():
    events = [make_event(3, data_checked=False), make_event(4, deadline_time=NEAR_DEADLINE)]
    fixtures = {
        3: [
            *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
            make_fixture(99, event=3, finished=False, kickoff_time="2026-10-14T19:00:00Z"),
        ]
    }
    target, _ = _resolve(events, fixtures=fixtures)
    assert target is not None
    assert (target.gw, target.partial, target.pending_fixture_ids) == (3, True, [99])


def test_every_fixture_already_finished_is_also_stuck():
    """Nothing left to wait on but `data_checked` itself, this close to the wall."""
    events = [
        make_event(3, finished=True, data_checked=False),
        make_event(4, deadline_time=NEAR_DEADLINE),
    ]
    fixtures = {3: [make_fixture(i, event=3, finished=True) for i in range(1, 11)]}
    target, _ = _resolve(events, fixtures=fixtures)
    assert target is not None
    assert (target.gw, target.partial, target.pending_fixture_ids) == (3, True, [])


def test_a_fixture_due_before_the_next_deadline_keeps_waiting():
    """Within the lead window, but this fixture can still complete in time."""
    events = [make_event(3, data_checked=False), make_event(4, deadline_time=NEAR_DEADLINE)]
    fixtures = {
        3: [
            make_fixture(1, event=3, finished=True),
            make_fixture(
                99, event=3, finished=False, kickoff_time=(NOW + timedelta(hours=1)).isoformat()
            ),
        ]
    }
    target, calls = _resolve(events, fixtures=fixtures)
    assert target is None
    assert calls == [3]


def test_null_kickoff_counts_as_stuck():
    cutoff = NOW + timedelta(hours=24)
    fixtures = [
        *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
        make_fixture(99, event=3, finished=False, kickoff_time=None),
    ]
    assert is_effectively_complete(fixtures, cutoff) == (True, [99])


def test_a_kickoff_before_the_cutoff_is_not_stuck():
    cutoff = NOW + timedelta(hours=24)
    fixtures = [
        *(make_fixture(i, event=3, finished=True) for i in range(1, 10)),
        make_fixture(
            99, event=3, finished=False, kickoff_time=(cutoff - timedelta(hours=6)).isoformat()
        ),
    ]
    assert is_effectively_complete(fixtures, cutoff) == (False, [])


def test_a_kickoff_at_the_cutoff_counts_as_stuck():
    cutoff = NOW + timedelta(hours=24)
    fixtures = [
        make_fixture(1, event=3, finished=True),
        make_fixture(99, event=3, finished=False, kickoff_time=cutoff.isoformat()),
    ]
    assert is_effectively_complete(fixtures, cutoff) == (True, [99])


def test_no_unfinished_fixtures_is_complete_vacuously():
    cutoff = NOW + timedelta(hours=24)
    fixtures = [make_fixture(i, event=3, finished=True) for i in range(1, 11)]
    assert is_effectively_complete(fixtures, cutoff) == (True, [])


def test_naive_kickoff_times_are_treated_as_utc():
    cutoff = NOW + timedelta(hours=24)
    fixtures = [
        make_fixture(1, event=3, finished=True),
        make_fixture(99, event=3, finished=False, kickoff_time="2026-10-14T19:00:00"),
    ]
    assert is_effectively_complete(fixtures, cutoff) == (True, [99])


# -- Metadata tagging ---------------------------------------------------------


def test_partial_metadata_is_empty_for_a_complete_gameweek():
    events = [make_event(1, finished=True, data_checked=True)]
    target, _ = _resolve(events)
    assert target is not None
    assert partial_metadata(target) == {}


def test_partial_metadata_lists_the_pending_fixtures():
    events = [make_event(3, data_checked=False), make_event(4, deadline_time=NEAR_DEADLINE)]
    fixtures = {
        3: [
            make_fixture(1, event=3, finished=True),
            make_fixture(88, event=3, finished=False, kickoff_time=None),
            make_fixture(99, event=3, finished=False, kickoff_time="2026-10-14T19:00:00Z"),
        ]
    }
    target, _ = _resolve(events, fixtures=fixtures)
    assert target is not None
    assert partial_metadata(target) == {"partial": "true", "pending-fixtures": "88,99"}
