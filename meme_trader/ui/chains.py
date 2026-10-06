"""Other chains: what's trending and what's new on Solana, BNB Chain, Base and Ethereum, from GeckoTerminal's
free API. Shared by the Charts tab's "Other chains" view and the other-chain paper trader (sniper/xchain.py).

GeckoTerminal's free allowance runs out fast in bursts (2026-10-05: 429s after 6 calls 6 s apart, following a
burst), so calls go one at a time, GAP_S apart; a 429 keeps the last lists and waits BACKOFF_S. Each list is
fetched only while it's used: the view's lists for VIEW_S after someone looks, the trader's for its chains."""
from __future__ import annotations

import asyncio
import calendar
import re
import time

GT = "https://api.geckoterminal.com/api/v2/networks"
NETWORKS = {"solana": ("Solana", "solana"), "bsc": ("BNB Chain", "bsc"), "base": ("Base", "base"), "eth": ("Ethereum", "ethereum")}
KINDS = {"trending": "trending_pools?page=1", "new": "new_pools?page=1", "hot": "trending_pools?duration=1h&page=1"}
REFRESH_S = 120         # each list at most this often
GAP_S = 2.0             # between calls
BACKOFF_S = 120         # after a 429
# a chart's timeframes (the coin card's buttons): GeckoTerminal's unit, aggregate, and how many candles
OHLCV = {"1m": ("minute", 1, 180), "5m": ("minute", 5, 144), "15m": ("minute", 15, 96), "1h": ("hour", 1, 168),
         "4h": ("hour", 4, 180), "1d": ("day", 1, 180)}
POOL_RE = re.compile(r"^(?:[1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40,64})$")
CANDLE_S = 30           # a pool's candles at one timeframe are reused this long
VIEW_S = 600            # the view's lists keep coming this long after someone looked


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
    rel = d.get("relationships") or {}
    base = (rel.get("base_token") or {}).get("data") or {}
    label, ds = NETWORKS[net]
    return {"chain": net, "chain_name": label, "name": name[:60], "symbol": name.split(" / ")[0][:20], "pool": addr,
            "token": str(base.get("id", "")).split("_", 1)[-1], "mcap_usd": _f(a.get("market_cap_usd")), "fdv_usd": _f(a.get("fdv_usd")),
            "liq_usd": _f(a.get("reserve_in_usd")), "vol_h1": _f(vol.get("h1")), "vol_h24": _f(vol.get("h24")),
            "chg_m5": _f(chg.get("m5")), "chg_h1": _f(chg.get("h1")), "chg_h24": _f(chg.get("h24")),
            "buys_h1": tx.get("buys"), "sells_h1": tx.get("sells"), "buyers_h1": tx.get("buyers"), "age_s": round(age_s) if age_s else None,
            "dex_id": ((rel.get("dex") or {}).get("data") or {}).get("id"), "price_usd": _f(a.get("base_token_price_usd")),
            "gt": f"https://www.geckoterminal.com/{net}/pools/{addr}", "dex": f"https://dexscreener.com/{ds}/{addr}"}


class Chains:
    def __init__(self):
        self.data: dict = {"trending": {}, "new": {}, "hot": {}, "fetched": 0.0, "error": "",
                           "chains": {k: v[0] for k, v in NETWORKS.items()}}
        self._lock = asyncio.Lock()
        self.viewed = 0.0                    # when someone last looked at the view
        self.trade_nets: set[str] = set()    # chains the paper trader scans (it sets this)
        self.fetched_at: dict[tuple[str, str], float] = {}
        self.backoff_until = 0.0
        self._last_call = 0.0
        self._task: asyncio.Task | None = None
        self._candles: dict[tuple, tuple[float, list]] = {}
        self._pace = asyncio.Lock()

    def wanted(self, now: float) -> list[tuple[str, str]]:
        out = [(n, k) for k in ("trending", "new") for n in NETWORKS] if now - self.viewed < VIEW_S else []
        for n in NETWORKS:
            if n in self.trade_nets:
                out += [(n, k) for k in ("trending", "hot") if (n, k) not in out]
        return out

    def _stale(self, now: float) -> list[tuple[str, str]]:
        return [x for x in self.wanted(now) if now - self.fetched_at.get(x, 0.0) >= REFRESH_S]

    async def get(self, viewer: bool = False, wait: bool = True) -> dict:
        """The lists. wait=False (the dashboard): answer at once with what's here, refresh in the background."""
        now = time.time()
        if viewer:
            self.viewed = now
        if self._stale(now) and now >= self.backoff_until:
            if wait:
                await self._locked_refresh()
            elif self._task is None or self._task.done():
                self._task = asyncio.create_task(self._locked_refresh())
        return {**self.data, "loading": self._task is not None and not self._task.done()}

    async def _locked_refresh(self) -> None:
        async with self._lock:
            if self._stale(time.time()) and time.time() >= self.backoff_until:
                await self._refresh()

    async def _fetch(self, s, net: str, kind: str) -> tuple[int, dict]:
        async with s.get(f"{GT}/{net}/{KINDS[kind]}") as r:
            return r.status, (await r.json() if r.status == 200 else {})

    async def _refresh(self) -> None:
        import aiohttp

        errors = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={"Accept": "application/json"}) as s:
            for net, kind in self._stale(time.time()):
                wait = self._last_call + GAP_S - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.time()
                try:
                    status, d = await self._fetch(s, net, kind)
                except Exception as e:                     # one list failing never blanks the others
                    errors.append(f"{net} {kind}: {type(e).__name__}")
                    continue
                if status == 429:                          # out of allowance: keep what we have, wait
                    self.backoff_until = time.time() + BACKOFF_S
                    errors.append(f"GeckoTerminal's free limit: next try in {BACKOFF_S // 60} min")
                    break
                if status != 200:
                    errors.append(f"{net} {kind}: HTTP {status}")
                    continue
                self.data.setdefault(kind, {})[net] = [x for x in (pool_row(net, p) for p in d.get("data") or []) if x][:20]
                self.fetched_at[(net, kind)] = time.time()
                self.data["fetched"] = time.time()
        self.data["error"] = "; ".join(errors)[:300]

    async def _ohlcv(self, net: str, pool: str, tf: str) -> tuple[int, dict]:
        import aiohttp

        unit, agg, n = OHLCV[tf]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), headers={"Accept": "application/json"}) as s:
            async with s.get(f"{GT}/{net}/pools/{pool}/ohlcv/{unit}", params={"aggregate": agg, "limit": n, "currency": "usd"}) as r:
                return r.status, (await r.json() if r.status == 200 else {})

    async def candles(self, net: str, pool: str, tf: str) -> dict:
        """One pool's candles at a timeframe (the coin card's 1m..1D buttons). Paced with the lists, so a few clicks
        can't spend the free allowance the other-chain trader needs, and cached for CANDLE_S."""
        if net not in NETWORKS or tf not in OHLCV or not POOL_RE.match(pool or ""):
            return {"candles": [], "tf": tf, "error": "unknown chain, pool or timeframe"}
        key = (net, pool, tf)
        hit = self._candles.get(key)
        if hit and time.time() - hit[0] < CANDLE_S:
            return {"candles": hit[1], "tf": tf, "error": ""}
        async with self._pace:
            if time.time() < self.backoff_until:
                return {"candles": hit[1] if hit else [], "tf": tf,
                        "error": f"GeckoTerminal's free limit: try again in {int(self.backoff_until - time.time()) + 1} s"}
            wait = self._last_call + GAP_S - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()
            try:
                status, d = await self._ohlcv(net, pool, tf)
            except Exception as e:
                return {"candles": [], "tf": tf, "error": f"couldn't reach GeckoTerminal ({type(e).__name__})"}
        if status == 429:
            self.backoff_until = time.time() + BACKOFF_S
            return {"candles": [], "tf": tf, "error": f"GeckoTerminal's free limit: try again in {BACKOFF_S // 60} min"}
        if status != 200:
            return {"candles": [], "tf": tf, "error": f"GeckoTerminal answered HTTP {status}"}
        rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out = [[int(t), o, h, lo, cl, v] for t, o, h, lo, cl, v in sorted(rows)]
        if len(self._candles) > 200:
            self._candles.clear()
        self._candles[key] = (time.time(), out)
        return {"candles": out, "tf": tf, "error": ""}

    def names(self, limit: int = 5) -> dict:
        """What's trending per chain, for the desk's narrative reads (no fetch: whatever was last seen)."""
        return {NETWORKS[n][0]: [r["symbol"] for r in rows[:limit]] for n, rows in (self.data.get("trending") or {}).items() if rows}
