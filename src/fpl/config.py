"""Settings loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """R2 credentials"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str

    @field_validator("r2_bucket")
    @classmethod
    def _reject_endpoint_url(cls, value: str) -> str:
        """Catch the easy mix-up of pasting the S3 endpoint in as the bucket.

        boto3 otherwise fails deep in request signing with a bucket-name regex,
        which says nothing about which setting is actually wrong.
        """
        if "://" in value or "/" in value or "r2.cloudflarestorage.com" in value:
            raise ValueError(
                f"R2_BUCKET should be the bucket name, not a URL (got {value!r}). "
                "The endpoint is derived from R2_ACCOUNT_ID."
            )
        return value

    @property
    def r2_endpoint_url(self) -> str:
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"


@lru_cache
def get_settings() -> Settings:
    """Load settings on first use.

    Deliberately not a module-level singleton: importing any module in this
    package must not require R2 credentials to be present, or the test suite
    can't run without them.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from env
