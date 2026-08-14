"""Job 3's curated output: manager picks and the ownership aggregate."""

from __future__ import annotations

import pytest

from fpl import curated_schema, keys
from fpl.jobs import manager_sample
from fpl.transforms import ownership

from .conftest import (
    FakeAPI,
    FakeStore,
    column_types,
    make_bootstrap,
    make_element,
    make_event,
    read_curated,
    relation_rows,
)

SEASON = "2026-27"


def record(entry_id: int, group: str, picks: list[dict], rank: int | None = 1) -> dict:
    return {
        "entry_id": entry_id,
        "gameweek": 1,
        "group": group,
        "rank": rank,
        "data": {"picks": picks},
    }


def pick(element: int, position: int, *, multiplier: int = 1, captain: bool = False) -> dict:
    return {
        "element": element,
        "position": position,
        "multiplier": multiplier,
        "is_captain": captain,
        "is_vice_captain": False,
    }


class TestSampleSize:
    def test_counts_managers_actually_fetched_not_the_target(self):
        """A failed request must not quietly deflate the whole group's ownership."""
        records = [
            record(1, "top1000", [pick(1, 1)]),
            record(2, "top1000", [pick(1, 1)]),
            record(3, "sampled", [pick(1, 1)]),
        ]
        assert ownership.sample_sizes(records) == {"top1000": 2, "sampled": 1}

    def test_a_manager_counted_once_however_many_picks(self):
        records = [record(1, "top1000", [pick(e, e) for e in range(1, 16)])]
        assert ownership.sample_sizes(records) == {"top1000": 1}


class TestPickRows:
    def test_flattens_to_one_row_per_pick(self):
        rows = ownership.pick_rows([record(7, "top1000", [pick(1, 1), pick(2, 12)])])
        assert [(r["entry_id"], r["element_id"], r["pick_position"]) for r in rows] == [
            (7, 1, 1),
            (7, 2, 12),
        ]

    def test_carries_the_group_and_rank(self):
        (row,) = ownership.pick_rows([record(7, "sampled", [pick(1, 1)], rank=4321)])
        assert (row["sample_group"], row["overall_rank"]) == ("sampled", 4321)


@pytest.fixture
def harvested(api: FakeAPI, store: FakeStore, client, monkeypatch):
    """Run the job with a tiny hand-built cohort so ownership is checkable by eye."""
    api.bootstrap = make_bootstrap(
        [make_event(1, finished=True, data_checked=True)],
        elements=[make_element(e) for e in range(1, 4)],
    )

    records = [
        # top1000: 1 and 2 own element 1; only manager 1 starts it and captains it.
        record(1, "top1000", [pick(1, 1, multiplier=2, captain=True), pick(2, 12)]),
        record(2, "top1000", [pick(1, 13), pick(3, 1)]),
        # sampled: a single manager who owns element 3 only.
        record(3, "sampled", [pick(3, 1)]),
    ]
    monkeypatch.setattr(manager_sample, "harvest_picks", lambda client, cohort, gw: (records, []))
    monkeypatch.setattr(
        manager_sample,
        "collect_cohort",
        lambda client, rng: ([manager_sample.CohortEntry(1, 1, "top1000")], [], [], []),
    )
    manager_sample.run(client, store)
    return store


def read(store: FakeStore, table: str, tmp_path) -> list[dict]:
    return relation_rows(read_curated(store, keys.curated_key(SEASON, table), tmp_path))


class TestOwnershipAggregate:
    def test_ownership_uses_the_group_denominator(self, harvested, tmp_path):
        rows = {
            (r["element_id"], r["sample_group"]): r
            for r in read(harvested, "agg_player_ownership", tmp_path)
        }
        top = rows[(1, "top1000")]
        assert (top["sample_size"], top["owned_count"]) == (2, 2)
        assert top["ownership_pct"] == pytest.approx(100.0)

        sampled = rows[(3, "sampled")]
        assert (sampled["sample_size"], sampled["owned_count"]) == (1, 1)
        assert sampled["ownership_pct"] == pytest.approx(100.0)

    def test_a_benched_player_is_owned_but_not_starting(self, harvested, tmp_path):
        rows = {
            (r["element_id"], r["sample_group"]): r
            for r in read(harvested, "agg_player_ownership", tmp_path)
        }
        element_one = rows[(1, "top1000")]
        assert element_one["owned_count"] == 2
        assert element_one["starting_count"] == 1
        assert element_one["starting_pct"] == pytest.approx(50.0)

    def test_captaincy_is_counted_separately(self, harvested, tmp_path):
        rows = {
            (r["element_id"], r["sample_group"]): r
            for r in read(harvested, "agg_player_ownership", tmp_path)
        }
        assert rows[(1, "top1000")]["captain_count"] == 1
        assert rows[(1, "top1000")]["captain_pct"] == pytest.approx(50.0)
        assert rows[(3, "top1000")]["captain_count"] == 0

    def test_the_groups_are_kept_apart(self, harvested, tmp_path):
        """The differential signal is top1000 vs sampled, so they must not merge."""
        groups = {r["sample_group"] for r in read(harvested, "agg_player_ownership", tmp_path)}
        assert groups == {"top1000", "sampled"}


class TestManagerPicks:
    def test_every_pick_becomes_a_row_with_a_master_id(self, harvested, tmp_path):
        rows = read(harvested, "fact_manager_pick", tmp_path)
        assert len(rows) == 5
        assert all(r["player_master_id"] is not None for r in rows)

    def test_captain_and_multiplier_survive(self, harvested, tmp_path):
        row = next(
            r
            for r in read(harvested, "fact_manager_pick", tmp_path)
            if (r["entry_id"], r["element_id"]) == (1, 1)
        )
        assert (row["multiplier"], row["is_captain"], row["pick_position"]) == (2, True, 1)

    @pytest.mark.parametrize("table", ["fact_manager_pick", "agg_player_ownership"])
    def test_matches_the_column_contract(self, harvested, tmp_path, table):
        relation = read_curated(harvested, keys.curated_key(SEASON, table), tmp_path)
        assert column_types(relation) == curated_schema.COLUMNS[table]
