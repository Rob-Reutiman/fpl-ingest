"""Settings loaded from the environment or a local `.env` file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """R2 credentials."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str

    @field_validator("r2_bucket")
    @classmethod
    def _reject_endpoint_url(cls, value: str) -> str:
        """Reject an S3 endpoint pasted in as the bucket name.

        boto3 fails on this much later, during request signing, against a regex
        that names neither the setting nor the fix.
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
    """Load settings on first use, caching them thereafter.

    Deferring the load to call time leaves this package importable without
    credentials, so the tests run anywhere.
    """
    return Settings()  # pyright: ignore[reportCallIssue]
