"""R2 wrapper: existence checks, batching, and metadata passthrough."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from fpl.config import Settings
from fpl.r2_client import NDJSON_CONTENT_TYPE, DryRunStore, R2Client


class StubS3:
    def __init__(self, *, head_error: Exception | None = None) -> None:
        self.head_error = head_error
        self.head_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls.append(kwargs)
        if self.head_error:
            raise self.head_error
        return {"ContentLength": 2}

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")


def test_exists_is_true_when_head_succeeds():
    s3 = StubS3()
    assert R2Client(s3, "bucket").exists("raw/2026-27/gw1/gameweek-live.json") is True
    assert s3.head_calls == [{"Bucket": "bucket", "Key": "raw/2026-27/gw1/gameweek-live.json"}]


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_exists_is_false_when_the_object_is_missing(code: str):
    assert R2Client(StubS3(head_error=_error(code)), "bucket").exists("k") is False


def test_exists_propagates_real_errors():
    """A permissions failure must not be mistaken for 'not ingested yet'."""
    with pytest.raises(ClientError):
        R2Client(StubS3(head_error=_error("AccessDenied")), "bucket").exists("k")


def test_put_bytes_writes_the_body_unchanged_with_metadata():
    s3 = StubS3()
    R2Client(s3, "bucket").put_bytes(
        "raw/2026-27/gw1/gameweek-live.json", b'{"elements":[]}', metadata={"partial": "true"}
    )

    (call,) = s3.put_calls
    assert call["Bucket"] == "bucket"
    assert call["Key"] == "raw/2026-27/gw1/gameweek-live.json"
    assert call["Body"] == b'{"elements":[]}'
    assert call["ContentType"] == "application/json"
    assert call["Metadata"] == {"partial": "true"}


def test_put_json_serializes():
    s3 = StubS3()
    R2Client(s3, "bucket").put_json("k", {"a": 1})
    assert json.loads(s3.put_calls[0]["Body"]) == {"a": 1}


def test_put_ndjson_is_one_write_of_many_lines():
    s3 = StubS3()
    records = [{"entry_id": i} for i in range(2000)]
    R2Client(s3, "bucket").put_ndjson("k", records)

    assert len(s3.put_calls) == 1
    body = s3.put_calls[0]["Body"].decode()
    assert s3.put_calls[0]["ContentType"] == NDJSON_CONTENT_TYPE
    lines = body.splitlines()
    assert len(lines) == 2000
    assert json.loads(lines[0]) == {"entry_id": 0}
    assert json.loads(lines[-1]) == {"entry_id": 1999}


def test_put_ndjson_with_no_records_writes_an_empty_object():
    s3 = StubS3()
    R2Client(s3, "bucket").put_ndjson("k", [])
    assert s3.put_calls[0]["Body"] == b""


def test_dry_run_store_writes_nothing_and_reports_keys_absent():
    store = DryRunStore()
    assert store.exists("raw/2026-27/gw1/gameweek-live.json") is False
    store.put_bytes("k", b"{}")
    store.put_json("k", {})
    store.put_ndjson("k", [{"a": 1}])  # no assertion needed: nothing to assert against


def test_endpoint_url_is_derived_from_the_account_id():
    settings = Settings(
        r2_account_id="abc123",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket="fpl-raw",
    )
    assert settings.r2_endpoint_url == "https://abc123.r2.cloudflarestorage.com"
