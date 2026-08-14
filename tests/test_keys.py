"""The R2 key layout, pinned against the documented contract."""

from __future__ import annotations

from datetime import date

from fpl import keys

DAY = date(2026, 8, 15)


def test_daily_snapshots_are_date_scoped():
    assert (
        keys.bootstrap_key("2026-27", DAY) == "raw/2026-27/daily/bootstrap-static/2026-08-15.json"
    )
    assert keys.fixtures_key("2026-27", DAY) == "raw/2026-27/daily/fixtures/2026-08-15.json"


def test_gameweek_objects_are_gameweek_scoped():
    assert keys.gameweek_live_key("2026-27", 7) == "raw/2026-27/gw7/gameweek-live.json"
    assert keys.standings_top_key("2026-27", 7) == "raw/2026-27/gw7/standings-top1000.json"
    assert keys.standings_sample_key("2026-27", 7) == "raw/2026-27/gw7/standings-sample.json"
    assert keys.manager_picks_key("2026-27", 7) == "raw/2026-27/gw7/manager-picks.ndjson"
    assert keys.manager_summary_key("2026-27", 7) == "raw/2026-27/gw7/manager-picks-summary.json"
