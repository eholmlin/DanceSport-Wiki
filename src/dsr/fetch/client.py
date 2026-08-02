"""Polite HTTP client: serial requests, per-host delay, descriptive UA, retries.

Per spec section 2 ("Politeness"): 1-2s delay, descriptive User-Agent with a
contact email, respect robots.txt (checked manually per source before adding
entry points here — this client does not fetch robots.txt itself).
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

CONTACT_EMAIL = "eholmlin@gmail.com"
USER_AGENT = f"DancesportResultsBot/0.1 (+contact: {CONTACT_EMAIL})"
DEFAULT_DELAY_SECONDS = 1.5


class PoliteFetcher:
    """Serial, rate-limited HTTP GET client with one delay-tracker per host."""

    def __init__(self, delay_seconds: float = DEFAULT_DELAY_SECONDS, timeout: float = 20.0):
        self.delay_seconds = delay_seconds
        self._last_request_at: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )

    def _wait_for_host(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            remaining = self.delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at[host] = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, url: str) -> httpx.Response:
        response = self._client.get(url)
        if response.status_code >= 500:
            response.raise_for_status()
        return response

    def get(self, url: str) -> httpx.Response:
        self._wait_for_host(url)
        return self._get(url)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteFetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
