"""Event sources for the sniper engine.

PumpPortalFeed - live pump.fun launches/trades/migrations over wss://pumpportal.fun/api/data
                 (new-token + migration streams are free; token-trade data is metered at
                 0.01 SOL per 10k trades and needs an API key). ~100s of ms behind chain;
                 a Yellowstone/LaserStream gRPC feed is the upgrade path for real speed.
FileFeed       - replays a recorded JSONL file (for backtests).
SyntheticFeed  - a fake market with realistic launch archetypes (rugs, bundles, dev dumps,
                 duds, fake runners, real runners). For demos, UI work and testing strategy
                 *logic* only - its P&L says nothing about real profitability.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import os
import random
import string
import time
from pathlib import Path
from typing import AsyncIterator

from .curve import Curve
from .events import Event, Launch, Migration, Trade, loads


class Feed:
    realtime = True

    def now(self) -> float:
        """The engine's clock: wall time for live feeds, event time for replays."""
        return time.time() if self.realtime else getattr(self, "_last", 0.0)

    async def events(self) -> AsyncIterator[Event]:  # pragma: no cover - interface
        raise NotImplementedError
        yield

    async def watch(self, mints: list[str]) -> None:
        pass

    async def unwatch(self, mints: list[str]) -> None:
        pass

    async def watch_accounts(self, wallets: list[str]) -> None:
        """Stream every trade these wallets make (copy trading)."""


# --------------------------------------------------------------------------- live
class PumpPortalFeed(Feed):
    URL = "wss://pumpportal.fun/api/data"

    def __init__(self, fallback_urls: list[str] | None = None):
        key = os.environ.get("PUMPPORTAL_API_KEY", "")
        # primary first; on disconnect rotate to backups (e.g. pumpdev.io), then back to primary
        self.urls = [f"{self.URL}?api-key={key}" if key else self.URL] + list(fallback_urls or [])
        self.url_idx = 0
        self.ws = None
        self.watched: set[str] = set()
        self.accounts: set[str] = set()

    async def _send(self, payload: dict) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_json(payload)

    async def watch(self, mints: list[str]) -> None:
        self.watched.update(mints)
        await self._send({"method": "subscribeTokenTrade", "keys": mints})

    async def unwatch(self, mints: list[str]) -> None:
        self.watched.difference_update(mints)
        await self._send({"method": "unsubscribeTokenTrade", "keys": mints})

    @property
    def host(self) -> str:
        return self.urls[self.url_idx].split("/")[2]

    @property
    def degraded(self) -> bool:
        """On a backup feed, which may carry launches but not per-token trades."""
        return self.url_idx != 0

    async def watch_accounts(self, wallets: list[str]) -> None:
        self.accounts.update(wallets)
        await self._send({"method": "subscribeAccountTrade", "keys": wallets})

    async def events(self) -> AsyncIterator[Event]:
        import aiohttp

        backoff = 1
        while True:
            try:
                url = self.urls[self.url_idx]
                async with aiohttp.ClientSession() as session, session.ws_connect(url, heartbeat=20) as ws:
                    self.ws = ws
                    await ws.send_json({"method": "subscribeNewToken"})
                    await ws.send_json({"method": "subscribeMigration"})
                    if self.watched:
                        await ws.send_json({"method": "subscribeTokenTrade", "keys": list(self.watched)})
                    if self.accounts:
                        await ws.send_json({"method": "subscribeAccountTrade", "keys": list(self.accounts)})
                    backoff = 1
                    connected = time.time()
                    async for msg in ws:
                        if self.degraded and time.time() - connected > 300:
                            break                       # on a backup: go try the primary again
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        e = self.parse(json.loads(msg.data), time.time())
                        if e:
                            yield e
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                print(f"[feed] {self.host} disconnected: {err!r}; retrying in {backoff}s")
            self.ws = None
            self.url_idx = (self.url_idx + 1) % len(self.urls)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    @staticmethod
    def parse(d: dict, now: float) -> Event | None:
        tx = d.get("txType")
        if tx == "create":
            return Launch(mint=d["mint"], ts=now, creator=d.get("traderPublicKey", ""), name=d.get("name", ""),
                          symbol=d.get("symbol", ""), uri=d.get("uri", ""),
                          dev_buy_tokens=float(d.get("initialBuy") or 0), dev_buy_sol=float(d.get("solAmount") or 0),
                          v_sol=float(d.get("vSolInBondingCurve") or 30),
                          v_tokens=float(d.get("vTokensInBondingCurve") or 1_073_000_000))
        if tx in ("buy", "sell"):
            return Trade(mint=d["mint"], ts=now, trader=d.get("traderPublicKey", ""), side=tx,
                         sol=float(d.get("solAmount") or 0), tokens=float(d.get("tokenAmount") or 0),
                         v_sol=float(d.get("vSolInBondingCurve") or 0), v_tokens=float(d.get("vTokensInBondingCurve") or 0),
                         new_balance=float(d.get("newTokenBalance", -1)), signature=d.get("signature", ""),
                         pool=d.get("pool") or "pump", mcap_sol=float(d.get("marketCapSol") or 0))
        if tx == "migrate":
            return Migration(mint=d["mint"], ts=now)
        return None


async def fetch_metadata(uri: str) -> dict:
    """Token metadata JSON (twitter/telegram/website). Best effort, 3s budget."""
    import aiohttp

    if not uri:
        return {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s, s.get(uri) as r:
            return await r.json(content_type=None)
    except Exception:
        return {}


# --------------------------------------------------------------------------- replay
class FileFeed(Feed):
    realtime = False

    def __init__(self, *paths: str | Path):
        self.paths = [Path(p) for p in paths]

    async def events(self) -> AsyncIterator[Event]:
        for p in self.paths:
            with p.open() as f:
                for line in f:
                    if line.strip():
                        e = loads(line)
                        self._last = e.ts
                        yield e


# --------------------------------------------------------------------------- synthetic
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WORDS = ["PEPE", "DOGE", "CAT", "MOON", "WIF", "BONK", "FROG", "TRUMP", "AI", "GOAT", "CHAD", "PNUT", "MOG",
         "BRETT", "SIGMA", "GIGA", "BOME", "SLERF", "POPCAT", "MEW", "NEIRO", "TURBO", "HAMSTER", "WOJAK"]
ARCHETYPES = [("bundle_rug", 0.30), ("dev_dump", 0.18), ("dud", 0.34), ("fake_runner", 0.10), ("runner", 0.08)]
# phases: (duration_s range, buy probability, mean seconds between trades)
PHASES = {
    "bundle_rug":  [((20, 70), 0.62, 1.2), ((15, 40), 0.25, 0.8), ((60, 120), 0.40, 4.0)],
    "dev_dump":    [((40, 160), 0.66, 1.0), ((30, 60), 0.30, 1.5), ((60, 120), 0.45, 5.0)],
    "dud":         [((40, 200), 0.52, 4.0)],
    "fake_runner": [((25, 80), 0.78, 0.45), ((20, 60), 0.22, 0.6), ((60, 120), 0.40, 3.0)],
    "runner":      [((60, 240), 0.76, 0.35), ((60, 200), 0.56, 0.5), ((60, 300), 0.66, 0.4),
                    ((120, 400), 0.30, 0.7), ((120, 300), 0.45, 3.0)],
}


def _addr(rng: random.Random, suffix: str = "") -> str:
    return "".join(rng.choice(B58) for _ in range(44 - len(suffix))) + suffix


class SyntheticFeed(Feed):
    """speed: 1 = real time; 0 = as fast as possible (backtests)."""

    def __init__(self, seed: int | None = None, speed: float = 1.0, launches: int | None = None,
                 launch_every_s: float = 4.0, start_ts: float | None = None):
        self.rng = random.Random(seed)
        self.speed = speed
        self.realtime = speed > 0
        self.launches = launches
        self.launch_every = launch_every_s
        self.t0 = start_ts if start_ts is not None else time.time()
        self.serial_creators = [_addr(self.rng) for _ in range(5)]
        self.counter = itertools.count()
        self.archetype: dict[str, str] = {}      # ground truth, for evaluating the strategy
        # simulated wallets to copy: two genuinely skilled ones and one "bait" KOL who dumps on copiers
        self.smart = [_addr(self.rng) for _ in range(2)]
        self.bait = _addr(self.rng)
        self.leader_labels = {self.smart[0]: "sim-smart-1", self.smart[1]: "sim-smart-2", self.bait: "sim-bait-kol"}

    def now(self) -> float:
        if self.realtime:
            return self.t0 + (time.time() - getattr(self, "_wall0", time.time())) * self.speed
        return getattr(self, "_last", self.t0)

    def _token(self, t0: float) -> list[Event]:
        rng = self.rng
        kind = rng.choices([a for a, _ in ARCHETYPES], [w for _, w in ARCHETYPES])[0]
        mint = _addr(rng, "pump")
        self.archetype[mint] = kind
        creator = rng.choice(self.serial_creators) if kind in ("bundle_rug", "dev_dump") and rng.random() < 0.5 \
            else _addr(rng)
        word = rng.choice(WORDS) + rng.choice(["", "", "2", "INU", "AI", rng.choice(string.ascii_uppercase)])
        curve = Curve()
        dev_sol = rng.choice([0, 0.2, 0.5, 1, 2]) * (3 if kind in ("bundle_rug", "dev_dump") else 1)
        dev_tokens = curve.quote_buy(dev_sol, 1.25) if dev_sol else 0.0
        curve.apply(dev_sol * 0.9875, -dev_tokens)
        socials = rng.random() < (0.7 if kind in ("runner", "fake_runner") else 0.4)
        out: list[Event] = [Launch(mint=mint, ts=t0, creator=creator, name=word.title(), symbol=word,
                                   dev_buy_tokens=dev_tokens, dev_buy_sol=dev_sol, v_sol=curve.v_sol,
                                   v_tokens=curve.v_tokens,
                                   twitter=f"https://x.com/{word.lower()}" if socials else "",
                                   telegram=f"https://t.me/{word.lower()}" if socials and rng.random() < .6 else "")]
        holders: dict[str, float] = {creator: dev_tokens} if dev_tokens else {}
        bundlers: list[str] = []

        def trade(ts: float, who: str, side: str, amount: float) -> None:
            if curve.progress >= 1:
                return
            if side == "buy":
                tok = curve.quote_buy(amount, 1.25)
                if tok <= 0:
                    return
                curve.apply(amount * 0.9875, -tok)
                holders[who] = holders.get(who, 0) + tok
                sol = amount
            else:
                tok = min(amount, holders.get(who, 0))
                if tok <= 0:
                    return
                gross = curve.quote_sell(tok, 0)
                curve.apply(-gross, tok)
                holders[who] -= tok
                sol = gross * 0.9875
            out.append(Trade(mint=mint, ts=ts, trader=who, side=side, sol=round(sol, 6), tokens=tok,
                             v_sol=curve.v_sol, v_tokens=curve.v_tokens, new_balance=holders[who]))

        # leader wallets' scheduled actions: (time, wallet, side, sol)
        sched: list[tuple] = []
        p_smart = {"runner": .6, "fake_runner": .35, "dev_dump": .15, "dud": .1, "bundle_rug": .05}[kind]
        smart_in = [w for w in self.smart if rng.random() < p_smart]
        for w in smart_in:
            tb = t0 + rng.uniform(4, 25)
            sched.append((tb, w, "buy", rng.uniform(0.8, 3)))
            if kind not in ("runner", "fake_runner"):
                sched.append((tb + rng.uniform(20, 60), w, "sell", 0))
        if rng.random() < .35:
            tb = t0 + rng.uniform(3, 10)
            sched += [(tb, self.bait, "buy", rng.uniform(1, 4)), (tb + rng.uniform(6, 15), self.bait, "sell", 0)]
        sched.sort()
        leaders = set(self.leader_labels)

        def run_sched(until: float) -> None:
            while sched and sched[0][0] <= until:
                ts, w, side, sol = sched.pop(0)
                trade(ts, w, side, sol if side == "buy" else holders.get(w, 0))

        if kind == "bundle_rug":
            for _ in range(rng.randint(3, 8)):
                b = _addr(rng)
                bundlers.append(b)
                trade(t0 + rng.uniform(0.2, 1.6), b, "buy", rng.uniform(0.5, 2.5))
        t = t0 + 0.5
        phases = PHASES[kind]
        for i, ((lo, hi), p_buy, gap) in enumerate(phases):
            end = t + rng.uniform(lo, hi)
            if kind == "bundle_rug" and i == 1:          # bundlers dump
                for b in bundlers:
                    t += rng.uniform(0.2, 2.0)
                    trade(t, b, "sell", holders.get(b, 0))
            if kind == "dev_dump" and i == 1 and dev_tokens:
                t += 0.5
                trade(t, creator, "sell", holders.get(creator, 0))
            if (kind, i) in (("runner", 3), ("fake_runner", 1)):     # skilled wallets exit as the move tops
                for w in smart_in:
                    sched.append((t + rng.uniform(0, 15), w, "sell", 0))
                sched.sort()
            while t < end and curve.progress < 1:
                t += rng.expovariate(1 / gap)
                run_sched(t)
                existing = [h for h, v in holders.items()
                            if v > 0 and h != creator and h not in bundlers and h not in leaders]
                if rng.random() < p_buy or not existing:
                    who = rng.choice(existing) if existing and rng.random() < 0.15 else _addr(rng)
                    trade(t, who, "buy", min(rng.lognormvariate(-1.6, 1.0), 8))
                else:
                    who = rng.choice(existing)
                    trade(t, who, "sell", holders[who] * rng.choice([1, 1, 0.5, 0.3]))
            if curve.progress >= 1:
                out.append(Migration(mint=mint, ts=t + 1))
                break
        run_sched(t + 120)
        out.sort(key=lambda e: e.ts)
        return out

    async def events(self) -> AsyncIterator[Event]:
        heap: list = []
        n = 0
        next_launch = self.t0
        wall0 = self._wall0 = time.time()
        while True:
            while (self.launches is None or n < self.launches) and (not heap or next_launch <= heap[0][0]):
                for e in self._token(next_launch):
                    heapq.heappush(heap, (e.ts, next(self.counter), e))
                n += 1
                next_launch += self.rng.expovariate(1 / self.launch_every)
            if not heap:
                return
            ts, _, e = heapq.heappop(heap)
            if self.realtime:
                delay = (ts - self.t0) / self.speed - (time.time() - wall0)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last = ts
            yield e
