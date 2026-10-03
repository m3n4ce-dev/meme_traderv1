"""RugCheck public API (read endpoints need no key). Swagger: https://api.rugcheck.xyz/swagger/index.html"""
from __future__ import annotations

from .http import get

BASE = "https://api.rugcheck.xyz/v1"


def report(mint: str) -> dict:
    return get(f"{BASE}/tokens/{mint}/report")
