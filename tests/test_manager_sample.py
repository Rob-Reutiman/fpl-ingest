"""Job 3 — cohort sampling, batched writes, and failure accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl.constants import ENTRIES_PER_SAMPLED_PAGE, SAMPLE_PAGE_COUNT, TOP_PAGE_COUNT
from fpl.jobs import manager_sample

from .conftest import FakeAPI, FakeStore, make_bootstrap, make_event, make_fixture

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SUMMARY_KEY = "raw/2026-27/gw{}/manager-picks-summary.json"

# A following gameweek's deadline this close means the settlement lead time has
# started; this far away means it hasn't.
NEAR_DEADLINE = (NOW + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
FAR_DEADLINE = (NOW + timedelta(days=6)).isoformat().replace("+00:00", "Z")
EXPECTED_KEYS = [
    "raw/2026-27/gw1/standings-top1000.json",
    "raw/2026-27/gw1/standings-sample.json",
    "raw/2026-27/gw1/manager-picks.ndjson",
    "raw/2026-27/gw1/manager-picks-summary.json",
]


@pytest.fixture
def settled(api: FakeAPI) -> FakeAPI:
    api.bootstrap = make_bootstrap([make_event(1, finished=True, data_checked=True), make_event(2)])
    return api


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> int:
    """Shrink the cohort so most tests don't fetch 2,000 responses.

    Returns the resulting cohort size: 2 top pages of 50, plus 3 sampled pages
    of 5.
    """
    monkeypatch.setattr(manager_sample, "TOP_PAGE_COUNT", 2)
    monkeypatch.setattr("fpl.sampling.SAMPLE_PAGE_COUNT", 3)
    monkeypatch.setattr("fpl.sampling.ENTRIES_PER_SAMPLED_PAGE", 5)
    return 2 * 50 + 3 * 5


# -- The batching guarantee ---------------------------------------------------


def test_a_two_thousand_manager_harvest_costs_a_handful_of_writes(
    settled: FakeAPI, store: FakeStore, client
):
    """The whole point of the job: batch, don't PutObject per manager.

    R2 bills Class A operations, so the cost that matters is writes per run, not
    managers per run. Four raw objects regardless of cohort size, plus the curated
    tables and any master-table extension.
    """
    assert manager_sample.run(client, store, now=NOW) == 1

    expected_cohort = TOP_PAGE_COUNT * 50 + SAMPLE_PAGE_COUNT * ENTRIES_PER_SAMPLED_PAGE
    assert expected_cohort == 2000
    assert settled.count("/picks/") == expected_cohort

    raw_writes = [key for key in store.keys if key.startswith("raw/")]
    assert raw_writes == EXPECTED_KEYS
    assert len(store.puts) < 20
    assert len(store.objects[EXPECTED_KEYS[2]].lines) == expected_cohort


# -- Output shape -------------------------------------------------------------


def test_each_ndjson_record_carries_its_entry_group_and_raw_response(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    manager_sample.run(client, store, now=NOW)

    lines = store.objects[EXPECTED_KEYS[2]].lines
    assert len(lines) == small
    first = lines[0]
    assert first["entry_id"] == 100_001
    assert first["gameweek"] == 1
    assert first["group"] == "top1000"
    assert first["rank"] == 1
    assert first["data"]["picks"][0]["element"] == 1

    groups = {line["group"] for line in lines}
    assert groups == {"top1000", "sampled"}
    assert sum(1 for line in lines if line["group"] == "top1000") == 100


def test_sampled_managers_come_from_below_the_top_1000(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    lines = _run_and_read(settled, store, client)
    sampled_ranks = [line["rank"] for line in lines if line["group"] == "sampled"]
    assert sampled_ranks
    assert min(sampled_ranks) > 1000


def test_no_manager_appears_twice(settled: FakeAPI, store: FakeStore, client, small: int):
    lines = _run_and_read(settled, store, client)
    entry_ids = [line["entry_id"] for line in lines]
    assert len(set(entry_ids)) == len(entry_ids)


def test_raw_standings_pages_are_stored_batched(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    manager_sample.run(client, store, now=NOW)

    top = store.objects[EXPECTED_KEYS[0]].json
    sample = store.objects[EXPECTED_KEYS[1]].json
    assert [page["standings"]["page"] for page in top] == [1, 2]
    assert len(sample) == 3
    assert all(page["standings"]["page"] >= 21 for page in sample)


# -- Idempotency --------------------------------------------------------------


def test_an_existing_summary_makes_the_run_a_complete_no_op(settled: FakeAPI, client):
    store = FakeStore(existing=[SUMMARY_KEY.format(1)])

    assert manager_sample.run(client, store, now=NOW) is None
    assert store.puts == []
    assert settled.count("/standings/") == 0
    assert settled.count("/picks/") == 0


def test_the_summary_is_written_last_so_a_killed_run_is_retried(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    manager_sample.run(client, store, now=NOW)
    assert store.keys[-1] == SUMMARY_KEY.format(1)


def test_no_op_when_no_gameweek_has_settled(api: FakeAPI, store: FakeStore, client):
    api.bootstrap = make_bootstrap(
        [make_event(1, finished=True), make_event(2, deadline_time=FAR_DEADLINE)]
    )
    api.fixtures = [make_fixture(i, event=1, finished=True) for i in range(1, 11)]

    assert manager_sample.run(client, store, now=NOW) is None
    assert store.puts == []
    assert api.count("/picks/") == 0


# -- Failure handling ---------------------------------------------------------


def test_a_permanently_failing_entry_is_recorded_not_dropped(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    settled.permanent_failures = {"/entry/100001/": 404}

    manager_sample.run(client, store, now=NOW)

    summary = store.objects[SUMMARY_KEY.format(1)].json
    assert summary["requested"] == small
    assert summary["succeeded"] == small - 1
    assert summary["failed"] == 1
    assert [f["entry_id"] for f in summary["failures"]] == [100_001]
    assert summary["failures"][0]["group"] == "top1000"
    assert "404" in summary["failures"][0]["error"]

    lines = store.objects[EXPECTED_KEYS[2]].lines
    assert len(lines) == small - 1
    assert 100_001 not in {line["entry_id"] for line in lines}


def test_a_transient_failure_is_retried_and_recovers(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    settled.transient_failures = {"/entry/100002/": [503, 500]}

    manager_sample.run(client, store, now=NOW)

    summary = store.objects[SUMMARY_KEY.format(1)].json
    assert summary["failures"] == []
    assert summary["succeeded"] == small


def test_the_summary_records_which_pages_were_sampled(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    manager_sample.run(client, store, now=NOW)

    summary = store.objects[SUMMARY_KEY.format(1)].json
    assert summary["season"] == "2026-27"
    assert summary["gameweek"] == 1
    assert len(summary["sampled_pages"]) == 3
    assert summary["ingested_at"]


def test_an_empty_cohort_aborts_before_writing_anything(
    settled: FakeAPI, store: FakeStore, client, small: int
):
    """An empty summary would mark the gameweek done forever — fail instead."""
    settled.empty_standings = True

    with pytest.raises(RuntimeError, match="no entries"):
        manager_sample.run(client, store, now=NOW)

    assert store.puts == []


# -- Partial gameweeks --------------------------------------------------------


def test_a_partial_gameweek_tags_every_gameweek_object(
    api: FakeAPI, store: FakeStore, client, small: int
):
    """The tag belongs on objects that describe this gameweek.

    The cross-season master tables are deliberately excluded: they span every
    season, so "partial" would be meaningless on them.
    """
    api.bootstrap = make_bootstrap(
        [
            make_event(1, finished=True, data_checked=False),
            make_event(2, deadline_time=NEAR_DEADLINE),
        ]
    )
    api.fixtures = [
        make_fixture(1, event=1, finished=True),
        make_fixture(99, event=1, finished=False, kickoff_time=None),
    ]

    assert manager_sample.run(client, store, now=NOW) == 1

    tagged = [put for put in store.puts if not put.key.startswith("curated/master/")]
    assert tagged
    assert all(put.metadata == {"partial": "true", "pending-fixtures": "99"} for put in tagged)
    assert store.objects[SUMMARY_KEY.format(1)].json["pending_fixture_ids"] == [99]


def _run_and_read(api: FakeAPI, store: FakeStore, client) -> list[dict]:
    manager_sample.run(client, store, now=NOW)
    return store.objects[EXPECTED_KEYS[2]].lines
