# FPL Ingest

Scheduled jobs with Github Actions that pull data from the [Fantasy Premier League public
API](https://fantasy.premierleague.com/api/) into a Cloudflare R2 bucket, and transform
it into Parquet tables.

## Two layers

```
raw/       untouched API JSON — the replayable source of truth
curated/   Parquet — the query layer, read by DuckDB
```

Raw is append-only and never deleted: the FPL API only exposes *current* state, so a
moment you didn't capture is gone for good. Curated is fully regenerable from raw, so
a transform bug is a re-run rather than a data loss.

## The jobs

| Workflow | Schedule (UTC) | What it does |
|---|---|---|
| `hourly-current` | :05 hourly | Snapshots `bootstrap-static/` and `fixtures/`; rebuilds `fpl_current` and the four dimension tables. Always overwrites. |
| `gameweek-live` | 05:35 daily | Ingests `event/{gw}/live/` for the earliest settled gameweek not already stored, and rebuilds the match facts. No-op most days. |
| `manager-sample` | 06:35 daily | Harvests ~2,000 high-ranked managers' picks for that gameweek and aggregates ownership. No-op most days. |

Jobs 2 and 3 are safe to run any day: each checks R2 for the object it would write and
exits cleanly if it's already there. **Job 1 is deliberately the opposite** — prices,
injury news and ownership describe the present, so there is no older version worth
keeping and it overwrites unconditionally.

The schedules are spread rather than merely distinct. All three jobs can extend the
shared master tables, and Actions cron is approximate, so `:35` keeps the daily jobs as
far as possible from the hourly `:05` slot.

### What each table costs

| Table | Job | Cadence | Write mode |
|---|---|---|---|
| `fpl_current` | 1 | Hourly | Overwrite |
| `dim_player`, `dim_team`, `dim_fixture`, `dim_gameweek` | 1 | Hourly | Overwrite |
| `fact_player_fixture` | 2 | On `data_checked` | Whole-file rewrite |
| `fact_team_fixture` | 2 | On `data_checked` | Full regenerate |
| `fact_manager_pick`, `agg_player_ownership` | 3 | On `data_checked` | Whole-file rewrite |
| master ID tables | 1, 2, 3 | When a squad changes | Extend |

`fact_player_gameweek_fpl` is **not** populated live. It is price and ownership history,
which the backfill already provides for past seasons and which nothing forward-looking
needs. It can be started at any gameweek later without invalidating anything.

### Double gameweeks need a second endpoint

`event/{gw}/live/` reports a player's stats **aggregated over the gameweek**. Its
`explain` array is split per fixture, but only carries point-scoring identifiers — and
xG/xA don't score points, so the per-fixture xG split isn't recoverable from it. When a
club plays twice, those players are fetched from `element-summary/{id}` instead, whose
`history[]` is genuinely per-fixture and includes xG. That's ~40–60 extra requests, a
handful of times a season, and only for the affected clubs.

Each row records which endpoint it came from in `source` (`event_live` or
`element_summary`). Whenever the fallback runs, the job also sums the per-fixture rows
and compares them against the live aggregate, logging a warning if they disagree — that
check is what would catch either endpoint changing shape, which is otherwise the sort of
thing you discover months later in a model that has quietly been wrong.

> The `explain` reasoning above is from the API's documented shape, not from a live
> double gameweek — the season hadn't started when this was built. The design doesn't
> depend on it being right: `element-summary` is per-fixture either way, and the
> reconciliation check reports the truth the first time a real DGW runs.

### The blank-vs-benched distinction

`event/{gw}/live/` returns every player in the game, whether or not their club played.
Writing that through would put a zero-minute row against a **blank** gameweek, and every
rolling window downstream would read it as a genuine non-appearance. So:

- club played, player didn't feature → a row with `minutes = 0`
- club had no fixture → **no row at all**

`team_id` is pinned to the fixture, taken from the bootstrap snapshot in the same run.
Football facts are immutable once written, so a January transfer can't reach back and
reattribute an August fixture to the wrong club.

### When a gameweek counts as "settled"

`finished: true` on an event only means the matches ended — bonus points and
autosubs are still revised for hours afterwards. The jobs wait for
`finished && data_checked`.

One postponed fixture can hold `data_checked` at false for an entire gameweek
indefinitely. When every unfinished fixture in an event has a null or far-future
kickoff, the gameweek is ingested anyway and the object is tagged with R2 object
metadata `partial=true` and `pending-fixtures=<ids>`. The key is unchanged, and
the stored bytes are still exactly what the API returned.

### Why the manager sample waits for settlement

The brief for this repo originally triggered it ~20 hours *before* a deadline.
That can't work: `entry/{id}/event/{gw}/picks/` returns 404 until GW `gw`'s
deadline has passed, and league 314's standings only describe the GW `gw` cohort
once its points have settled. Waiting for settlement gets the right cohort and
the right squads from the same moment, both filed under the same gameweek.

### The sample

- **Top 1,000** — every entry from standings pages 1–20 of league 314, group `top1000`.
- **Ranks 1,001–10,000** — 40 pages drawn at random from pages 21–200, 25 entries
  kept from each, group `sampled`. Spreading across the range beats taking a
  contiguous block for the same entry budget.

The page draw is seeded on `{season}:{gw}`, so a re-run repeats it.

## Historical backfill

A separate, manually-triggered job loads past seasons from
[vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) — the
community archive of FPL data, supplemented with xG/xA from Understat — into curated
Parquet conforming to `fpl-parquet-schemas.md`.

```bash
gh workflow run backfill.yml                              # 2023-24, 2024-25, 2025-26
gh workflow run backfill.yml -f dry_run=true
gh workflow run backfill.yml -f seasons=2025-26
```

It is safe to re-run: existing `player_master_id`s are read back from the bucket and
extended rather than reassigned, every table is written in a deterministic order, and a
second run reproduces the same files byte for byte (`backfill_report.md` aside, which
carries a run timestamp).

> The data belongs to fantasy.premierleague.com and understat.com. The archive repo's
> *code* is MIT; its data is not ours to republish as our own dataset.

### What it writes

```
curated/{season}/dim_player.parquet
curated/{season}/dim_team.parquet
curated/{season}/dim_fixture.parquet
curated/{season}/dim_gameweek.parquet
curated/{season}/fact_player_fixture.parquet
curated/{season}/fact_team_fixture.parquet
curated/{season}/fact_player_gameweek_fpl.parquet
curated/master/dim_player_master.parquet
curated/master/map_player_season.parquet
curated/master/dim_team_master.parquet
curated/master/map_team_season.parquet
curated/master/player_match_review.csv
curated/master/backfill_report.md
raw/{season}/archive/{source file}
```

The source CSVs go to `raw/` unmodified, so a transform bug is fixable by re-running the
transform without re-fetching from a third-party repo that may have changed.

Nothing is uploaded until every season has built *and* passed validation — a half-loaded
bucket is worse than an empty one, because a consumer can't tell the difference.

### Cross-season identity

FPL reassigns `element` ids every season, so a query spanning two seasons is meaningless
without a stable key. Players are matched on `players_raw.code` (the Premier League player
code, stable across seasons), falling back to a normalized name, then team continuity, then
a fresh master id. Anything not matched on the code is written to `player_match_review.csv`.

The code has to lead: FPL relists players' names between seasons, and name-only matching
splits 66 careers across these three seasons alone — Rodri, Kepa, Merino and Ugarte among
them. Teams are simpler; their three-letter `short_name` is already stable and is used
directly as `team_master_id`.

### Deliberate exclusions

- **`xP` is not loaded.** It's scraped from FPL's `ep_this` *after* a gameweek concludes,
  so it may encode post-match information rather than the pre-deadline prediction managers
  actually saw. As a feature it is a target-leakage risk, so it is dropped rather than
  loaded and left as a trap.
- **Assistant managers** (2024-25's `AM` asset, `element_type` 5) are dropped — not
  players, not in the schema's 1–4 positions, and retired by FPL after that season.
- **`tackles`, `recoveries`, `clearances_blocks_interceptions`** (2025-26 only) aren't in
  the schema spec, so they're dropped rather than invented into it.
- **`dim_gameweek` deadline and scoring columns are NULL** for backfilled seasons. The
  archive has no events file and they can't be recovered after the fact.
- **`defensive_contribution` is NULL, never 0,** before 2025-26. Null means "not measured
  that season"; zero means "measured, and it was zero". Conflating them poisons any
  cross-season model.

## R2 key layout

```
raw/{season}/current/bootstrap-static.json      # overwritten hourly
raw/{season}/current/fixtures.json              # overwritten hourly
raw/{season}/daily/bootstrap-static/{date}.json # provenance, one per day
raw/{season}/daily/fixtures/{date}.json
raw/{season}/gw{N}/gameweek-live.json
raw/{season}/gw{N}/fixtures.json
raw/{season}/gw{N}/element-summary/{id}.json    # double gameweeks only
raw/{season}/gw{N}/standings-top1000.json
raw/{season}/gw{N}/standings-sample.json
raw/{season}/gw{N}/manager-picks.ndjson
raw/{season}/gw{N}/manager-picks-summary.json

curated/{season}/fpl_current.parquet
curated/{season}/dim_player.parquet
curated/{season}/dim_team.parquet
curated/{season}/dim_fixture.parquet
curated/{season}/dim_gameweek.parquet
curated/{season}/fact_player_fixture.parquet
curated/{season}/fact_team_fixture.parquet
curated/{season}/fact_manager_pick.parquet
curated/{season}/agg_player_ownership.parquet
curated/master/...                              # cross-season, not season-scoped
```

`current/` is the live snapshot the transform reads and is overwritten every hour. The
dated copies exist because of that: without them an un-captured hour would be gone, and
curated has to stay rebuildable from raw. Everything else keys off the gameweek number,
which is what the idempotency checks and any consumer join on.

`{season}` is **derived on every run** from `bootstrap-static`'s
`game_config.settings.static_content_url` (`.../2026_27/` → `2026-27`), never hardcoded.
Season rollover needs no code change: the jobs just start writing under a new prefix.
The `YYYY-YY` form is the schema contract's and matches the archive's directory naming,
so a season ingested live and a season loaded by the backfill glob together.

## Querying it

```sql
-- every season, live and backfilled, in one glob
SELECT season, count(*) FROM read_parquet('curated/*/fact_player_fixture.parquet')
GROUP BY 1 ORDER BY 1;

-- who the best managers own that the field doesn't
SELECT o.element_id, c.web_name, o.ownership_pct, c.selected_by_percent
FROM read_parquet('curated/2026-27/agg_player_ownership.parquet') o
JOIN read_parquet('curated/2026-27/fpl_current.parquet') c USING (element_id)
WHERE o.sample_group = 'top1000' AND o.gameweek = 7
ORDER BY o.ownership_pct - c.selected_by_percent DESC;
```

Derived analytics — dynamic FDR, form windows, predicted points — are deliberately
**not** materialized. They're query-time SQL over these tables, so the models stay
tunable without a pipeline rerun.

`manager-picks.ndjson` is one JSON object per line:

```json
{"entry_id": 100001, "gameweek": 7, "group": "top1000", "rank": 1, "data": { ...verbatim picks response... }}
```

`manager-picks-summary.json` is written **last** and is the idempotency marker for
`manager-sample` — its presence means a run completed, so a job killed mid-harvest
is retried rather than skipped. It records the cohort size, success/failure counts,
every failed `entry_id`, and which pages were sampled.

## Setup

### 1. Create the bucket and an API token

In the Cloudflare dashboard: **R2 → Create bucket**, then **Manage R2 API Tokens →
Create API token** with **Object Read & Write** permission scoped to that bucket.
Note the Access Key ID and Secret Access Key — the secret is shown once.

Your account ID is in the R2 overview page's S3 endpoint,
`https://<account-id>.r2.cloudflarestorage.com`.

### 2. Add the repo secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Value |
|---|---|
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 API token access key ID |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET` | Bucket name |

> GitHub disables scheduled workflows on a repo with no activity for 60 days.
> If the crons go quiet in the off-season, push a commit or re-enable them.

## Running a job manually

Every workflow has `workflow_dispatch` with an optional `dry_run` input, which
fetches as normal but logs the writes instead of performing them.

From the **Actions** tab pick the workflow → **Run workflow**, or:

```bash
gh workflow run hourly-current.yml
gh workflow run gameweek-live.yml
gh workflow run manager-sample.yml -f dry_run=true

gh run watch
```

## Local development

```bash
uv sync --extra dev
cp .env.example .env      # fill in the four R2 values
uv run pre-commit install
```

```bash
uv run pytest                     # fully mocked, no network
uv run pytest -m integration      # live read-only calls to the FPL API
uv run ruff check --fix . && uv run ruff format .
uv run pyright
```

Run a job against your bucket — start with `--dry-run`:

```bash
uv run python -m fpl.jobs.hourly_current --dry-run
uv run python -m fpl.jobs.hourly_current
uv run python -m fpl.jobs.gameweek_live
uv run python -m fpl.jobs.manager_sample
```

The backfill caches its downloads and can stage its Parquet locally, so you can inspect
the output before it goes near the bucket:

```bash
uv run python -m fpl.jobs.backfill --dry-run --cache-dir data/archive --staging-dir data/out
```
