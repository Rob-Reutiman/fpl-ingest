"""Application settings, loaded from environment variables / a local .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for FPL Edge.

    Values are read from the process environment, falling back to a local
    ``.env`` file, then to the defaults declared here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    fpl_base_url: str = "https://fantasy.premierleague.com/api"
    db_path: str = "data/fpl.duckdb"
    my_manager_id: int | None = None

    # Starting year of the current season (2026 => the 2026/27 season). Feeds
    # the cohort sampling seed, so it must be bumped each August.
    season_year: int = 2026

    # HTTP client tunables
    max_concurrent_requests: int = 5
    request_delay_seconds: float = 0.2
    retry_attempts: int = 3

    # Cohort sampling
    cohort_sample_size: int = 10_000
    cohort_top_slice: int = 5_000
    cohort_random_slice: int = 5_000


settings = Settings()
