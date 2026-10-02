"""Shared HTTP helper with simple retry/backoff."""
from __future__ import annotations

import time
from typing import Any

import httpx

_client = httpx.Client(timeout=15, headers={"User-Agent": "meme_trader/0.1"})


def request(method: str, url: str, *, retries: int = 3, **kw: Any) -> Any:
    delay = 1.0
    for attempt in range(retries):
        try:
            r = _client.request(method, url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


def get(url: str, **kw: Any) -> Any:
    return request("GET", url, **kw)


def post(url: str, **kw: Any) -> Any:
    return request("POST", url, **kw)
