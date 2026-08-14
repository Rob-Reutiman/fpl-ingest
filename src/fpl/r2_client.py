"""Writes to Cloudflare R2 over its S3-compatible API."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from fpl.config import Settings, get_settings

logger = logging.getLogger(__name__)

JSON_CONTENT_TYPE = "application/json"
NDJSON_CONTENT_TYPE = "application/x-ndjson"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
CSV_CONTENT_TYPE = "text/csv"
MARKDOWN_CONTENT_TYPE = "text/markdown"

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}


def _to_ndjson(records: Iterable[Any]) -> bytes:
    lines = [json.dumps(record, separators=(",", ":")) for record in records]
    return ("\n".join(lines) + "\n").encode() if lines else b""


class ObjectStore(Protocol):
    """The read/write surface the jobs depend on."""

    def exists(self, key: str) -> bool: ...

    def get_bytes(self, key: str) -> bytes | None: ...

    def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = JSON_CONTENT_TYPE,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    def put_json(self, key: str, obj: Any, *, metadata: dict[str, str] | None = None) -> None: ...

    def put_ndjson(
        self, key: str, records: Iterable[Any], *, metadata: dict[str, str] | None = None
    ) -> None: ...


class R2Client:
    """Thin boto3 wrapper. One PutObject per call — batch before you write."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> R2Client:
        settings = settings or get_settings()
        client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                # R2 rejects the CRC32 integrity headers boto3 >=1.36 sends by
                # default on every upload.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_supported",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, settings.r2_bucket)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND:
                return False
            raise
        return True

    def get_bytes(self, key: str) -> bytes | None:
        """Return the object's bytes, or None if it isn't there."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND:
                return None
            raise
        body: bytes = response["Body"].read()
        return body

    def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = JSON_CONTENT_TYPE,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata=metadata or {},
        )
        logger.info("wrote s3://%s/%s (%d bytes)", self._bucket, key, len(body))

    def put_json(self, key: str, obj: Any, *, metadata: dict[str, str] | None = None) -> None:
        self.put_bytes(key, json.dumps(obj).encode(), metadata=metadata)

    def put_ndjson(
        self, key: str, records: Iterable[Any], *, metadata: dict[str, str] | None = None
    ) -> None:
        self.put_bytes(
            key,
            _to_ndjson(records),
            content_type=NDJSON_CONTENT_TYPE,
            metadata=metadata,
        )


class DryRunStore:
    """Logs what would be written. Reports every key as absent so a dry run
    exercises the whole job rather than short-circuiting on idempotency."""

    def exists(self, key: str) -> bool:
        logger.info("[dry-run] exists? %s -> False", key)
        return False

    def get_bytes(self, key: str) -> bytes | None:
        # Reads would be harmless, but a dry run must work without credentials,
        # so every key reads as absent — consistent with `exists`.
        logger.info("[dry-run] get %s -> absent", key)
        return None

    def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = JSON_CONTENT_TYPE,
        metadata: dict[str, str] | None = None,
    ) -> None:
        logger.info(
            "[dry-run] would write %s (%d bytes, %s, metadata=%s)",
            key,
            len(body),
            content_type,
            metadata or {},
        )

    def put_json(self, key: str, obj: Any, *, metadata: dict[str, str] | None = None) -> None:
        self.put_bytes(key, json.dumps(obj).encode(), metadata=metadata)

    def put_ndjson(
        self, key: str, records: Iterable[Any], *, metadata: dict[str, str] | None = None
    ) -> None:
        self.put_bytes(
            key, _to_ndjson(records), content_type=NDJSON_CONTENT_TYPE, metadata=metadata
        )


def build_store(*, dry_run: bool) -> ObjectStore:
    return DryRunStore() if dry_run else R2Client.from_settings()
