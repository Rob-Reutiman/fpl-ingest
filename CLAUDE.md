# CLAUDE.md

## Project

FPL Edge — Fantasy Premier League companion tooling. Async data pipeline: API client → DuckDB → analysis → Streamlit dashboard.

## Stack

- Python 3.12, managed with uv
- Async HTTP via httpx + tenacity (retries)
- DuckDB + Polars for data
- Streamlit + Altair for UI
- pydantic-settings for config

## Commands

```bash
source .venv/bin/activate
pytest                                # unit tests (integration excluded by default)
pytest -m integration                 # live FPL API smoke tests (manual only, never CI)
ruff check --fix . && ruff format .   # lint + format
pyright                               # type check
pre-commit run --all-files            # all hooks
```

## Layout

Source lives under `src/fpl/`, tests under `tests/`. Editable install via `uv pip install -e ".[dev]"`.

## Conventions

- Ruff for linting and formatting (not black, not pylint)
- Pyright for type checking (`basic` mode)
- pytest-asyncio in `strict` mode — async tests need `@pytest.mark.asyncio`
- Integration tests use `@pytest.mark.integration` and are excluded from default runs
- Pre-commit hooks enforce lint/format/types on every commit
- Config loads from env vars / `.env` via pydantic-settings (`from fpl.config import settings`)

## FPL API notes

- Behind Cloudflare — requires realistic User-Agent, bounded concurrency, per-request delays
- Picks and transfers responses are cached to disk (they're immutable once a GW completes)
- 404s are not retried (deleted/invalid manager IDs are not transient)
- 429s and 5xx are retried with exponential backoff + jitter
