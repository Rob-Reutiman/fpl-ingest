"""Tunables and FPL API facts shared across the ingest jobs."""

FPL_BASE_URL = "https://fantasy.premierleague.com/api"

# Courtesy throttle. Holds a 2,000 entry manager sample inside ten minutes.
REQUEST_DELAY_SECONDS = 0.175
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_ATTEMPTS = 3

USER_AGENT = "fpl-ingest/0.1 (+https://github.com/robreutiman/fpl-ingest)"

# The global "Overall" league. Every entry belongs to it.
OVERALL_LEAGUE_ID = 314
ENTRIES_PER_PAGE = 50

# Ranks 1 to 1,000, being every entry on pages 1 to 20.
TOP_PAGE_COUNT = 20

# Ranks 1,001 to 10,000. Sampling pages across the range and keeping a subset of
# each spreads the entry budget over the whole rank distribution.
SAMPLE_PAGE_START = 21
SAMPLE_PAGE_END = 200
SAMPLE_PAGE_COUNT = 40
ENTRIES_PER_SAMPLED_PAGE = 25

# How close to the following gameweek's deadline an unconfirmed gameweek is
# allowed to sit before it gets ingested as partial rather than held for
# `data_checked`.
SETTLEMENT_LEAD_HOURS = 24
