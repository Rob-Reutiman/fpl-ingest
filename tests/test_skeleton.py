"""Acceptance tests for Module 1 — Project Skeleton."""


def test_config_loads():
    from fpl.config import settings

    assert settings.fpl_base_url == "https://fantasy.premierleague.com/api"
    assert settings.db_path == "data/fpl.duckdb"


def test_models_import():
    from fpl.models import Pick, Player, Transfer

    assert Player is not None
    assert Pick is not None
    assert Transfer is not None


def test_constants():
    from fpl.constants import OVERALL_LEAGUE_ID

    assert OVERALL_LEAGUE_ID == 314


def test_cohort_strategy_shape():
    from fpl.constants import COHORT_STRATEGY

    assert COHORT_STRATEGY["template"]["source"] == "template"
    assert COHORT_STRATEGY["gw_2_4"]["pool_size"] == 100_000
