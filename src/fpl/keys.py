"""R2 object keys.

Every key in the bucket is built here so no other module concatenates a path.

    raw/{season}/current/bootstrap-static.json
    raw/{season}/current/fixtures.json
    raw/{season}/daily/bootstrap-static/{date}.json
    raw/{season}/daily/fixtures/{date}.json
    raw/{season}/gw{N}/gameweek-live.json
    raw/{season}/gw{N}/fixtures.json
    raw/{season}/gw{N}/element-summary/{element_id}.json
    raw/{season}/gw{N}/standings-top1000.json
    raw/{season}/gw{N}/standings-sample.json
    raw/{season}/gw{N}/manager-picks.ndjson
    raw/{season}/gw{N}/manager-picks-summary.json
    raw/{season}/archive/{filename}
    curated/{season}/{table}.parquet
    curated/master/{filename}
"""

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
    """Fixed key, overwritten hourly — the live snapshot the transform reads."""
    return f"raw/{season}/current/bootstrap-static.json"


def current_fixtures_key(season: str) -> str:
    return f"raw/{season}/current/fixtures.json"


def gameweek_fixtures_key(season: str, gw: int) -> str:
    return _gameweek(season, gw, "fixtures.json")


def element_summary_key(season: str, gw: int, element_id: int) -> str:
    """One player's per-fixture history, fetched only for double gameweeks."""
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
    """Provenance copy of a third-party source file, stored unmodified."""
    return f"raw/{season}/archive/{filename}"


def curated_key(season: str, table: str) -> str:
    return f"curated/{season}/{table}.parquet"


def master_key(filename: str) -> str:
    """Cross-season table or operational output. Not season-scoped."""
    return f"curated/master/{filename}"
