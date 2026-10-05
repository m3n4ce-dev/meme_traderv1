"""Other chains, read-only: what's trending and what's new on Solana, BNB Chain, Base and Ethereum, from
GeckoTerminal's free API (30 calls a minute; this uses 8 per refresh, at most every 90 s, only while someone looks).

The bot trades pump.fun only. Other chains are watched here, not traded: docs/MULTICHAIN.md says to add a chain
only after the Solana bot proves an edge, because more chains multiply losses as much as wins."""
from __future__ import annotations

import asyncio
import calendar
import time

GT = "https://api.geckoterminal.com/api/v2/networks"
NETWORKS = {"solana": ("Solana", "solana"), "bsc": ("BNB Chain", "bsc"), "base": ("Base", "base"), "eth": ("Ethereum", "ethereum")}
REFRESH_S = 90


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pool_row(net: str, d: dict) -> dict | None:
    a = d.get("attributes") or {}
    addr = a.get("address")
    if not addr:
        return None
    name = str(a.get("name") or "")
    tx = (a.get("transactions") or {}).get("h1") or {}
    vol, chg = a.get("volume_usd") or {}, a.get("price_change_percentage") or {}
    created = a.get("pool_created_at")
    try:
        age_s = time.time() - calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) if created else None
    except (ValueError, TypeError):
        age_s = None
    base = ((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    label, ds = NETWORKS[net]
    return {"chain": net, "chain_name": label, "name": name[:60], "symbol": name.split(" / ")[0][:20], "pool": addr,
            "token": str(base.get("id", "")).split("_", 1)[-1], "mcap_usd": _f(a.get("market_cap_usd")), "fdv_usd": _f(a.get("fdv_usd")),
            "liq_usd": _f(a.get("reserve_in_usd")), "vol_h1": _f(vol.get("h1")), "vol_h24": _f(vol.get("h24")),
            "chg_m5": _f(chg.get("m5")), "chg_h1": _f(chg.get("h1")), "chg_h24": _f(chg.get("h24")),
            "buys_h1": tx.get("buys"), "sells_h1": tx.get("sells"), "buyers_h1": tx.get("buyers"), "age_s": round(age_s) if age_s else None,
            "gt": f"https://www.geckoterminal.com/{net}/pools/{addr}", "dex": f"https://dexscreener.com/{ds}/{addr}"}


class Chains:
    def __init__(self):
        self.data: dict = {"trending": {}, "new": {}, "fetched": 0.0, "error": ""}
        self._lock = asyncio.Lock()

    async def get(self) -> dict:
        async with self._lock:
            if time.time() - self.data["fetched"] >= REFRESH_S:
                await self._refresh()
        return self.data

    async def _refresh(self) -> None:
        import aiohttp

        out = {"trending": {}, "new": {}}
        errors = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={"Accept": "application/json"}) as s:
            async def one(net: str, kind: str) -> None:
                path = "trending_pools" if kind == "trending" else "new_pools"
                try:
                    async with s.get(f"{GT}/{net}/{path}?page=1") as r:
                        if r.status != 200:
                            errors.append(f"{net} {kind}: HTTP {r.status}")
                            return
                        d = await r.json()
                    out[kind][net] = [x for x in (pool_row(net, p) for p in d.get("data") or []) if x][:20]
                except Exception as e:                     # one chain failing never blanks the others
                    errors.append(f"{net} {kind}: {type(e).__name__}")
            await asyncio.gather(*(one(n, k) for n in NETWORKS for k in ("trending", "new")))
        for kind in ("trending", "new"):                   # keep the last good list for a chain that failed
            for net in NETWORKS:
                out[kind].setdefault(net, self.data[kind].get(net, []))
        self.data = {**out, "fetched": time.time(), "error": "; ".join(errors)[:300],
                     "chains": {k: v[0] for k, v in NETWORKS.items()}}

    def names(self, limit: int = 5) -> dict:
        """What's trending per chain, for the desk's narrative reads (no fetch: whatever was last seen)."""
        return {NETWORKS[n][0]: [r["symbol"] for r in rows[:limit]] for n, rows in (self.data.get("trending") or {}).items() if rows}
