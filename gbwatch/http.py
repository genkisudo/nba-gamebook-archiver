"""HTTP helpers. NBA hosts always get the required browser user-agent."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

NBA_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def make_client() -> httpx.Client:
    return httpx.Client(timeout=30.0, follow_redirects=True)


def headers_for(url: str, extra: dict | None = None) -> dict:
    headers = {}
    host = urlparse(url).netloc.lower()
    if host.endswith("nba.com"):
        headers["User-Agent"] = NBA_UA
    if extra:
        headers.update(extra)
    return headers


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 3,
    sleeper=time.sleep,
    headers: dict | None = None,
    **kwargs,
) -> httpx.Response:
    """Retry network errors and 429/5xx. 404 is returned to the caller."""
    delay = 1.0
    last_exc: Exception | None = None
    merged = headers_for(url, headers)
    for attempt in range(retries):
        try:
            response = client.request(method, url, headers=merged, **kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            sleeper(delay)
            delay *= 2
            continue
        if response.status_code in RETRY_STATUSES and attempt < retries - 1:
            sleeper(delay)
            delay *= 2
            continue
        return response
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{method} {url} failed")
