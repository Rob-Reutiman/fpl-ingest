"""HTTP client for the public FPL API.

Every method returns the response body verbatim — callers that need structure
parse it themselves. Keeping the bytes intact is what lets the ingest jobs
write exactly what the API returned.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

from fpl.constants import (
    FPL_BASE_URL,
    OVERALL_LEAGUE_ID,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_ATTEMPTS,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Transient failures only — a 404 means the resource isn't there yet."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    # Covers timeouts, connection resets and protocol errors.
    return isinstance(exc, httpx.TransportError)


class FPLClient:
    """Throttled, retrying HTTP client for the FPL API."""

    def __init__(
        self,
        *,
        base_url: str = FPL_BASE_URL,
        delay: float = REQUEST_DELAY_SECONDS,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_wait: wait_base | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._delay = delay
        self._timeout = timeout
        self._transport = transport
        self._last_request: float | None = None
        self._client: httpx.Client | None = None
        self._retrying = Retrying(
            stop=stop_after_attempt(retry_attempts),
            wait=retry_wait or wait_exponential_jitter(initial=1, max=10),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

    def __enter__(self) -> FPLClient:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(self._timeout),
            transport=self._transport,
        )
        return self

    def __exit__(self, *args: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- Endpoints ------------------------------------------------------------

    def bootstrap_static(self) -> bytes:
        return self.get("bootstrap-static/")

    def fixtures(self, event: int | None = None) -> bytes:
        params = {"event": event} if event is not None else None
        return self.get("fixtures/", params=params)

    def event_live(self, gw: int) -> bytes:
        return self.get(f"event/{gw}/live/")

    def element_summary(self, element_id: int) -> bytes:
        """One player's per-fixture history. Only needed for double gameweeks."""
        return self.get(f"element-summary/{element_id}/")

    def standings_page(self, page: int) -> bytes:
        return self.get(
            f"leagues-classic/{OVERALL_LEAGUE_ID}/standings/",
            params={"page_standings": page},
        )

    def entry_picks(self, entry_id: int, gw: int) -> bytes:
        return self.get(f"entry/{entry_id}/event/{gw}/picks/")

    # -- Transport ------------------------------------------------------------

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        """GET the path relative to the API base and return the raw body."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        return self._retrying(self._request, url, params)

    def _request(self, url: str, params: dict[str, Any] | None) -> bytes:
        if self._client is None:
            raise RuntimeError("FPLClient must be used as a context manager")

        self._throttle()
        response = self._client.get(url, params=params)
        if response.status_code >= 400:
            logger.warning("%s %s", response.status_code, response.url)
        response.raise_for_status()
        return response.content

    def _throttle(self) -> None:
        """Cap the request rate, measured from the start of the last request."""
        now = time.monotonic()
        if self._last_request is not None:
            remaining = self._delay - (now - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request = time.monotonic()
