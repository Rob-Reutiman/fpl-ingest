"""Async HTTP client for the FPL API.

Handles bounded concurrency, per-request delays, retries with backoff,
and disk-based response caching for crash recovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from fpl.config import settings

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CACHE_DIR = Path(settings.db_path).parent / "cache"

# URL path patterns that get indefinite caching.
_CACHEABLE_PATTERNS = ("/picks/", "/transfers/")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _should_cache(path: str) -> bool:
    return any(pattern in path for pattern in _CACHEABLE_PATTERNS)


class FPLClient:
    """Async HTTP client for the FPL API with rate limiting and caching."""

    def __init__(
        self,
        *,
        base_url: str = settings.fpl_base_url,
        max_concurrent: int = settings.max_concurrent_requests,
        delay: float = settings.request_delay_seconds,
        retry_attempts: int = settings.retry_attempts,
        cache_dir: Path = CACHE_DIR,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._delay = delay
        self._retry_attempts = retry_attempts
        self._cache_dir = cache_dir
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> FPLClient:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str) -> Any:
        """GET {base_url}/{path}, return parsed JSON.

        Cacheable paths (picks, transfers) are served from disk on cache hit.

        The return type is ``Any`` because the shape is endpoint-dependent —
        most endpoints return a JSON object, but ``fixtures/`` returns an array.
        Callers that know the shape should annotate it at the call site.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"

        if _should_cache(path):
            cached = self._read_cache(url)
            if cached is not None:
                logger.debug("CACHE HIT  %s", url)
                return cached

        data = await self._fetch_with_semaphore(url)

        if _should_cache(path):
            self._write_cache(url, data)

        return data

    async def get_many(self, paths: list[str]) -> list[Any]:
        """Concurrent GET for multiple paths, respecting the semaphore."""
        return await asyncio.gather(*(self.get(p) for p in paths))

    async def _fetch_with_semaphore(self, url: str) -> dict:
        async with self._semaphore:
            data = await self._fetch_with_retry(url)
            await asyncio.sleep(self._delay)
            return data

    async def _fetch_with_retry(self, url: str) -> dict:
        @retry(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential_jitter(initial=1, max=10),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        async def _do_fetch() -> dict:
            assert self._client is not None
            resp = await self._client.get(url)
            if resp.status_code >= 400:
                level = (
                    logging.WARNING
                    if resp.status_code in (429,) or resp.status_code >= 500
                    else logging.DEBUG
                )
                logger.log(level, "%s %s", resp.status_code, url)
            else:
                logger.debug("OK %s", url)
            resp.raise_for_status()
            return resp.json()

        return await _do_fetch()

    # -- Disk cache -----------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        return self._cache_dir / f"{_cache_key(url)}.json"

    def _read_cache(self, url: str) -> dict | None:
        path = self._cache_path(url)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def _write_cache(self, url: str, data: dict) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(url).write_text(json.dumps(data))

    def set_transport(self, mock_get: Any) -> None:
        """Replace the underlying client's GET for testing."""
        assert self._client is not None
        self._client.get = mock_get  # type: ignore[method-assign]

    def clear_cache(self) -> int:
        """Delete all cached responses. Returns the number of files removed."""
        if not self._cache_dir.exists():
            return 0
        count = 0
        for f in self._cache_dir.glob("*.json"):
            f.unlink()
            count += 1
        return count
