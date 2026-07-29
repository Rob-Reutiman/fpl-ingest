# FPL Edge

Fantasy Premier League companion tooling.

## Quick start

Requires Python 3.10+ and [uv](https://github.com/astral-sh/uv).

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env         # fill in MY_MANAGER_ID
pre-commit install
```

## Daily

```bash
# Run unit tests
pytest

# Run integration tests (live API smoke tests)
pytest -m integration

# lint, fix, format
ruff check --fix . && ruff format .

# type check
pyright
```

Ruff + Pyright + pytest run automatically on commit via pre-commit. Or run all manually with `pre-commit run --all-files`.
