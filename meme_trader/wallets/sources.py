"""Free market-data sources for the wallet study.

GeckoTerminal (public API, ~30 calls/min): a pool's latest 300 trades with the wallet that sent each one, and
OHLCV candles. DexScreener (300 calls/min): pool age, liquidity and tokens, 30 pools per call.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import aiohttp

from .pumpswap import WSOL

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
DS = "https://api.dexscreener.com/latest/dex/pairs/solana/"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class RateLimiter:
    """Spaces calls evenly at `per_min`; after a 429 everything waits `cooldown_s`."""

    def __init__(self, per_min: float, cooldown_s: float = 60.0):
        self.gap = 60.0 / per_min
        self.cooldown_s = cooldown_s
        self.next_at = 0.0
        self.calls: list[float] = []
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self.next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            self.next_at = now + self.gap
            self.calls.append(time.time())
            self.calls = self.calls[-2000:]

    def backoff(self) -> None:
        self.next_at = time.monotonic() + self.cooldown_s

    def per_hour(self) -> int:
        cut = time.time() - 3600
        return sum(1 for t in self.calls if t >= cut)


class SourceError(Exception):
    pass


async def _get_json(s: aiohttp.ClientSession, url: str, limiter: RateLimiter | None = None, params=None):
    if limiter:
        await limiter.wait()
    async with s.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"}) as r:
        if r.status == 429:
            if limiter:
                limiter.backoff()
            raise SourceError(f"429 rate limited: {url.split('?')[0][-60:]}")
        if r.status == 404:
            return None
        if r.status != 200:
            raise SourceError(f"HTTP {r.status}: {url.split('?')[0][-60:]}")
        return await r.json(content_type=None)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def gt_trade_row(pool: str, d: dict) -> dict | None:
    """GeckoTerminal trade -> {id, t, pool, mint, wallet, side, sol, tokens, px (SOL/token), usd}.
    None for pools not quoted in SOL."""
    a = d.get("attributes") or {}
    fr, to = a.get("from_token_address"), a.get("to_token_address")
    side = a.get("kind")
    if side == "buy" and fr == WSOL:
        mint, sol, tokens, px = to, a.get("from_token_amount"), a.get("to_token_amount"), a.get("price_to_in_currency_token")
    elif side == "sell" and to == WSOL:
        mint, sol, tokens, px = fr, a.get("to_token_amount"), a.get("from_token_amount"), a.get("price_from_in_currency_token")
    else:
        return None
    try:
        return {"id": d["id"], "t": _epoch(a["block_timestamp"]), "pool": pool, "mint": mint,
                "wallet": a["tx_from_address"], "side": side, "sol": float(sol), "tokens": float(tokens),
                "px": float(px), "usd": float(a.get("volume_in_usd") or 0), "sig": a.get("tx_hash")}
    except (KeyError, TypeError, ValueError):
        return None


async def gt_trades(s: aiohttp.ClientSession, limiter: RateLimiter, pool: str, min_usd: float = 0) -> list[dict]:
    """The pool's latest trades (at most 300, last 24 h), oldest first. min_usd reaches further back."""
    params = {"trade_volume_in_usd_greater_than": min_usd} if min_usd else None
    js = await _get_json(s, f"{GT}/pools/{pool}/trades", limiter, params)
    rows = [gt_trade_row(pool, d) for d in ((js or {}).get("data") or [])]
    return sorted((r for r in rows if r), key=lambda r: r["t"])


async def gt_ohlcv(s: aiohttp.ClientSession, limiter: RateLimiter, pool: str, timeframe: str = "day",
                   aggregate: int = 1, limit: int = 1000, before: float | None = None) -> list[list[float]]:
    """[[ts, open, high, low, close, volume_usd], ...] oldest first, prices in SOL per token."""
    params = {"aggregate": aggregate, "limit": limit, "currency": "token", "token": "base"}
    if before:
        params["before_timestamp"] = int(before)
    js = await _get_json(s, f"{GT}/pools/{pool}/ohlcv/{timeframe}", limiter, params)
    rows = (((js or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted(([float(x) for x in r] for r in rows), key=lambda r: r[0])


def _sol_usd(p: dict) -> float | None:
    """SOL's dollar price implied by a SOL-quoted pair (priceUsd / priceNative)."""
    try:
        v = float(p["priceUsd"]) / float(p["priceNative"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return v if 10 < v < 10_000 else None


async def ds_pairs(s: aiohttp.ClientSession, pools: list[str]) -> dict[str, dict]:
    """pool -> {dex, mint, symbol, quote, created, liq_usd, fdv}. Pools DexScreener doesn't know are absent."""
    out: dict[str, dict] = {}
    for i in range(0, len(pools), 30):
        js = await _get_json(s, DS + ",".join(pools[i:i + 30]))
        for p in (js or {}).get("pairs") or []:
            out[p["pairAddress"]] = {
                "dex": p.get("dexId"), "mint": (p.get("baseToken") or {}).get("address"),
                "symbol": (p.get("baseToken") or {}).get("symbol"), "quote": (p.get("quoteToken") or {}).get("address"),
                "created": (p.get("pairCreatedAt") or 0) / 1000 or None,
                "liq_usd": (p.get("liquidity") or {}).get("usd"), "fdv": p.get("fdv"),
                "sol_usd": _sol_usd(p)}
        await asyncio.sleep(0.25)
    return out
