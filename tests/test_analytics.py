"""Acceptance tests for the Analytics Engine."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from fpl.analytics import (
    EO_SCHEMA,
    FLOW_SCHEMA,
    GAP_SCHEMA,
    compute_effective_ownership,
    compute_ownership_gap,
    compute_transfer_flow,
)
from fpl.storage import Storage

# ---------------------------------------------------------------------------
# Seed data and builders
# ---------------------------------------------------------------------------

TEAMS = [
    {"id": 1, "name": "Liverpool", "short_name": "LIV"},
    {"id": 2, "name": "Man City", "short_name": "MCI"},
    {"id": 3, "name": "Arsenal", "short_name": "ARS"},
]

PLAYERS = [
    {
        "code": 111,
        "fpl_id": 1,
        "web_name": "Salah",
        "full_name": "Mohamed Salah",
        "team": 1,
        "team_name": "Liverpool",
        "position": 3,
        "now_cost": 130,
        "news": "",
    },
    {
        "code": 222,
        "fpl_id": 2,
        "web_name": "Haaland",
        "full_name": "Erling Haaland",
        "team": 2,
        "team_name": "Man City",
        "position": 4,
        "now_cost": 150,
        "news": "",
    },
    {
        "code": 333,
        "fpl_id": 3,
        "web_name": "Saka",
        "full_name": "Bukayo Saka",
        "team": 3,
        "team_name": "Arsenal",
        "position": 3,
        "now_cost": 100,
        "news": "knock",
    },
    {
        "code": 444,
        "fpl_id": 4,
        "web_name": "Raya",
        "full_name": "David Raya",
        "team": 3,
        "team_name": "Arsenal",
        "position": 1,
        "now_cost": 55,
        "news": "",
    },
]

FIXTURES = [
    # Team 1 plays in GWs 3, 4, 5; team 3 only in GW 4 (null-tail case).
    {"id": 1, "gameweek": 3, "team_h": 1, "team_a": 2, "kickoff_time": datetime(2026, 8, 15, 15)},
    {"id": 2, "gameweek": 4, "team_h": 3, "team_a": 1, "kickoff_time": datetime(2026, 8, 22, 15)},
    {"id": 3, "gameweek": 5, "team_h": 2, "team_a": 1, "kickoff_time": datetime(2026, 8, 29, 15)},
]


def _storage(tmp_path: Path) -> Storage:
    s = Storage(str(tmp_path / "test.duckdb"))
    s.upsert_teams(TEAMS)
    s.upsert_players(PLAYERS)
    s.upsert_fixtures(FIXTURES)
    return s


def _seed_managers(s: Storage, gw: int, broad_ids: list[int], top_ids: list[int]) -> None:
    rows = [
        {"manager_id": m, "rank": i + 1, "total_points": 0, "is_top_slice": m in top_ids}
        for i, m in enumerate(top_ids + broad_ids)
    ]
    s.upsert_cohort_managers(gw, rows)


def _pick(gw: int, manager_id: int, fpl_id: int, multiplier: int = 1, chip: str | None = None):
    return {
        "gameweek": gw,
        "manager_id": manager_id,
        "fpl_id": fpl_id,
        "squad_position": fpl_id,
        "multiplier": multiplier,
        "is_captain": multiplier == 2,
        "is_vice_captain": False,
        "active_chip": chip,
    }


def _transfer(gw: int, manager_id: int, fpl_id_in: int, fpl_id_out: int):
    return {
        "manager_id": manager_id,
        "gameweek": gw,
        "fpl_id_in": fpl_id_in,
        "fpl_id_out": fpl_id_out,
        "cost_in": 100,
        "cost_out": 90,
        "transfer_time": datetime(2026, 8, 10, 12),
    }


def _row(df: pl.DataFrame, fpl_id: int) -> dict:
    matches = df.filter(pl.col("fpl_id") == fpl_id)
    assert matches.height == 1
    return matches.row(0, named=True)


# ---------------------------------------------------------------------------
# Effective Ownership — template mode
# ---------------------------------------------------------------------------


def test_eo_template_passthrough(tmp_path: Path):
    """GW 1 EO is global selected_by_percent verbatim, with no captaincy."""
    with _storage(tmp_path) as s:
        s.upsert_player_gw_stats(1, [{"fpl_id": 1, "selected_by_percent": 45.0, "ep_next": 6.5}])
        eo = compute_effective_ownership(s, 1)

    row = _row(eo, 1)
    assert row["eo_broad"] == 45.0
    assert row["owned_pct_broad"] == 45.0
    assert row["captained_pct_broad"] == 0.0
    assert row["eo_top5k"] is None
    assert row["cohort_size"] is None
    assert row["source"] == "template"


# ---------------------------------------------------------------------------
# Effective Ownership — cohort mode
# ---------------------------------------------------------------------------


def test_eo_cohort_per_slice(tmp_path: Path):
    """Slice EO = owned + captained + 2*tc, each slice over its own managers."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 5, broad_ids=[201, 202], top_ids=[101, 102, 103, 104, 105])
        s.insert_picks(
            [
                # 3 of 5 top managers own player 1, 2 of them as captain.
                _pick(5, 101, 1, multiplier=2),
                _pick(5, 102, 1, multiplier=2),
                _pick(5, 103, 1),
                # 1 of 2 broad managers owns player 1 as triple captain.
                _pick(5, 201, 1, multiplier=3),
            ]
        )
        eo = compute_effective_ownership(s, 5)

    row = _row(eo, 1)
    assert row["eo_top5k"] == pytest.approx(3 / 5 + 2 / 5)  # = 1.0
    assert row["owned_pct_top5k"] == pytest.approx(0.6)
    assert row["captained_pct_top5k"] == pytest.approx(0.4)
    # Broad computed independently: 1/2 owned, 0 captained, 1/2 triple-captained.
    assert row["eo_broad"] == pytest.approx(0.5 + 0.0 + 2 * 0.5)
    assert row["captained_pct_broad"] == 0.0
    assert row["cohort_size"] == 7
    assert row["source"] == "cohort"


def test_eo_slices_never_pool(tmp_path: Path):
    """A player fully owned by one slice shows 0.0 (not null) in the other."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 5, broad_ids=[201, 202], top_ids=[101, 102])
        s.insert_picks(
            [
                _pick(5, 101, 1),
                _pick(5, 102, 1),
                _pick(5, 201, 2),
                _pick(5, 202, 2),
            ]
        )
        eo = compute_effective_ownership(s, 5)

    row = _row(eo, 1)
    assert row["eo_top5k"] == 1.0
    assert row["eo_broad"] == 0.0
    assert row["eo_divergence"] == -1.0


def test_eo_gw2_4_top5k_null(tmp_path: Path):
    """GW 2-4 has no top slice: top5k columns and divergence are null."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 3, broad_ids=[201, 202], top_ids=[])
        s.insert_picks([_pick(3, 201, 1), _pick(3, 202, 1, multiplier=2)])
        eo = compute_effective_ownership(s, 3)

    row = _row(eo, 1)
    assert row["eo_top5k"] is None
    assert row["owned_pct_top5k"] is None
    assert row["eo_divergence"] is None
    assert row["eo_broad"] == pytest.approx(1.0 + 0.5)
    assert row["source"] == "cohort"


def test_eo_freehit_excluded_from_slice(tmp_path: Path):
    """Chip managers drop out of both numerator and denominator of their slice."""
    picks = [
        _pick(5, 101, 1),
        _pick(5, 102, 1),
        _pick(5, 103, 2, chip="freehit"),  # FH manager owns player 2 only
    ]
    with _storage(tmp_path) as s:
        _seed_managers(s, 5, broad_ids=[], top_ids=[101, 102, 103])
        s.insert_picks(picks)

        eo = compute_effective_ownership(s, 5)
        # Denominator shrinks to 2; player 2's only owner is excluded entirely.
        assert _row(eo, 1)["eo_top5k"] == 1.0
        assert eo.filter(pl.col("fpl_id") == 2).is_empty()
        assert _row(eo, 1)["cohort_size"] == 2

        # exclude_chip=None keeps the FH manager in both sides.
        eo_all = compute_effective_ownership(s, 5, exclude_chip=None)
        assert _row(eo_all, 1)["eo_top5k"] == pytest.approx(2 / 3)
        assert _row(eo_all, 2)["eo_top5k"] == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# Ownership gap
# ---------------------------------------------------------------------------


def test_gap_threshold_my_team_and_fixtures(tmp_path: Path):
    """Gap keeps unowned players above threshold, EO-descending, with fixtures."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 3, broad_ids=list(range(201, 211)), top_ids=[])
        picks = (
            [_pick(3, m, 1) for m in range(201, 206)]  # player 1: 5/10
            + [_pick(3, m, 2) for m in range(201, 206)]  # player 2: 5/10 (mine)
            + [_pick(3, m, 3) for m in range(201, 204)]  # player 3: 3/10
            + [_pick(3, 201, 4)]  # player 4: 1/10 (below threshold)
        )
        s.insert_picks(picks)
        s.upsert_my_picks(3, [{"fpl_id": 2, "multiplier": 1, "is_captain": False}])

        gap = compute_ownership_gap(s, 3, eo_threshold=0.2)

    assert gap["fpl_id"].to_list() == [1, 3]  # mine and below-threshold absent
    salah = _row(gap, 1)
    assert salah["effective_ownership"] == 0.5
    assert (salah["next_1_opponent"], salah["next_1_is_home"]) == ("MCI", True)
    assert (salah["next_2_opponent"], salah["next_2_is_home"]) == ("ARS", False)
    assert (salah["next_3_opponent"], salah["next_3_is_home"]) == ("MCI", False)
    saka = _row(gap, 3)
    assert (saka["next_1_opponent"], saka["next_1_is_home"]) == ("LIV", True)
    assert saka["next_2_opponent"] is None
    assert saka["news"] == "knock"


def test_gap_prefers_top5k_eo(tmp_path: Path):
    """When a top slice exists, effective_ownership is the top-slice EO."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 5, broad_ids=[201, 202], top_ids=[101, 102])
        s.insert_picks([_pick(5, 101, 1), _pick(5, 102, 1), _pick(5, 201, 2)])
        gap = compute_ownership_gap(s, 5, eo_threshold=0.6)

    assert gap["fpl_id"].to_list() == [1]  # player 2's broad 0.5 < threshold
    assert _row(gap, 1)["effective_ownership"] == 1.0


def test_gap_template_mode(tmp_path: Path):
    """Gap works identically pre-cohort, using global ownership as EO."""
    with _storage(tmp_path) as s:
        s.upsert_player_gw_stats(
            1,
            [
                {"fpl_id": 1, "selected_by_percent": 50.0, "ep_next": 7.0},
                {"fpl_id": 2, "selected_by_percent": 30.0, "ep_next": 6.0},
                {"fpl_id": 3, "selected_by_percent": 2.0, "ep_next": 4.0},
            ],
        )
        gap = compute_ownership_gap(s, 1, eo_threshold=5.0)

    assert gap["fpl_id"].to_list() == [1, 2]
    row = _row(gap, 1)
    assert row["effective_ownership"] == 50.0
    assert row["ep_next"] == 7.0
    assert row["source"] == "template"


# ---------------------------------------------------------------------------
# Transfer flow
# ---------------------------------------------------------------------------


def test_flow_counts_pct_and_eo(tmp_path: Path):
    """Flow counts distinct movers, as % of cohort, with current EO attached."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 3, broad_ids=list(range(201, 211)), top_ids=[])
        s.insert_picks([_pick(3, m, 1) for m in range(201, 206)])  # player 1 EO 0.5
        s.insert_transfers([_transfer(3, m, fpl_id_in=1, fpl_id_out=2) for m in range(201, 205)])

        top_in, top_out = compute_transfer_flow(s, 3)

    row_in = _row(top_in, 1)
    assert row_in["transfer_count"] == 4
    assert row_in["transfer_pct"] == pytest.approx(40.0)
    assert row_in["current_eo"] == 0.5
    assert row_in["web_name"] == "Salah"
    row_out = _row(top_out, 2)
    assert row_out["transfer_count"] == 4
    assert row_out["current_eo"] == 0.0  # unowned in cohort


def test_flow_top_n(tmp_path: Path):
    """top_n caps the rows; fewer distinct transfers return fewer rows."""
    with _storage(tmp_path) as s:
        _seed_managers(s, 3, broad_ids=[201, 202, 203], top_ids=[])
        s.insert_transfers(
            [
                _transfer(3, 201, fpl_id_in=1, fpl_id_out=4),
                _transfer(3, 202, fpl_id_in=1, fpl_id_out=4),
                _transfer(3, 202, fpl_id_in=2, fpl_id_out=3),
                _transfer(3, 203, fpl_id_in=3, fpl_id_out=4),
            ]
        )
        top_in_capped, _ = compute_transfer_flow(s, 3, top_n=2)
        top_in_all, _ = compute_transfer_flow(s, 3, top_n=5)

    assert top_in_capped.height == 2
    assert top_in_capped["fpl_id"][0] == 1  # most transferred in first
    assert top_in_all.height == 3


def test_flow_template_gw_is_empty(tmp_path: Path):
    """No cohort in a template GW means no transfers to analyse."""
    with _storage(tmp_path) as s:
        top_in, top_out = compute_transfer_flow(s, 1)
    assert top_in.is_empty() and top_out.is_empty()
    assert top_in.schema == pl.Schema(FLOW_SCHEMA)


# ---------------------------------------------------------------------------
# Empty-data behaviour
# ---------------------------------------------------------------------------


def test_all_functions_empty_without_data(tmp_path: Path):
    """Every function returns a typed empty frame when the GW has no data."""
    with _storage(tmp_path) as s:
        eo_template = compute_effective_ownership(s, 1)
        eo_cohort = compute_effective_ownership(s, 5)
        gap = compute_ownership_gap(s, 5)
        top_in, top_out = compute_transfer_flow(s, 5)

    assert eo_template.is_empty() and eo_template.schema == pl.Schema(EO_SCHEMA)
    assert eo_cohort.is_empty() and eo_cohort.schema == pl.Schema(EO_SCHEMA)
    assert gap.is_empty() and gap.schema == pl.Schema(GAP_SCHEMA)
    assert top_in.is_empty() and top_out.schema == pl.Schema(FLOW_SCHEMA)
