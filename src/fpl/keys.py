"""Every R2 object key in the bucket, so no other module concatenates a path."""

from __future__ import annotations

from datetime import date


def _daily(season: str, dataset: str, on: date) -> str:
    return f"raw/{season}/daily/{dataset}/{on.isoformat()}.json"


def _gameweek(season: str, gw: int, name: str) -> str:
    return f"raw/{season}/gw{gw}/{name}"


def bootstrap_key(season: str, on: date) -> str:
    return _daily(season, "bootstrap-static", on)


def fixtures_key(season: str, on: date) -> str:
    return _daily(season, "fixtures", on)


def current_bootstrap_key(season: str) -> str:
    """The live snapshot the transforms read. Overwritten in place every hour."""
    return f"raw/{season}/current/bootstrap-static.json"


def current_fixtures_key(season: str) -> str:
    return f"raw/{season}/current/fixtures.json"


def gameweek_fixtures_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "fixtures.json")


def element_summary_key(season: str, gw: int, element_id: int) -> str:
    """One player's history at fixture grain. Stored for double gameweeks."""
    return _gameweek(season, gw, f"element-summary/{element_id}.json")


def gameweek_live_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "gameweek-live.json")


def standings_top_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "standings-top1000.json")


def standings_sample_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "standings-sample.json")


def manager_picks_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "manager-picks.ndjson")


def manager_summary_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "manager-picks-summary.json")


def raw_archive_key(season: str, filename: str) -> str:
    """A verbatim copy of one third party source file, kept for provenance."""
    return f"raw/{season}/archive/{filename}"


def curated_key(season: str, table: str) -> str:
    return f"curated/{season}/{table}.parquet"


def master_key(filename: str) -> str:
    """A table or report spanning every season."""
    return f"curated/master/{filename}"
