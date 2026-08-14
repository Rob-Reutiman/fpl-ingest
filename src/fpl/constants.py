"""Tunables and FPL API facts shared across the ingest jobs."""

FPL_BASE_URL = "https://fantasy.premierleague.com/api"

# The FPL API publishes no rate limit. One request per ~175ms is a courtesy
# default that keeps a ~2,000-call manager sample under ten minutes.
REQUEST_DELAY_SECONDS = 0.175
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_ATTEMPTS = 3

USER_AGENT = "fpl-ingest/0.1 (+https://github.com/robreutiman/fpl-ingest)"

# League 314 is the global "Overall" league every entry is a member of.
OVERALL_LEAGUE_ID = 314
ENTRIES_PER_PAGE = 50

# Ranks 1–1,000: every entry on pages 1–20.
TOP_PAGE_COUNT = 20

# Ranks 1,001–10,000: pages 21–200. Rather than take every entry from a
# contiguous block, sample pages across the whole range and take a subset of
# each — same entry budget, far better spread over the rank distribution.
SAMPLE_PAGE_START = 21
SAMPLE_PAGE_END = 200
SAMPLE_PAGE_COUNT = 40
ENTRIES_PER_SAMPLED_PAGE = 25

# A fixture whose kickoff is this far out (or null) is treated as postponed
# rather than merely upcoming, when deciding if a gameweek is done.
POSTPONED_FIXTURE_THRESHOLD_HOURS = 24
