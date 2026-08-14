"""Season derivation — the mechanism that makes season rollover a non-event."""

from __future__ import annotations

import pytest

from fpl import keys
from fpl.season import derive_season

from .conftest import make_bootstrap


def test_derives_short_season_from_static_content_url():
    assert derive_season(make_bootstrap([], season="2026_27")) == "2026-27"


def test_rollover_changes_every_derived_key_without_a_code_change():
    """The same code, fed next season's bootstrap, writes under a new prefix."""
    this_season = derive_season(make_bootstrap([], season="2026_27"))
    next_season = derive_season(make_bootstrap([], season="2027_28"))

    assert (this_season, next_season) == ("2026-27", "2027-28")
    assert keys.gameweek_live_key(this_season, 5) == "raw/2026-27/gw5/gameweek-live.json"
    assert keys.gameweek_live_key(next_season, 5) == "raw/2027-28/gw5/gameweek-live.json"
    assert keys.manager_picks_key(next_season, 1).startswith("raw/2027-28/")


def test_trailing_path_after_the_season_still_parses():
    bootstrap = make_bootstrap([])
    bootstrap["game_config"]["settings"]["static_content_url"] = (
        "https://fantasy.premierleague.com/img/static/2030_31/badges/"
    )
    assert derive_season(bootstrap) == "2030-31"


@pytest.mark.parametrize(
    "url",
    ["https://fantasy.premierleague.com/img/static/", "", "https://example.com/2026-27/"],
)
def test_unparseable_url_raises(url: str):
    bootstrap = make_bootstrap([])
    bootstrap["game_config"]["settings"]["static_content_url"] = url
    with pytest.raises(ValueError, match="static_content_url"):
        derive_season(bootstrap)


def test_missing_field_raises_naming_the_path():
    with pytest.raises(ValueError, match="game_config.settings.static_content_url"):
        derive_season({"events": []})
