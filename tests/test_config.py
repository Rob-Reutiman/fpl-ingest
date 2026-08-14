"""Settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fpl.config import Settings


def make(bucket: str) -> Settings:
    return Settings(
        r2_account_id="acct",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket=bucket,
    )


def test_a_plain_bucket_name_is_accepted():
    assert make("fpl-data").r2_bucket == "fpl-data"


def test_the_endpoint_url_is_derived_from_the_account_id():
    assert make("fpl-data").r2_endpoint_url == "https://acct.r2.cloudflarestorage.com"


@pytest.mark.parametrize(
    "bucket",
    [
        "https://acct.r2.cloudflarestorage.com",
        "acct.r2.cloudflarestorage.com",
        "bucket/prefix",
    ],
)
def test_pasting_the_endpoint_in_as_the_bucket_is_rejected(bucket: str):
    """boto3's own error names a regex, not the setting that's wrong."""
    with pytest.raises(ValidationError, match="should be the bucket name"):
        make(bucket)
