"""Static constants shared across FPL Edge modules."""

# The global "Overall" league every manager is a member of.
OVERALL_LEAGUE_ID = 314

# Standings pagination size returned by the FPL API.
ENTRIES_PER_PAGE = 50

# Cohort strategy thresholds.
#
# "template"        -> no manager cohort; use global selected_by_percent from
#                      the bootstrap endpoint (appropriate pre-season and GW1
#                      when standings are empty or meaningless).
# "current_season"  -> scrape league 314 current standings, then sample a pool
#                      of that size before drawing the cohort sample.
COHORT_STRATEGY = {
    "template": {"source": "template", "pool_size": 0},  # Pre-season + GW1
    "gw_2_4": {"source": "current_season", "pool_size": 100_000},
    "gw_5_9": {"source": "current_season", "pool_size": 50_000},
    "gw_10_plus": {"source": "current_season", "pool_size": 25_000},
}

# Position codes used throughout the FPL API.
POSITION_GK = 1
POSITION_DEF = 2
POSITION_MID = 3
POSITION_FWD = 4

POSITION_NAMES = {
    POSITION_GK: "GK",
    POSITION_DEF: "DEF",
    POSITION_MID: "MID",
    POSITION_FWD: "FWD",
}
