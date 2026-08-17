# fpl-edge

A Fantasy Premier League data warehouse that builds itself. Scheduled GitHub Actions
jobs pull from the [FPL public API](https://fantasy.premierleague.com/api/) into a
Cloudflare R2 bucket and transform it into Parquet. 

## The jobs

| Workflow | Schedule (UTC) | What it does |
|---|---|---|
| `hourly-current` | :05 hourly | Snapshots `bootstrap-static` and `fixtures`; rebuilds `fpl_current` and the dimensions |
| `gameweek-live` | every 4h | Ingests a settled gameweek's match facts. No-op most runs |
| `manager-sample` | every 4h | Harvests ~2,000 managers' picks and aggregates ownership. No-op most runs |
| `backfill` | manual | Loads historical seasons from the community archive |

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

Seasons 2023-24 onward (captured weekly starting in 2026-27; backfilled from 2023-2026), and it updates itself the current season plays out.

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

## Reading the data correctly
**A blank gameweek has no row.** If a player's club had no fixture, there is no row at
all. If their club played and they didn't feature, there is a row with `minutes = 0`.
Those are different facts and the tables keep them apart.

**A double gameweek has two rows,** one per fixture, with distinct `fixture_id`s — not
one row holding the total. `team_id` is pinned to the fixture, so a January transfer
never reattributes an August match to the player's new club.

**NULL means "not measured", 0 means "measured, and it was zero".** Stats that didn't
exist in an older season are NULL throughout it. `defensive_contribution` before 2025-26
is the case you'll hit; coalescing it to 0 will quietly poison any cross-season model.

A gameweek counts as settled once FPL reports `data_checked`; bonus points and autosubs
are revised for hours after the final whistle. The two end-of-gameweek jobs check the 
bucket for the object they would write and exit cleanly if it's already there. The hourly 
job always overwrites since we only care about current state.

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
```

Re-running is safe.

A few things are deliberately left out of backfilled seasons:

- **`xP`** — scraped from FPL *after* a gameweek ends, so it may encode post-match
  information rather than the pre-deadline prediction managers actually saw
- **Assistant managers** — 2024-25's `AM` asset. Not players, and retired by FPL after
  that season
- **`dim_gameweek` deadline and scoring columns** — NULL, because the archive has no
  events file and they can't be recovered after the fact.

> The underlying data belongs to fantasy.premierleague.com and understat.com. The archive
> repo's *code* is MIT; its data is not ours to republish as our own dataset.

Not affiliated with the Premier League or Fantasy Premier League.
