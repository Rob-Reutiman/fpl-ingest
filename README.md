# fpl-edge

A Fantasy Premier League data warehouse that builds itself. Scheduled GitHub Actions
jobs pull from the [FPL public API](https://fantasy.premierleague.com/api/) into a
Cloudflare R2 bucket and transform it into Parquet you can query directly with DuckDB —
four seasons of per-fixture player stats, expected goals, team form and the squads of the
top-ranked managers.

## What you get

```
raw/       untouched API JSON — append-only, never deleted
curated/   Parquet — the query layer
```

The FPL API only ever exposes *current* state, so anything you didn't capture is gone.
`raw/` keeps every response as it arrived; `curated/` is fully regenerable from it, which
makes a transform bug a re-run rather than a data loss.

| Table | Grain | What's in it |
|---|---|---|
| `fact_player_fixture` | player × fixture | Minutes, goals, xG, xA, xGC, defensive contribution, BPS, bonus, points |
| `fact_team_fixture` | team × fixture | Goals for/against, xG for/against, clean sheet, result |
| `fact_player_gameweek_fpl` | player × gameweek | Price, ownership, transfers in/out |
| `fact_manager_pick` | manager × pick | The squads of ~2,000 top-ranked managers |
| `agg_player_ownership` | player × gameweek × cohort | Ownership, start rate and captaincy among those managers |
| `fpl_current` | player | Live price, injury news, form — refreshed hourly |
| `dim_player`, `dim_team`, `dim_fixture`, `dim_gameweek` | — | Names, fixtures, schedule, deadlines |
| `curated/master/*` | — | Cross-season identity: stable ids for players and teams |

Seasons 2023-24 onward, and it keeps going as the current one plays out.

## Querying it

Everything is plain Parquet, so DuckDB reads it straight out of the bucket:

```sql
INSTALL httpfs; LOAD httpfs;
CREATE SECRET r2 (TYPE r2, KEY_ID '…', SECRET '…', ACCOUNT_ID '…');
```

FPL reassigns player ids every season, so joins across seasons go through
`player_master_id` — never `element_id`:

```sql
SELECT m.canonical_web_name, f.season, sum(f.total_points) AS points, sum(f.expected_goals) AS xg
FROM read_parquet('r2://my-bucket/curated/*/fact_player_fixture.parquet') f
JOIN read_parquet('r2://my-bucket/curated/master/dim_player_master.parquet') m USING (player_master_id)
GROUP BY 1, 2 ORDER BY points DESC LIMIT 10;
```

Who the best managers own that the field doesn't — the differential signal:

```sql
SELECT o.element_id, c.web_name, o.ownership_pct, c.selected_by_percent
FROM read_parquet('curated/2026-27/agg_player_ownership.parquet') o
JOIN read_parquet('curated/2026-27/fpl_current.parquet') c USING (element_id)
WHERE o.sample_group = 'top1000' AND o.gameweek = 7
ORDER BY o.ownership_pct - c.selected_by_percent DESC;
```

Derived analytics — dynamic FDR, form windows, predicted points — are deliberately not
materialized. They are query-time SQL over these tables, so you can tune a model without
rerunning a pipeline.

## Reading the data correctly

Three conventions are worth knowing before you write a rolling average.

**A blank gameweek has no row.** If a player's club had no fixture, there is no row at
all. If their club played and they didn't feature, there is a row with `minutes = 0`.
Those are different facts and the tables keep them apart.

**A double gameweek has two rows,** one per fixture, with distinct `fixture_id`s — not
one row holding the total. `team_id` is pinned to the fixture, so a January transfer
never reattributes an August match to the player's new club.

**NULL means "not measured", 0 means "measured, and it was zero".** Stats that didn't
exist in an older season are NULL throughout it. `defensive_contribution` before 2025-26
is the case you'll hit; coalescing it to 0 will quietly poison any cross-season model.

One more, for xG against: `xg_against` is the opposing side's summed xG and is the
measure to use. `xgc_reported` is FPL's own per-player figure, kept only as a
cross-check — it is a *team* value copied onto every player row, so summing it gives
roughly eleven times the truth.

## The jobs

| Workflow | Schedule (UTC) | What it does |
|---|---|---|
| `hourly-current` | :05 hourly | Snapshots `bootstrap-static` and `fixtures`; rebuilds `fpl_current` and the dimensions |
| `gameweek-live` | 05:35 daily | Ingests a settled gameweek's match facts. No-op most days |
| `manager-sample` | 06:35 daily | Harvests ~2,000 managers' picks and aggregates ownership. No-op most days |
| `backfill` | manual | Loads historical seasons from the community archive |

A gameweek counts as settled once FPL reports `finished` **and** `data_checked` — bonus
points and autosubs are revised for hours after the final whistle. The daily jobs check
the bucket for the object they would write and exit cleanly if it's already there, so
they're safe to run any day and work off a backlog one gameweek per run. The hourly job
is the deliberate opposite: prices and injury news describe the present, so it always
overwrites.

The manager sample covers ranks 1–10,000 of the overall league: every entry in the top
1,000 (group `top1000`), plus 40 pages drawn at random from the rest with 25 entries kept
from each (group `sampled`). The draw is seeded on the season and gameweek, so a re-run
repeats it.

## Historical backfill

Past seasons come from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League),
the community archive of FPL data supplemented with xG/xA from Understat, transformed
into the same schema as the live tables.

```bash
gh workflow run backfill.yml                    # 2023-24, 2024-25, 2025-26
gh workflow run backfill.yml -f seasons=2025-26
```

Re-running is safe: existing `player_master_id`s are read back and extended rather than
reassigned, and a second run reproduces the same files byte for byte. Nothing is uploaded
until every season has built *and* passed validation, so you never get a half-loaded
bucket you can't distinguish from a complete one. Each run writes
`curated/master/backfill_report.md` describing what it loaded.

A few things are deliberately left out of backfilled seasons:

- **`xP`** — scraped from FPL *after* a gameweek ends, so it may encode post-match
  information rather than the pre-deadline prediction managers actually saw. Loading it
  would leave a target-leakage trap in the feature set.
- **Assistant managers** — 2024-25's `AM` asset. Not players, and retired by FPL after
  that season.
- **`dim_gameweek` deadline and scoring columns** — NULL, because the archive has no
  events file and they can't be recovered after the fact.

> The underlying data belongs to fantasy.premierleague.com and understat.com. The archive
> repo's *code* is MIT; its data is not ours to republish as our own dataset.

## Running your own copy

You need a Cloudflare R2 bucket and an API token.

1. **R2 → Create bucket**, then **Manage R2 API Tokens → Create API token** with
   **Object Read & Write** scoped to it. The secret is shown once.
2. Your account ID is in the R2 overview page's S3 endpoint,
   `https://<account-id>.r2.cloudflarestorage.com`.
3. Fork this repo and add four secrets under **Settings → Secrets and variables →
   Actions**: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.
4. Run `backfill.yml` once to load the history, then let the schedules take over.

Every workflow has a manual trigger with an optional `dry_run` input, which fetches as
normal but logs the writes instead of performing them:

```bash
gh workflow run hourly-current.yml -f dry_run=true && gh run watch
```

The season prefix is derived from the API on every run, never hardcoded, so rollover
needs no code change — the jobs simply start writing under `curated/2027-28/`.

> GitHub disables scheduled workflows on a repo with no activity for 60 days. If the
> crons go quiet over the summer, push a commit or re-enable them.

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

Jobs run locally against your own bucket. Start with `--dry-run`:

```bash
uv run python -m fpl.jobs.hourly_current --dry-run
```

The backfill caches its downloads and can stage its Parquet locally, so you can inspect
the output before it goes near a bucket:

```bash
uv run python -m fpl.jobs.backfill --dry-run --cache-dir data/archive --staging-dir data/out
```

## Layout

```
src/fpl/
  constants.py season.py keys.py gameweek.py sampling.py   pure logic, no I/O
  curated_schema.py identity.py                            the column contract, and cross-season ids
  transforms/                                              DuckDB SQL — take a connection, never a bucket
  backfill/                                                archive-specific transforms and checks
  fpl_client.py r2_client.py                               the only network and bucket access
  jobs/                                                    orchestration, one module per workflow
```

Not affiliated with the Premier League or Fantasy Premier League.
