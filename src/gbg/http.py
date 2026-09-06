"""One throttled HTTP client for everything that talks to BGS.

Design intent: it must be impossible to hammer BGS by accident. Every request
passes through `PoliteClient`, which enforces a global minimum spacing between
requests, retries on transient failures with backoff, and honours Retry-After.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import httpx

from .config import ClientConfig

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class RateLimiter:
    """Minimum spacing between calls, in wall-clock seconds."""

    min_interval: float
    _last: float = 0.0

    def wait(self) -> float:
        now = time.monotonic()
        delay = self._last + self.min_interval - now
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()
        return max(delay, 0.0)


class PoliteClient:
    def __init__(self, cfg: ClientConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.limiter = RateLimiter(min_interval=1.0 / cfg.requests_per_second)
        self._client = httpx.Client(
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json, application/pdf"},
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, url: str, **kwargs) -> httpx.Response:
        """GET with throttling and bounded retry. Raises after the last attempt."""
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            self.limiter.wait()
            self.request_count += 1
            try:
                resp = self._client.get(url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                self._backoff(attempt, None)
                continue
            if resp.status_code in RETRYABLE_STATUS and attempt < self.cfg.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            return resp
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            time.sleep(int(retry_after))
            return
        time.sleep(min(2**attempt + random.random(), 60))
