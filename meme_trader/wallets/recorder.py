"""Records wallet-level trades on graduated pump.fun coins (PumpSwap pools) for the wallet study.

Constraints measured 2026-10-04: the whole PumpSwap log stream is ~0.9 MB/s (~80 GB/day); PublicNode drops most
per-pool subscriptions (and throttled this IP after a few dozen, which also moved the bot's feed); GeckoTerminal's
free API sustains ~5 calls/min. So, in the default `mode: poll`:
  1. discovery: every `discover_every_s`, sample the whole stream for `discover_s` and note which pools traded
     (one PublicNode connection; never the bot's endpoint, whose per-IP data cap the bot needs);
  2. metadata: DexScreener gives each new pool's age, liquidity and coin (30 pools per call);
  3. trades: pump.fun coins' pools at least `min_pool_age_days` old with `min_liquidity_usd` are polled on
     GeckoTerminal (latest 300 trades, with the sending wallet), each as often as its trade rate needs and the
     budget allows. A call that overflows is a gap; status.json estimates the coverage.
`mode: stream` instead keeps the whole stream open and records every swap on those pools: complete, at ~80 GB/day.
Coins younger than `min_pool_age_days` carry ~90% of PumpSwap trades and are left out on purpose; the study
only trades coins 14+ days old.

  4. candles: daily price history for pools nearing the study's 14-day age (the "survived its first dump" gate).
Output: data/wallets/trades-YYYY-MM-DD.jsonl (one row per trade, compressed after the day), candles/<pool>.json,
pools.json, status.json.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ..config import ROOT
from ..sniper.feeds import compress_file
from .pumpswap import AMM_PROGRAM, WSOL, parse_logs
from .sources import RateLimiter, SourceError, ds_pairs, gt_ohlcv, gt_trades

log = logging.getLogger("wallets")
DATA = ROOT / "data" / "wallets"
WS_URLS = ["wss://solana-rpc.publicnode.com"]     # not the bot's api.mainnet-beta: its per-IP data cap is the bot's
GT_MAX_ROWS = 300


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def read_control(data_dir: Path = DATA) -> dict:
    """data/wallets/control.json, written by the dashboard: {paused, pause_until, quiet: {start, end} | None}.
    Quiet hours are local time; a window like 23:00-06:00 wraps midnight."""
    p = Path(data_dir) / "control.json"
    try:
        c = json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        c = {}
    return {"paused": bool(c.get("paused")), "pause_until": c.get("pause_until"), "quiet": c.get("quiet")}


def control_state(c: dict, now: float | None = None) -> tuple[bool, str]:
    """(recording?, why not)."""
    now = now or time.time()
    if c.get("paused"):
        return False, "paused"
    if c.get("pause_until") and now < c["pause_until"]:
        return False, f"paused until {datetime.fromtimestamp(c['pause_until']):%H:%M}"
    q = c.get("quiet")
    if q and q.get("start") and q.get("end"):
        t = datetime.fromtimestamp(now).strftime("%H:%M")
        a, b = q["start"], q["end"]
        inside = (a <= t < b) if a <= b else (t >= a or t < b)
        if inside:
            return False, f"quiet hours {a}-{b}"
    return True, ""


def latest_liq(rec: dict) -> float:
    return (rec.get("liq") or [[0, 0]])[-1][1] or 0.0


class Recorder:
    def __init__(self, cfg, data_dir: Path = DATA, ws_urls: list[str] | None = None):
        self.cfg = cfg
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ws_urls = ws_urls or WS_URLS
        path = self.dir / "pools.json"
        self.pools: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}
        self.gt = RateLimiter(cfg.gt_calls_per_min)
        self.buf: list[dict] = []
        self.errors: collections.deque = collections.deque(maxlen=20)
        self.st = {"started": time.time(), "rows": 0, "gaps": 0, "polls": 0, "discovery": {}, "missed_est": 0.0,
                   "captured": 0}
        self.day_rows = collections.Counter()
        self.sol_usd: float | None = None                # from DexScreener pairs; prices stream rows in USD
        self.stream_bytes = collections.Counter()        # per UTC day: what the full stream downloads
        self.control = read_control(self.dir)
        self.active, self.pause_reason = control_state(self.control)
        self._pause_started = None if self.active else time.time()

    # ------------------------------------------------------------------ pool bookkeeping
    def _err(self, where: str, e) -> None:
        from ..redact import describe
        msg = f"{datetime.now(timezone.utc):%H:%M:%S} {where}: {describe(e)}"     # (never an HTTP error's URL)
        self.errors.append(msg)
        log.warning(msg)

    def last_active(self, rec: dict) -> float:
        return max(rec.get("last_seen") or 0, rec.get("last_trade") or 0)

    def eligible(self, rec: dict, now: float) -> bool:
        m = rec.get("meta")
        return bool(m and m.get("dex") == "pumpswap" and m.get("quote") == WSOL and m.get("created")
                    and (m.get("mint") or "").endswith("pump")                  # pump.fun coins (6 decimals)
                    and now - m["created"] >= self.cfg.min_pool_age_days * 86400
                    and latest_liq(rec) >= self.cfg.min_liquidity_usd
                    and now - self.last_active(rec) <= self.cfg.keep_days * 86400)

    def _prune(self, now: float) -> None:
        """Forget pools that never qualified and went quiet, and anything silent for a month."""
        drop = [p for p, r in self.pools.items()
                if (now - self.last_active(r) > 30 * 86400)
                or (not r.get("polls") and now - self.last_active(r) > 3 * 86400)]
        for p in drop:
            del self.pools[p]
        if len(self.pools) > self.cfg.max_pools * 4:
            keep = sorted(self.pools, key=lambda p: self.last_active(self.pools[p]), reverse=True)[: self.cfg.max_pools * 4]
            self.pools = {p: self.pools[p] for p in keep}

    # ------------------------------------------------------------------ 1. discovery
    async def discover(self, s: aiohttp.ClientSession) -> dict:
        counts: collections.Counter = collections.Counter()
        stats = {"at": time.time(), "tx": 0, "swaps": 0, "bytes": 0, "url": None}

        async def sample(ws):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    return
                stats["bytes"] += len(msg.data)
                v = (json.loads(msg.data).get("params") or {}).get("result", {}).get("value")
                if not v or v.get("err"):
                    continue
                stats["tx"] += 1
                for sw in parse_logs(v.get("logs") or []):
                    counts[sw.pool] += 1
                    stats["swaps"] += 1

        for url in self.ws_urls:
            try:
                async with s.ws_connect(url, max_msg_size=0, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                        "params": [{"mentions": [AMM_PROGRAM]}, {"commitment": "confirmed"}]})
                    try:
                        await asyncio.wait_for(sample(ws), timeout=self.cfg.discover_s)
                    except asyncio.TimeoutError:
                        pass
                stats["url"] = url
                break
            except (aiohttp.ClientError, OSError, ValueError) as e:
                self._err(f"discovery {url.split('//')[-1]}", e)
        now = time.time()
        new = 0
        for pool, n in counts.items():
            rec = self.pools.get(pool)
            if rec is None:
                rec = self.pools[pool] = {"first_seen": now, "meta": None, "meta_tries": 0, "meta_at": 0}
                new += 1
            rec["last_seen"] = now
            rec["seen_swaps"] = rec.get("seen_swaps", 0) + n
        stats.update(pools=len(counts), new_pools=new, secs=self.cfg.discover_s)
        self.st["discovery"] = stats
        return stats

    async def discover_loop(self, s: aiohttp.ClientSession) -> None:
        while True:
            t0 = time.time()
            if not self.active:
                await asyncio.sleep(10)
                continue
            try:
                await self.discover(s)
            except Exception as e:                       # noqa: BLE001 - keep recording through anything
                self._err("discovery", e)
            await asyncio.sleep(max(5.0, self.cfg.discover_every_s - (time.time() - t0)))

    # ------------------------------------------------------------------ 2. metadata
    async def refresh_meta(self, s: aiohttp.ClientSession, now: float | None = None) -> int:
        now = now or time.time()
        need = [p for p, r in self.pools.items() if r.get("meta") is None and r.get("meta_tries", 0) < 3
                and now - r.get("meta_at", 0) > 300]
        need.sort(key=lambda p: self.pools[p].get("last_seen") or 0, reverse=True)
        # liquidity snapshots: polled pools, and recently active ones that may grow into the universe
        stale = [p for p, r in self.pools.items() if r.get("meta") and now - r.get("meta_at", 0) >= self.cfg.meta_refresh_s
                 and (r.get("polls") or now - self.last_active(r) < 86400)]
        batch = (need[:240] + stale[:60]) if need else stale[:300]
        if not batch:
            return 0
        try:
            info = await ds_pairs(s, batch)
        except (SourceError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._err("dexscreener", e)
            return 0
        for p in batch:
            rec = self.pools.get(p)
            if rec is None:
                continue
            rec["meta_at"] = now
            m = info.get(p)
            if not m:
                rec["meta_tries"] = rec.get("meta_tries", 0) + 1
                continue
            liq = m.pop("liq_usd", None)
            su = m.pop("sol_usd", None)
            if su and m.get("quote") == WSOL:
                self.sol_usd = su
            rec["meta"] = m
            rec["liq"] = ((rec.get("liq") or []) + [[now, liq or 0]])[-60:]
        return len(batch)

    async def meta_loop(self, s: aiohttp.ClientSession) -> None:
        while True:
            try:
                if self.active:
                    await self.refresh_meta(s)
            except Exception as e:                       # noqa: BLE001
                self._err("meta", e)
            await asyncio.sleep(20)

    # ------------------------------------------------------------------ 3. trades
    def next_due(self, now: float) -> tuple[str | None, float]:
        best, due = None, float("inf")
        for p, r in self.pools.items():
            if not self.eligible(r, now):
                continue
            t = r.get("next_poll") or 0
            if t < due:
                best, due = p, t
        return best, due

    def overdue(self, now: float) -> int:
        return sum(1 for r in self.pools.values() if self.eligible(r, now) and (r.get("next_poll") or 0) < now - 60)

    def absorb(self, pool: str, rows: list[dict], now: float, raw_count: int) -> list[dict]:
        """New rows since the pool's watermark; schedules its next poll from the observed trade rate."""
        rec = self.pools[pool]
        wm, wm_ids = rec.get("wm", 0.0), set(rec.get("wm_ids") or ())
        new = [r for r in rows if r["t"] > wm or (r["t"] == wm and r["id"] not in wm_ids)]
        if rec.get("polls") and raw_count >= GT_MAX_ROWS and rows and rows[0]["t"] > wm:
            rec["gaps"] = rec.get("gaps", 0) + 1                      # the call overflowed: trades in between missed
            self.st["gaps"] += 1
            self.st["missed_est"] += (rec.get("rate") or 0.0) * (rows[0]["t"] - wm)
        self.st["captured"] += len(new)
        if rows:
            top = max(r["t"] for r in rows)
            if top >= wm:
                rec["wm_ids"] = sorted({r["id"] for r in rows if r["t"] == top} | (wm_ids if top == wm else set()))
                rec["wm"] = top
            rec["last_trade"] = max(rec.get("last_trade") or 0, top)
        dt = now - rec["last_poll"] if rec.get("last_poll") else None
        if dt:
            rate = len(new) / max(dt, 1.0)
            rec["rate"] = 0.5 * rec.get("rate", rate) + 0.5 * rate
        elif new:
            span = max(new[-1]["t"] - new[0]["t"], 60.0)
            rec["rate"] = len(new) / span
        interval = self.cfg.target_trades_per_poll / max(rec.get("rate") or 0.0, 1e-9)
        if rec.get("gaps") and raw_count >= GT_MAX_ROWS:
            interval /= 2
        rec["next_poll"] = now + min(max(interval, self.cfg.min_poll_s), self.cfg.max_poll_s)
        rec["last_poll"] = now
        rec["polls"] = rec.get("polls", 0) + 1
        return new

    async def poll_one(self, s: aiohttp.ClientSession, pool: str) -> int:
        rec = self.pools[pool]
        rows = await gt_trades(s, self.gt, pool)
        raw = len(rows)
        if not rec.get("polls") and self.cfg.deep_min_usd and raw >= GT_MAX_ROWS:
            deep = await gt_trades(s, self.gt, pool, min_usd=self.cfg.deep_min_usd)
            seen = {r["id"] for r in rows}
            rows = sorted(rows + [r for r in deep if r["id"] not in seen], key=lambda r: r["t"])
        now = time.time()
        new = self.absorb(pool, rows, now, raw)
        sym = (rec.get("meta") or {}).get("symbol")
        for r in new:
            r["sym"] = sym
        self.buf.extend(new)
        self.st["polls"] += 1
        return len(new)

    async def poll_loop(self, s: aiohttp.ClientSession) -> None:
        while True:
            now = time.time()
            if not self.active:
                await asyncio.sleep(5)
                continue
            pool, due = self.next_due(now)
            if pool is None or due > now:
                await asyncio.sleep(min(5.0, max(0.5, due - now)) if pool else 5.0)
                continue
            try:
                await self.poll_one(s, pool)
            except (SourceError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                self._err(f"trades {pool[:6]}", e)
                self.pools[pool]["next_poll"] = time.time() + 300

    # ------------------------------------------------------------------ 4. daily candles
    def candle_due(self, now: float) -> str | None:
        cdir = self.dir / "candles"
        best, oldest = None, now - self.cfg.candles_refresh_days * 86400
        for p, r in self.pools.items():
            c = (r.get("meta") or {}).get("created")
            if not (self.eligible(r, now) and r.get("polls") and c and now - c >= self.cfg.candles_min_age_days * 86400):
                continue
            f = cdir / f"{p}.json"
            at = r.get("candles_at") or (f.stat().st_mtime if f.exists() else 0)
            if at < oldest:
                best, oldest = p, at
        return best

    async def candle_loop(self, s: aiohttp.ClientSession) -> None:
        cdir = self.dir / "candles"
        cdir.mkdir(exist_ok=True)
        while True:
            await asyncio.sleep(self.cfg.candles_every_s)
            pool = self.candle_due(time.time()) if self.active else None
            if not pool:
                continue
            try:
                daily = await gt_ohlcv(s, self.gt, pool, "day", 1, 1000)
                _write_json(cdir / f"{pool}.json", {"fetched": time.time(), "daily": daily})
            except (SourceError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                self._err(f"candles {pool[:6]}", e)
            self.pools[pool]["candles_at"] = time.time()

    # ------------------------------------------------------------------ output
    def flush(self) -> int:
        if not self.buf:
            return 0
        rows, self.buf = self.buf, []
        day = _day(time.time())
        with (self.dir / f"trades-{day}.jsonl").open("a") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        self.st["rows"] += len(rows)
        self.day_rows[day] += len(rows)
        return len(rows)

    def compress_old(self) -> None:
        today = _day(time.time())
        for p in self.dir.glob("trades-*.jsonl"):
            if p.stem.split("-", 1)[1] < today:
                compress_file(p)

    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        elig = [r for r in self.pools.values() if self.eligible(r, now)]
        cap, miss = self.st["captured"], self.st["missed_est"]
        return {"updated": now, "started": self.st["started"], "mode": self.cfg.mode, "pools_known": len(self.pools),
                "coverage_est": round(cap / (cap + miss), 3) if cap + miss else None,
                "pools_eligible": len(elig), "pools_polled": sum(1 for r in elig if r.get("polls")),
                "overdue": self.overdue(now), "gt_calls_last_hour": self.gt.per_hour(), "rows_this_run": self.st["rows"],
                "rows_by_day": dict(self.day_rows), "gaps": self.st["gaps"], "polls": self.st["polls"],
                "discovery": self.st["discovery"], "sol_usd": self.sol_usd, "active": self.active,
                "pause_reason": self.pause_reason, "paused_since": self._pause_started, "control": self.control,
                "stream_gb_by_day": {d: round(b / 1e9, 2) for d, b in self.stream_bytes.items()},
                "errors": list(self.errors)[-10:]}

    def apply_control(self, now: float | None = None) -> None:
        """Re-read control.json; log each pause (start, end, why) to pauses.jsonl so the study knows the gaps."""
        now = now or time.time()
        self.control = read_control(self.dir)
        active, why = control_state(self.control, now)
        if active != self.active:
            if not active:
                self._pause_started = now
                log.info("recording paused: %s", why)
            else:
                with (self.dir / "pauses.jsonl").open("a") as f:
                    f.write(json.dumps({"start": self._pause_started, "end": now, "why": self.pause_reason}) + "\n")
                log.info("recording resumed after %.0f min", (now - (self._pause_started or now)) / 60)
                self._pause_started = None
        self.active, self.pause_reason = active, why

    async def housekeeping(self) -> None:
        last_save = last_status = 0.0
        while True:
            await asyncio.sleep(5)
            try:
                was = self.active
                self.apply_control()
                self.flush()
                now = time.time()
                if now - last_save >= 120:
                    self._prune(now)
                    self.compress_old()
                    _write_json(self.dir / "pools.json", self.pools)
                    last_save = now
                if now - last_status >= 15 or was != self.active:     # the dashboard reads this
                    _write_json(self.dir / "status.json", self.status(now))
                    last_status = now
            except Exception as e:                       # noqa: BLE001
                self._err("housekeeping", e)

    # ------------------------------------------------------------------ mode: stream
    def swap_row(self, sw, sig: str, k: int, rx: float | None = None) -> dict | None:
        """One recorded swap. `t` is its chain time; `rx` when this recorder RECEIVED it (wall clock), so a reader's
        lag splits into the chain-to-receipt and receipt-to-read parts (an eleventh review)."""
        rec = self.pools.get(sw.pool)
        if not rec or not self.eligible(rec, time.time()) or sw.base <= 0 or sw.pool_base <= sw.pool_quote:
            return None                                  # not in the universe, or a pool with SOL as its base
        sol, tokens = sw.quote / 1e9, sw.base / 1e6
        m = rec["meta"]
        return {"id": f"ws_{sig}_{k}", "t": float(sw.ts), "pool": sw.pool, "mint": m.get("mint"), "wallet": sw.user,
                "side": sw.side, "sol": sol, "tokens": tokens, "px": sol / tokens,
                "usd": round(sol * self.sol_usd, 2) if self.sol_usd else 0.0, "sig": sig,
                "sym": m.get("symbol"), **({"rx": round(rx, 3)} if rx is not None else {})}

    async def stream_loop(self, s: aiohttp.ClientSession) -> None:
        """Every swap, all the time (~0.9 MB/s). Discovery is the same stream."""
        while True:
            if not self.active:
                await asyncio.sleep(5)
                continue
            try:
                async with s.ws_connect(self.ws_urls[0], max_msg_size=0, heartbeat=20) as ws:
                    await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                        "params": [{"mentions": [AMM_PROGRAM]}, {"commitment": "confirmed"}]})
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        if not self.active:
                            break                               # a pause started: close the stream
                        self.stream_bytes[_day(time.time())] += len(msg.data)
                        v = (json.loads(msg.data).get("params") or {}).get("result", {}).get("value")
                        if not v or v.get("err"):
                            continue
                        now = time.time()
                        for k, sw in enumerate(parse_logs(v.get("logs") or [])):
                            rec = self.pools.get(sw.pool)
                            if rec is None:
                                rec = self.pools[sw.pool] = {"first_seen": now, "meta": None, "meta_tries": 0, "meta_at": 0}
                            rec["last_seen"] = now
                            row = self.swap_row(sw, v.get("signature", ""), k, rx=now)
                            if row:
                                rec["last_trade"] = row["t"]
                                rec["polls"] = rec.get("polls") or 1          # counts as covered for candles/meta
                                self.buf.append(row)
                                self.st["captured"] += 1
            except (aiohttp.ClientError, OSError, ValueError) as e:
                self._err("stream", e)
            await asyncio.sleep(1 if not self.active else 5)

    async def run(self) -> None:
        log.info("wallet recorder (%s mode): %d pools known, data in %s", self.cfg.mode, len(self.pools), self.dir)
        async with aiohttp.ClientSession() as s:
            loops = ([self.stream_loop(s)] if self.cfg.mode == "stream" else [self.discover_loop(s), self.poll_loop(s)])
            tasks = [asyncio.create_task(c) for c in loops + [self.meta_loop(s), self.candle_loop(s), self.housekeeping()]]
            try:
                await asyncio.gather(*tasks)
            finally:
                for t in tasks:
                    t.cancel()
                self.flush()
                _write_json(self.dir / "pools.json", self.pools)
                _write_json(self.dir / "status.json", self.status())
