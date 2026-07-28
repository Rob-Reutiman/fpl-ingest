# FPL Edge

Fantasy Premier League companion tooling.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in MY_MANAGER_ID
```

## Test

```bash
pytest
```

## Layout

```
src/fpl/
  config.py       # Settings from env / .env (pydantic-settings)
  models.py       # Shared dataclasses: Player, Pick, Transfer
  constants.py    # API base URL, league IDs, cohort strategy
```
