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
import dataclasses
import heapq
import itertools
import json
import os
import random
import string
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator

from .curve import Curve
from .events import Event, Funding, Launch, Migration, Trade, loads


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

    def __init__(self, fallback_urls: list[str] | None = None, use_key: bool = True):
        key = os.environ.get("PUMPPORTAL_API_KEY", "") if use_key else ""
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
                        try:
                            e = self.parse(json.loads(msg.data), time.time())
                        except (ValueError, TypeError, KeyError, AttributeError):
                            continue                    # one malformed message must not stop the feed
                        if e:
                            yield e
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
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
                         new_balance=float(d["newTokenBalance"]) if d.get("newTokenBalance") is not None else -1.0,
                         signature=d.get("signature", ""),
                         pool=d.get("pool") or "pump", mcap_sol=float(d.get("marketCapSol") or 0))
        if tx == "migrate":
            return Migration(mint=d["mint"], ts=now)
        return None


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_EVENT = bytes.fromhex("bddb7fd34ee661ee")        # sha256("event:TradeEvent")[:8] (Anchor)
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"


class SolanaTradeFeed(Feed):
    """Launches + migrations from PumpPortal's free streams; trades read straight from the pump.fun
    program's own logs (logsSubscribe + its Anchor TradeEvent) over a Solana RPC websocket.

    Same events as PumpPortalFeed without the 0.01 SOL / 10k-trade meter. The cost is bandwidth:
    every bonding-curve trade (~2-3k/min, ~20 GB/day), so point SOLANA_WS_URL at a provider that
    doesn't bill per MB (Helius bills 2 credits / 0.1 MB = ~12M credits/month). The public endpoint
    is free but best-effort: if trades stop arriving the feed reports degraded and entries pause.
    """
    STALL_S = 30            # no log notification for this long = stalled; reconnect
    EARLY_S = 15            # hold unwatched trades this long: a launch's first buys (often its insider
                            # bundle) can land before PumpPortal announces it; replay them on watch()

    def __init__(self, ws_url: str = "", fallback_urls: list[str] | None = None):
        self.ws_url = ws_url or os.environ.get("SOLANA_WS_URL") or PUBLIC_WS
        self.launches = PumpPortalFeed(fallback_urls, use_key=False)
        self.watched: set[str] = set()
        self.accounts: set[str] = set()
        self.trades_up = False
        self.early: dict[str, list[Trade]] = {}
        self._early_order: deque[tuple[float, str]] = deque()
        self._q: asyncio.Queue | None = None
        self.dev_buys: dict[str, tuple[str, float]] = {}   # mint -> (creator, tokens) until its event is seen

    async def watch(self, mints: list[str]) -> None:
        self.watched.update(mints)
        now = time.time()
        for m in mints:
            for t in self.early.pop(m, ()):
                if self._q is not None and not self._is_dev_buy(t):
                    self._q.put_nowait(dataclasses.replace(t, ts=now))   # keep the feed's clock monotonic

    def _is_dev_buy(self, t: Trade) -> bool:
        """The launch already carries the creator's initial buy; its TradeEvent would count it twice."""
        dev = self.dev_buys.get(t.mint)
        if dev and t.side == "buy" and t.trader == dev[0] and abs(t.tokens - dev[1]) <= max(dev[1], 1) * 1e-6:
            del self.dev_buys[t.mint]
            return True
        return False

    def _hold(self, t: Trade) -> None:
        if t.mint not in self.early:
            self._early_order.append((t.ts, t.mint))
        self.early.setdefault(t.mint, []).append(t)
        while self._early_order and t.ts - self._early_order[0][0] > self.EARLY_S:
            self.early.pop(self._early_order.popleft()[1], None)

    async def unwatch(self, mints: list[str]) -> None:
        self.watched.difference_update(mints)

    async def watch_accounts(self, wallets: list[str]) -> None:
        self.accounts.update(wallets)

    @property
    def host(self) -> str:
        return self.launches.host if self.launches.degraded else self.ws_url.split("/")[2].split("?")[0]

    @property
    def degraded(self) -> bool:
        return self.launches.degraded or not self.trades_up

    @staticmethod
    def parse_logs(value: dict, now: float) -> list[Trade]:
        """Trades from one logsSubscribe notification value ({signature, err, logs}).

        `mentions` matches whole transactions, so any program in one can log bytes that look like a
        TradeEvent. An event only counts while pump.fun itself is the executing program, tracked through
        the runtime's invoke/success/failed lines; a stack that doesn't add up rejects the whole tx."""
        import base64
        import struct

        from solders.pubkey import Pubkey

        if value.get("err"):
            return []                                   # failed transactions moved nothing
        out = []
        stack: list[str] = []
        for line in value.get("logs") or ():
            if line.startswith("Program ") and not line.startswith(("Program log:", "Program data:",
                                                                      "Program return:")):
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "invoke":
                    if parts[3] != f"[{len(stack) + 1}]":
                        return []                       # depth doesn't match what we've seen: untrustworthy
                    stack.append(parts[1])
                elif len(parts) >= 3 and (parts[2] == "success" or parts[2].startswith("failed")):
                    if not stack or stack[-1] != parts[1]:
                        return []
                    stack.pop()
                continue                                # "consumed N of M compute units" etc.
            if line.startswith("Log truncated"):
                return []                               # the rest of the stack is unknown
            if not line.startswith("Program data: "):
                continue
            if not stack or stack[-1] != PUMP_PROGRAM:
                continue                                # emitted by some other program
            try:
                raw = base64.b64decode(line[14:])
            except ValueError:
                continue
            if raw[:8] != TRADE_EVENT or len(raw) < 129:
                continue
            sol, tokens = struct.unpack_from("<QQ", raw, 40)
            v_sol, v_tokens = struct.unpack_from("<QQ", raw, 97)
            out.append(Trade(mint=str(Pubkey.from_bytes(raw[8:40])), ts=now,
                             trader=str(Pubkey.from_bytes(raw[57:89])), side="buy" if raw[56] else "sell",
                             sol=sol / 1e9, tokens=tokens / 1e6, v_sol=v_sol / 1e9, v_tokens=v_tokens / 1e6,
                             signature=value.get("signature", "")))
        return out

    async def _trades(self, q: asyncio.Queue) -> None:
        import aiohttp

        backoff = 1
        while True:
            try:
                async with aiohttp.ClientSession() as session, \
                        session.ws_connect(self.ws_url, heartbeat=20, max_msg_size=0) as ws:
                    await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                        "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}]})
                    while True:
                        msg = await ws.receive(timeout=self.STALL_S)
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        try:
                            value = json.loads(msg.data)["params"]["result"]["value"]
                        except (ValueError, KeyError, TypeError):
                            continue                    # subscription reply etc.
                        self.trades_up, backoff = True, 1
                        for t in self.parse_logs(value, time.time()):
                            if t.mint in self.watched or t.trader in self.accounts:
                                if not self._is_dev_buy(t):
                                    q.put_nowait(t)
                            else:
                                self._hold(t)
            except Exception as err:                    # anything: never leave a dead stream marked up
                print(f"[feed] trade logs {self.host} down: {err!r}; retrying in {backoff}s")
            self.trades_up = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _launches(self, q: asyncio.Queue) -> None:
        async for e in self.launches.events():
            if isinstance(e, Launch):
                if e.dev_buy_tokens > 0:
                    self.dev_buys[e.mint] = (e.creator, e.dev_buy_tokens)
                    if len(self.dev_buys) > 5000:          # events never seen (e.g. while trades were down)
                        self.dev_buys.pop(next(iter(self.dev_buys)))
                q.put_nowait(e)
            elif not isinstance(e, Trade):              # keyless: PumpPortal sends no trades anyway
                q.put_nowait(e)

    async def events(self) -> AsyncIterator[Event]:
        q = self._q = asyncio.Queue()
        tasks = [asyncio.ensure_future(self._launches(q)), asyncio.ensure_future(self._trades(q))]
        try:
            while True:
                yield await q.get()
        finally:
            for t in tasks:
                t.cancel()


METADATA_MAX_BYTES = 64 * 1024
_metadata_slots: asyncio.Semaphore | None = None


def _public_ip(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _allowed_url(url: str) -> bool:
    """http(s) with a host name, or a literal IP only if it's public (aiohttp skips the resolver for
    literal IPs, so those are checked here)."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url)
        host = u.hostname
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not host:
        return False
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True                                     # a name: the resolver checks what it points to
    return _public_ip(host)


class _PublicOnlyResolver:
    """aiohttp resolver that refuses names resolving to loopback, private, link-local or other non-public
    addresses. Runs for every connection, so redirects and DNS re-resolution can't reach them either."""

    def __init__(self):
        from aiohttp.resolver import DefaultResolver

        self._inner = DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addrs = [a for a in await self._inner.resolve(host, port, family) if _public_ip(a["host"])]
        if not addrs:
            raise OSError(f"{host} has no public address")
        return addrs

    async def close(self):
        await self._inner.close()


async def fetch_metadata(uri: str) -> dict:
    """Token metadata JSON (twitter/telegram/website). Best effort, 3 s budget.

    The URI is written by the token's creator, so it's treated as hostile: http(s) only, public
    addresses only (checked at connect time, redirects included), at most 3 redirects, a 64 KB body
    cap, 8 fetches at once, and only short string fields come back."""
    global _metadata_slots
    from urllib.parse import urljoin

    import aiohttp

    if not isinstance(uri, str) or not _allowed_url(uri):
        return {}
    if _metadata_slots is None:
        _metadata_slots = asyncio.Semaphore(8)
    try:
        async with _metadata_slots:
            conn = aiohttp.TCPConnector(resolver=_PublicOnlyResolver(), limit=2)
            async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=3)) as s:
                url, body = uri, None
                for _ in range(4):                      # the request + at most 3 redirects, each one checked
                    async with s.get(url, allow_redirects=False) as r:
                        if r.status in (301, 302, 303, 307, 308):
                            url = urljoin(url, r.headers.get("Location", ""))
                            if not _allowed_url(url):
                                return {}
                            continue
                        if r.status != 200 or (r.content_length or 0) > METADATA_MAX_BYTES:
                            return {}
                        body = await r.content.read(METADATA_MAX_BYTES + 1)
                        break
                if body is None or len(body) > METADATA_MAX_BYTES:
                    return {}
        d = json.loads(body)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: d[k][:200] for k in ("twitter", "telegram", "website") if isinstance(d.get(k), str)}


# --------------------------------------------------------------------------- replay
def dedupe_feed_paths(paths) -> list[Path]:
    """feed-X.jsonl and feed-X.jsonl.gz both present (mid-compression): keep the finished .gz only."""
    ps = [Path(p) for p in paths]
    names = {p.name for p in ps}
    return [p for p in ps if not (p.suffix == ".jsonl" and p.name + ".gz" in names)]


def compress_file(path: Path, level: int = 6) -> Path:
    """feed-X.jsonl -> feed-X.jsonl.gz (~8-10x smaller). Written to a temp name first and the plain
    file only removed once the archive is complete, so a crash mid-way loses nothing."""
    import gzip
    import shutil

    out = path.with_name(path.name + ".gz")
    tmp = path.with_name(path.name + ".gz.part")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=level) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    tmp.replace(out)
    path.unlink()
    return out


class FileFeed(Feed):
    """Replays recorded feed files (.jsonl or .jsonl.gz). Tolerates the damage a crash leaves behind:
    a half-written last line, or a .gz whose end was never written."""
    realtime = False

    def __init__(self, *paths: str | Path):
        self.paths = dedupe_feed_paths(paths)
        self.bad_lines = 0

    async def events(self) -> AsyncIterator[Event]:
        import gzip
        import zlib

        for p in self.paths:
            f = gzip.open(p, "rt") if p.suffix == ".gz" else p.open()
            try:
                while True:
                    try:
                        line = f.readline()
                    except (EOFError, zlib.error, gzip.BadGzipFile, UnicodeDecodeError):
                        self.bad_lines += 1             # truncated archive: keep what was readable
                        break
                    if not line:
                        break
                    if not line.strip():
                        continue
                    try:
                        e = loads(line)
                    except (ValueError, KeyError, TypeError):
                        self.bad_lines += 1             # half-written line from a crash
                        continue
                    self._last = e.ts
                    yield e
            finally:
                f.close()


# --------------------------------------------------------------------------- synthetic
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WORDS = ["PEPE", "DOGE", "CAT", "MOON", "WIF", "BONK", "FROG", "TRUMP", "AI", "GOAT", "CHAD", "PNUT", "MOG",
         "BRETT", "SIGMA", "GIGA", "BOME", "SLERF", "POPCAT", "MEW", "NEIRO", "TURBO", "HAMSTER", "WOJAK"]
ARCHETYPES = [("bundle_rug", 0.30), ("dev_dump", 0.18), ("dud", 0.27), ("fake_runner", 0.10), ("runner", 0.08),
              ("stealth_rug", 0.07)]
# phases: (duration_s range, buy probability, mean seconds between trades)
PHASES = {
    "bundle_rug":  [((20, 70), 0.62, 1.2), ((15, 40), 0.25, 0.8), ((60, 120), 0.40, 4.0)],
    "dev_dump":    [((40, 160), 0.66, 1.0), ((30, 60), 0.30, 1.5), ((60, 120), 0.45, 5.0)],
    "dud":         [((40, 200), 0.52, 4.0)],
    # insiders buy from dev-funded fresh wallets spread over the first minute (dodging bundle/sniper
    # timing gates), let organic buyers pile in, then dump - only the funding graph exposes them
    "stealth_rug": [((50, 110), 0.72, 0.5), ((20, 50), 0.22, 0.6), ((60, 120), 0.40, 3.0)],
    "fake_runner": [((25, 80), 0.78, 0.45), ((20, 60), 0.22, 0.6), ((60, 120), 0.40, 3.0)],
    "runner":      [((60, 240), 0.76, 0.35), ((60, 200), 0.56, 0.5), ((60, 300), 0.66, 0.4),
                    ((120, 400), 0.30, 0.7), ((120, 300), 0.45, 3.0)],
}


def _addr(rng: random.Random, suffix: str = "") -> str:
    return "".join(rng.choices(B58, k=44 - len(suffix))) + suffix


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
        self.exchanges = [_addr(self.rng) for _ in range(6)]     # funders labelled "exchange" (as Helius does)
        self.funded: set[str] = set()

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
            if who not in self.funded:
                self.funded.add(who)
                if rng.random() < 0.6:
                    out.append(Funding(who, ts - 0.001, rng.choice(self.exchanges), "exchange"))
                elif rng.random() < 0.5:
                    out.append(Funding(who, ts - 0.001, _addr(rng)))       # its own unrelated funder
                else:
                    out.append(Funding(who, ts - 0.001, ""))               # old wallet, unknown
            out.append(Trade(mint=mint, ts=ts, trader=who, side=side, sol=round(sol, 6), tokens=tok,
                             v_sol=curve.v_sol, v_tokens=curve.v_tokens, new_balance=holders[who]))

        # leader wallets' scheduled actions: (time, wallet, side, sol)
        sched: list[tuple] = []
        p_smart = {"runner": .6, "fake_runner": .35, "dev_dump": .15, "dud": .1, "bundle_rug": .05, "stealth_rug": .05}[kind]
        smart_in = [w for w in self.smart if rng.random() < p_smart]
        for w in smart_in:
            tb = t0 + rng.uniform(4, 25)
            sched.append((tb, w, "buy", rng.uniform(0.8, 3)))
            if kind not in ("runner", "fake_runner"):
                sched.append((tb + rng.uniform(20, 60), w, "sell", 0))
        if rng.random() < .35:
            tb = t0 + rng.uniform(3, 10)
            sched += [(tb, self.bait, "buy", rng.uniform(1, 4)), (tb + rng.uniform(6, 15), self.bait, "sell", 0)]
        insiders: list[str] = []
        if kind == "stealth_rug":
            mid = _addr(rng)                                       # dev -> mid wallet -> insiders (2 hops)
            out.append(Funding(mid, t0 - 30, creator))
            self.funded.add(mid)
            for _ in range(rng.randint(5, 9)):
                w = _addr(rng)
                insiders.append(w)
                out.append(Funding(w, t0 - 20, rng.choice([creator, mid])))
                self.funded.add(w)
                sched.append((t0 + rng.uniform(3, 55), w, "buy", rng.uniform(0.6, 2.0)))
        sched.sort()
        leaders = set(self.leader_labels) | set(insiders)
        pool: list[str] = []          # organic (non-dev/bundler/leader) wallets that may hold tokens

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
            if kind == "stealth_rug" and i == 1:                   # insiders dump
                for w in insiders:
                    sched.append((t + rng.uniform(0, 10), w, "sell", 0))
                sched.sort()
            if (kind, i) in (("runner", 3), ("fake_runner", 1)):     # skilled wallets exit as the move tops
                for w in smart_in:
                    sched.append((t + rng.uniform(0, 15), w, "sell", 0))
                sched.sort()
            while t < end and curve.progress < 1:
                t += rng.expovariate(1 / gap)
                run_sched(t)
                # organic holders pool, pruned lazily (was an O(holders) scan on every trade)
                while pool and holders.get(pool[-1], 0) <= 0:
                    pool.pop()
                existing = pool
                if rng.random() < p_buy or not existing:
                    if existing and rng.random() < 0.15:
                        who = rng.choice(existing)
                    else:
                        who = _addr(rng)
                        pool.append(who)
                    trade(t, who, "buy", min(rng.lognormvariate(-1.6, 1.0), 8))
                else:
                    i = rng.randrange(len(existing))
                    who = existing[i]
                    if holders.get(who, 0) <= 0:
                        existing[i] = existing[-1]
                        existing.pop()
                        continue
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
