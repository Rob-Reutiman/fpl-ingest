# CLAUDE.md

## Project

FPL Ingest - Scheduled GitHub Actions jobs call the Fantasy Premier League public API and write the response bytes unchanged to R2. A separate manual backfill job loads past seasons from the vaastav community archive into curated Parquet.

## Stack

- Python 3.12, managed with uv
- Sync HTTP via httpx + tenacity (retries)
- boto3 against R2's S3-compatible endpoint
- DuckDB for the backfill transforms (CSV in, Parquet out)
- pydantic-settings for R2 secrets

## Commands

```bash
uv sync --extra dev
uv run pytest                          # unit tests (CI safe)
uv run pytest -m integration           # live FPL API smoke tests (manual only, never CI)
uv run ruff check --fix . && uv run ruff format .
uv run pyright
uv run pre-commit run --all-files      # ruff, hygiene hooks, pyright

uv run python -m fpl.jobs.hourly_current --dry-run
uv run python -m fpl.jobs.backfill --dry-run --cache-dir data/archive --staging-dir data/out
```

## Layout

| Module | Role |
|---|---|
| `constants.py`, `season.py`, `keys.py`, `gameweek.py`, `sampling.py` | pure logic — no network, no bucket, no mocks needed to test |
| `curated_schema.py`, `identity.py` | pure logic, shared by ingest and backfill |
| `transforms/*.py` | pure logic — take a DuckDB connection, never a bucket. `master.py` is the exception: it reads and writes the master tables, so it takes a store |
| `backfill/transform.py`, `backfill/validate.py`, `backfill/report.py` | archive-specific transforms and checks |
| `fpl_client.py`, `r2_client.py`, `backfill/archive.py` | the only I/O |
| `jobs/*.py` | orchestration only |

Keep that separation. Detection and sampling rules go in the pure modules, not
inlined next to a request.

## Transform notes

- `curated_schema.py` is the transcription of `fpl-parquet-schemas.md`. The spec is the
  contract: don't add, drop or reorder a column in one place only, and update the spec
  document first if a column genuinely needs to change.
- Sources are read as text and cast explicitly, never inferred. Type sniffing drifts
  between seasons — an all-empty column infers as BOOLEAN and the glob stops unioning.
- A missing stat is NULL, never 0. This is load-bearing for `defensive_contribution`.
- `expected_goals_conceded` is a *team* figure stored on every player row. Aggregate it with
  MAX; SUM gives ~11x.
- Aggregations that feed a written file need a deterministic order (`sum(x ORDER BY ...)`,
  `min` over carry-alongs). Floating-point addition isn't associative, so parallel
  aggregation otherwise makes re-runs rewrite rows with last-bit-different values.
- Cross-season player matching leads with the stable `code`, not the name. Names get
  relisted between seasons; the code doesn't.
- The README is public-facing and covers what the data is and how to query it. Rationale
  for *why* the pipeline is built this way belongs here, not there.

## Conventions

- Ruff for linting and formatting (not black, not pylint)
- Pyright for type checking (`basic` mode), over `src` and `tests`
- Integration tests use `@pytest.mark.integration` and are excluded from default runs
- Tests make **zero live network calls** — `tests/conftest.py` serves a `FakeAPI`
  through an `httpx.MockTransport`, so jobs exercise the real client
- Config is `get_settings()`, not a module-level singleton: importing anything in
  this package must not require R2 credentials, or the suite can't run without them
- The FPL request tunables live in `constants.py`, deliberately not in env vars

## FPL API notes

- No published rate limit. We self-throttle to ~1 request per 175ms as a courtesy
  (`REQUEST_DELAY_SECONDS`) and set a descriptive `User-Agent`.
- 429s and 5xx are retried with exponential backoff + jitter. **404s are not** —
  a missing entry or an unopened gameweek is not transient.
- `finished: true` on an event means matches ended, but bonus points and autosubs
  are still revised for hours afterwards. `data_checked: true` is the settle
  signal. **Never trigger on `finished` alone.**
- One postponed fixture holds `data_checked` at false for its whole gameweek,
  potentially for months. `gameweek.is_effectively_complete` handles that case and
  the object is tagged `partial` in R2 metadata.
- `entry/{id}/event/{gw}/picks/` 404s until GW `gw`'s deadline passes, and league
  314's standings only describe the GW `gw` cohort once its points settle. That is
  why the manager sample triggers on settlement, not on the upcoming deadline.
- The season prefix is derived from `game_config.settings.static_content_url` on
  every run. Never hardcode a season. It is `YYYY-YY` (`2026-27`) to match the
  schema contract and the archive's directory naming, so live and backfilled
  seasons glob together.
- `event/{gw}/live/` returns **every** player in the game, not just those whose
  club played. Filtering to clubs with a fixture is what enforces the
  blank-gameweek rule; without it a blank looks like a zero-minute appearance.
- `event/{gw}/live/` aggregates stats over a double gameweek. Affected players
  come from `element-summary/{id}` instead, and `source` records which endpoint
  each row came from.
- All three jobs can extend the master tables, so their crons are spread (`:05`
  hourly, `:35` for the dailies) rather than merely distinct.
