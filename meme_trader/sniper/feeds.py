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
        self.last_launch = 0.0                          # when a new coin last came in (on any server)
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
            why = "connection closed"
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
                            why = "trying the primary again"
                            break                       # on a backup: go try the primary again
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            e = self.parse(json.loads(msg.data), time.time())
                        except (ValueError, TypeError, KeyError, AttributeError):
                            continue                    # one malformed message must not stop the feed
                        if e:
                            if isinstance(e, Launch):
                                self.last_launch = e.ts
                            yield e
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                why = f"disconnected: {err!r}"
            self.ws = None
            old = self.host
            self.url_idx = (self.url_idx + 1) % len(self.urls)
            print(f"[feed] launches {old}: {why[:160]}; switching to {self.host} in {backoff}s")
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
WSOL = "So11111111111111111111111111111111111111112"


def trade_quote_mint(raw: bytes) -> str:
    """The coin's quote asset from a TradeEvent: '' = SOL, else its mint (USDC, $PUMP, ...).

    pump.fun added non-SOL quotes in 2026. The current layout continues after last_update_timestamp (ends at
    258) with ix_name (u32 length + bytes), mayhem_mode (bool), cashback and buyback fee bps + amount (4 x u64),
    shareholders (u32 count x 34 bytes), then quote_mint. All zeros, WSOL, or an older event without the field
    = SOL. For a non-SOL coin the event's SOL fields are 0 (measured 2026-10-04: ~3% of trades)."""
    import struct

    o = 258
    if len(raw) < o + 4:
        return ""
    n = struct.unpack_from("<I", raw, o)[0]
    o += 4 + n + 1 + 32
    if n > 64 or len(raw) < o + 4:
        return ""
    k = struct.unpack_from("<I", raw, o)[0]
    o += 4 + 34 * k
    if k > 64 or len(raw) < o + 32:
        return ""
    q = raw[o:o + 32]
    if q == bytes(32):
        return ""
    from solders.pubkey import Pubkey

    m = str(Pubkey.from_bytes(q))
    return "" if m == WSOL else m


def ipfs_urls(url: str) -> list[str]:
    """The URL, then the same IPFS content through other gateways: ipfs.io (where pump.fun metadata points)
    and dweb.link answer 429 to busy IPs (measured 2026-10-04), these didn't."""
    import re

    url = str(url or "").strip()
    if url.startswith("ipfs://"):
        url = "https://ipfs.io/ipfs/" + url[len("ipfs://"):].removeprefix("ipfs/")
    m = re.search(r"/ipfs/([A-Za-z0-9]{40,100}(?:/[^?#\s]*)?)", url)
    if not m:
        return [url]
    return [url] + [g + m.group(1) for g in IPFS_GATEWAYS if not url.startswith(g)]


IPFS_GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://4everland.io/ipfs/", "https://gateway.pinata.cloud/ipfs/")
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
# free, keyless endpoints tried after the configured ones (both throttle a 24/7 stream eventually)
FALLBACK_WS = ["wss://solana-rpc.publicnode.com", PUBLIC_WS]


def ws_urls(configured="") -> list[str]:
    """Endpoints in order: sniper.feed.ws_url (string, comma-separated string or list), else SOLANA_WS_URL
    (comma-separated), then the free fallbacks. Duplicates dropped."""
    raw = configured or os.environ.get("SOLANA_WS_URL", "")
    first = [u.strip() for u in (raw if isinstance(raw, list) else str(raw).split(",")) if u and u.strip()]
    out: list[str] = []
    for u in first + FALLBACK_WS:
        if u not in out:
            out.append(u)
    return out


class FeedQuality:
    """Live completeness check of the trade stream. Each pump.fun trade carries the curve's token reserves
    after it, so a token's trades must chain exactly (reserves - bought, + sold). A trade whose previous state
    never shows up within REORDER_N more trades is one the endpoint dropped; free endpoints quietly drift from ~0% to 30%+
    as they throttle, without ever closing the connection. Order doesn't count against an endpoint: measured
    2026-10-06, RPC Fast delivers ~5% of trades out of order within their slot, with ~0.3% really missing, and
    a strict in-order check read that as 4-5% missing, next to the 5% that switches endpoints."""

    REORDER_N = 200         # a trade whose previous state arrives within this many more trades (~3 s at the
                            # day's peak, ~25 a slot) was reordered, not dropped

    def __init__(self, max_gap_pct: float = 5.0, window: int = 2000, min_checks: int = 400,
                 max_lag_s: float = 5.0, min_lags: int = 200, reorder_n: int | None = None):
        self.max_gap_pct = max_gap_pct
        self.reorder_n = self.REORDER_N if reorder_n is None else reorder_n
        self.min_checks = min_checks
        self.checks: deque[bool] = deque(maxlen=window)
        self.ends: dict[str, deque] = {}               # mint -> its recent end states (token reserves after a trade)
        self.pending: dict[str, list] = {}             # mint -> [(start state, deadline)] whose previous trade is due
        self.firsts: dict[str, float] = {}             # mint -> the start state of the earliest of its trades seen
        self._n = 0
        # latency: receive time minus the trade's on-chain Clock time. Complete is not enough - measured
        # 2026-10-03, PublicNode's log stream ran 12 s behind the chain (p99 13.6 s) with ~0% missing,
        # while the public RPC was 1-2 s behind. A stale feed makes every decision on old prices.
        self.max_lag_s = max_lag_s
        self.min_lags = min_lags
        self.lags: deque[float] = deque(maxlen=600)

    def reset(self) -> None:
        self.checks.clear()
        self.ends.clear()
        self.pending.clear()
        self.firsts.clear()
        self.lags.clear()

    @property
    def lag_s(self) -> float | None:
        if not self.lags:
            return None
        xs = sorted(self.lags)
        return round(xs[len(xs) // 2], 1)

    @property
    def slow(self) -> bool:
        return len(self.lags) >= self.min_lags and self.max_lag_s > 0 and (self.lag_s or 0) > self.max_lag_s

    def observe(self, t: Trade) -> None:
        if t.chain_ts > 0:
            self.lags.append(t.ts - t.chain_ts)
        self._n += 1
        n = self._n
        ends = self.ends.pop(t.mint, None)             # (re-inserted below: the dict stays least-recent first)
        start = t.v_tokens + t.tokens if t.side == "buy" else t.v_tokens - t.tokens    # reserves before this trade
        if ends is None:                               # the first trade seen of this coin: nothing to chain to
            ends = deque(maxlen=16)
            self.firsts[t.mint] = start
        elif abs(self.firsts.get(t.mint, -9.0) - t.v_tokens) < 1.0:
            self.firsts[t.mint] = start                # it came before the first one seen: nothing to chain to either
        elif any(abs(e - start) < 1.0 for e in ends):
            self.checks.append(True)
        else:                                          # its previous trade may still be on its way
            self.pending.setdefault(t.mint, []).append((start, n + self.reorder_n))
        ends.append(t.v_tokens)
        self.ends[t.mint] = ends
        waiting = self.pending.get(t.mint)
        if waiting:                                    # this trade arrived after the one that follows it
            keep = [(st, dl) for st, dl in waiting if abs(st - t.v_tokens) >= 1.0 and dl >= n]
            self.checks.extend([True] * sum(1 for st, _ in waiting if abs(st - t.v_tokens) < 1.0))
            self.checks.extend([False] * sum(1 for st, dl in waiting if abs(st - t.v_tokens) >= 1.0 and dl < n))
            if keep:
                self.pending[t.mint] = keep
            else:
                del self.pending[t.mint]
        if n % 50 == 0:                                # other coins' overdue ones: their previous trade never came
            for m in [m for m, w in self.pending.items() if any(dl < n for _, dl in w)]:
                w = self.pending[m]
                self.checks.extend([False] * sum(1 for _, dl in w if dl < n))
                left = [x for x in w if x[1] >= n]
                if left:
                    self.pending[m] = left
                else:
                    del self.pending[m]
        if len(self.ends) > 20_000:                    # bounded: forget the least recently traded token
            m = next(iter(self.ends))
            self.ends.pop(m)
            self.pending.pop(m, None)
            self.firsts.pop(m, None)

    @property
    def gap_pct(self) -> float:
        return 100.0 * (1 - sum(self.checks) / len(self.checks)) if self.checks else 0.0

    @property
    def measured(self) -> bool:
        return len(self.checks) >= self.min_checks

    @property
    def bad(self) -> bool:
        return self.measured and self.gap_pct > self.max_gap_pct


class SolanaTradeFeed(Feed):
    """Launches + migrations from PumpPortal's free streams; trades read straight from the pump.fun
    program's own logs (logsSubscribe + its Anchor TradeEvent) over a Solana RPC websocket.

    Same events as PumpPortalFeed without the 0.01 SOL / 10k-trade meter. The cost is bandwidth:
    every bonding-curve trade (~2-3k/min, ~20 GB/day), so point SOLANA_WS_URL at a provider that
    doesn't bill per MB (Helius bills 2 credits / 0.1 MB = ~12M credits/month). The public endpoint
    is free but best-effort: if trades stop arriving the feed reports degraded and entries pause.
    """
    STALL_S = 30            # no websocket message at all for this long = dead connection; reconnect
    non_sol_skipped = 0     # trades on coins quoted in USDC/$PUMP/...: skipped, their SOL fields are 0
    EARLY_S = 15            # hold unwatched trades this long: a launch's first buys (often its insider
                            # bundle) can land before PumpPortal announces it; replay them on watch()
    RETRY_PRIMARY_S = 1800  # on a fallback endpoint, go back and try the first one this often
    LAG_MEMORY_S = 1800     # how long an endpoint's measured lag counts when choosing where to go
    STARTUP_MEMORY_S = 6 * 3600   # at startup, begin on the fastest endpoint measured this recently

    def __init__(self, ws_url="", fallback_urls: list[str] | None = None, commitment: str = "confirmed",
                 max_gap_pct: float = 5.0, stall_s: float = 60.0, max_lag_s: float = 5.0, memory_path=None,
                 backup_ws_url: str = "", backup_mb_per_day: float = 1200.0, backup_bank_mb: float = 4800.0):
        self.ws_urls = ws_urls(ws_url)
        # A metered backup (your Helius key's websocket by default), used only while the free endpoints are down or
        # behind, up to backup_mb_per_day of stream a day: Helius bills websockets 2 credits per 0.1 MB, and this
        # stream is ~400 MB an hour, so its free 1M credits a month cover ~4 hours a day of outages.
        key = os.environ.get("HELIUS_API_KEY", "").strip()
        backup = backup_ws_url or os.environ.get("SOLANA_WS_BACKUP_URL", "") or (f"wss://mainnet.helius-rpc.com/?api-key={key}" if key else "")
        self.backup_idx: int | None = None
        if backup and backup not in self.ws_urls:
            self.ws_urls.append(backup)
            self.backup_idx = len(self.ws_urls) - 1
        # The allowance is a bank: it fills at backup_mb_per_day (so a month never uses more than 30 days' worth)
        # and holds up to backup_bank_mb, so a bad night can use what quiet days saved. Measured 2026-10-05 01:00:
        # the stream ran ~650 MB an hour, and a flat 1200 MB a day lasted under two hours of an outage.
        self.backup_mb_per_day = float(backup_mb_per_day)
        self.backup_bank_cap = max(float(backup_bank_mb), self.backup_mb_per_day) if self.backup_mb_per_day > 0 else 0.0
        self.backup_bank = self.backup_bank_cap
        self.backup_bank_ts = time.time()
        self.backup_used: dict[str, float] = {}      # UTC day -> MB streamed from the backup (for the display)
        self._usage_path = str(memory_path).replace("feed_endpoints.json", "feed_backup_usage.json") if memory_path else ""
        self._usage_saved = 0.0                      # kept on disk: a restart must not refill the bank
        try:
            if self._usage_path:
                d = json.loads(open(self._usage_path).read())
                if "bank" in d:
                    self.backup_bank, self.backup_bank_ts = float(d["bank"]), float(d["ts"])
                    self.backup_used = {k: float(v) for k, v in (d.get("days") or {}).items()}
                else:                                # the first format: MB used per day
                    self.backup_used = {k: float(v) for k, v in d.items()}
                    self.backup_bank = max(0.0, self.backup_bank_cap - self.backup_used.get(time.strftime("%Y-%m-%d", time.gmtime()), 0.0))
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            pass
        self._probe_task: asyncio.Task | None = None # on the backup: a side connection measuring a free endpoint
        self.probe_log: deque = deque(maxlen=50)     # (when, host, lag or None)
        raw = ws_url or os.environ.get("SOLANA_WS_URL", "")
        # endpoints you configured (SOLANA_WS_URL / feed.ws_url) are worth going back to; the free fallbacks aren't
        self.n_configured = len([u for u in (raw if isinstance(raw, list) else str(raw).split(",")) if u and u.strip()])
        self.ws_idx = 0
        self.memory_path = memory_path
        self._last_lag_save = 0.0
        # dropped connections per endpoint (times), and what the stream weighs (uncompressed bytes, for paid plans
        # that bill by data): a free endpoint that just hangs up is reconnected, not abandoned for a slower one
        self.closes: dict[int, deque] = {}
        self.switches: deque = deque(maxlen=500)
        self.bytes_in: deque = deque()              # (minute, bytes) for the last hour
        self.connected_since = 0.0
        # "confirmed": a fraction of a second later than "processed", but complete. Measured 2026-10-03 on
        # PublicNode: processed missed ~25% of trades (reserve-chain gaps), confirmed 0.4%.
        self.commitment = commitment
        # watchdog: pump.fun trades every second, so this long with no decoded trade means the stream is
        # broken even if the connection is up (seen 2026-10-03: a 91-minute silence on an open socket)
        self.stall_s = stall_s
        self.quality = FeedQuality(max_gap_pct, max_lag_s=max_lag_s)
        # endpoint index -> (median lag s, when measured). Seen 2026-10-04: mainnet-beta's lag spiked past 5 s
        # for a moment, the watchdog moved to PublicNode (10 s behind), then straight back: 40 switches in
        # 35 min, each pausing entries while the new connection was measured. Never move to a known-worse one.
        self.endpoint_lag: dict[int, tuple[float, float]] = {}
        self._load_lags()
        self.last_trade = 0.0
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
    def ws_url(self) -> str:
        return self.ws_urls[self.ws_idx]

    @staticmethod
    def _hostname(url: str) -> str:
        return url.split("/")[2].split("?")[0] if "//" in url else url

    @property
    def host(self) -> str:
        return self._hostname(self.ws_url)

    LAUNCH_QUIET_S = 60     # new coins arrive ~30 a minute: a minute without one means the launch feed is broken

    @property
    def gap_pct(self) -> float:
        return round(self.quality.gap_pct, 2)

    @property
    def lag_s(self) -> float | None:
        return self.quality.lag_s

    @property
    def degraded_reason(self) -> str:
        # The launch feed's backup (pumpdev.io) carries new coins like PumpPortal does (measured 2026-10-05: 27-47 a
        # minute, against 30 on average); trades come from the RPC here. So only a backup that's gone quiet pauses entries.
        quiet = time.time() - self.launches.last_launch
        if self.launches.degraded and quiet > self.LAUNCH_QUIET_S:
            return f"launch feed on backup {self.launches.host}: no new coins for {quiet:.0f}s"
        if not self.trades_up:
            return f"no trade data from {self.host}"
        if not self.quality.measured and not self._trusted():    # ~10-30 s after (re)connecting
            return f"checking trade data from {self.host}"
        if self.quality.bad:
            return f"trade data from {self.host} incomplete ({self.quality.gap_pct:.0f}% missing)"
        if self.quality.slow:
            return f"trade data from {self.host} is {self.quality.lag_s:.0f}s behind the chain"
        return ""

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reason)

    @staticmethod
    def parse_logs(value: dict, now: float, slot: int = 0) -> list[Trade]:
        """Trades from one logsSubscribe notification value ({signature, err, logs}); slot from its context.

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
        n_event = -1                                     # TradeEvents seen in this transaction, in log order
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
            n_event += 1
            if trade_quote_mint(raw):                   # not a SOL coin: this bot prices everything in SOL
                SolanaTradeFeed.non_sol_skipped += 1
                continue
            # TradeEvent: mint 8, sol 40, tokens 48, is_buy 56, user 57, timestamp 89, virtual reserves 97/105,
            # real reserves 113/121, fee recipient 129, fee bps 161, fee 169, creator 177, creator fee bps 209
            sol, tokens = struct.unpack_from("<QQ", raw, 40)
            chain_ts, = struct.unpack_from("<q", raw, 89)
            v_sol, v_tokens = struct.unpack_from("<QQ", raw, 97)
            fee_bps, creator_bps = (struct.unpack_from("<Q", raw, 161)[0], struct.unpack_from("<Q", raw, 209)[0]) \
                if len(raw) >= 217 else (-1, -1)
            out.append(Trade(mint=str(Pubkey.from_bytes(raw[8:40])), ts=now,
                             trader=str(Pubkey.from_bytes(raw[57:89])), side="buy" if raw[56] else "sell",
                             sol=sol / 1e9, tokens=tokens / 1e6, v_sol=v_sol / 1e9, v_tokens=v_tokens / 1e6,
                             signature=value.get("signature", ""), slot=int(slot), chain_ts=float(chain_ts),
                             fee_bps=int(fee_bps), creator_fee_bps=int(creator_bps), event_index=n_event))
        return out

    def _load_lags(self) -> None:
        """Start on the endpoint that was fastest in a recent run (seen 2026-10-04: every restart began on
        PublicNode, 10 s behind, and paused entries until the watchdog moved on). An endpoint you configured that
        hasn't been measured yet (a paid one just added, 2026-10-06) is tried first: the remembered free ones
        would otherwise win, and the feed would only go back to yours after RETRY_PRIMARY_S."""
        if not self.memory_path:
            return
        try:
            saved = json.loads(open(self.memory_path).read())
        except (OSError, ValueError):
            saved = {}
        now, best, fresh = time.time(), None, None
        for i, u in enumerate(self.ws_urls):
            v = saved.get(self._hostname(u))
            if isinstance(v, list) and len(v) == 2 and now - v[1] < self.STARTUP_MEMORY_S:
                self.endpoint_lag[i] = (float(v[0]), float(v[1]))
                if i == self.backup_idx:                     # never start on the metered backup
                    continue
                if best is None or v[0] < self.endpoint_lag[best][0]:
                    best = i
            elif fresh is None and i < self.n_configured and i != self.backup_idx:
                fresh = i
        if fresh is not None:
            self.ws_idx = fresh
        elif best is not None:
            self.ws_idx = best

    def _save_lags(self, now: float) -> None:
        """Hostname -> [lag, when]: never a full URL, which can carry an API key."""
        if not self.memory_path or now - self._last_lag_save < 60:
            return
        self._last_lag_save = now
        try:
            tmp = str(self.memory_path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump({self._hostname(self.ws_urls[i]): [round(lag, 2), ts] for i, (lag, ts) in self.endpoint_lag.items()}, f)
            os.replace(tmp, self.memory_path)
        except OSError:
            pass

    BACKUP_RETRY_S = 300    # on the backup, look at the free endpoints again this often
    PROBE_S = 40            # ... for this long, on a side connection (trading carries on over the backup)
    GOOD_LAG_S = 3.0        # a free endpoint measured this close behind the chain gets the stream back

    def _save_usage(self, now: float) -> None:
        if not self._usage_path or now - self._usage_saved < 30:
            return
        self._usage_saved = now
        try:
            tmp = self._usage_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"bank": round(self.backup_bank, 2), "ts": self.backup_bank_ts,
                           "days": {k: round(v, 2) for k, v in self.backup_used.items()}}, f)
            os.replace(tmp, self._usage_path)
        except OSError:
            pass

    async def _probe(self, i: int, secs: float | None = None) -> float | None:
        """Measure how far behind a free endpoint is, on its own connection, without using its trades.
        The median lag, or None if it hung up or sent too little to judge."""
        import aiohttp

        q = FeedQuality(max_lag_s=self.quality.max_lag_s, min_lags=self.quality.min_lags)
        try:
            async with aiohttp.ClientSession() as session, \
                    session.ws_connect(self.ws_urls[i], heartbeat=20, max_msg_size=0) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                    "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": self.commitment}]})
                end = time.time() + (secs or self.PROBE_S)
                while time.time() < end:
                    msg = await ws.receive(timeout=self.STALL_S)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        return None
                    try:
                        r = json.loads(msg.data)["params"]["result"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    for t in self.parse_logs(r["value"], time.time(), (r.get("context") or {}).get("slot", 0)):
                        q.observe(t)
        except Exception:                           # refused, timed out, dropped: not ready
            return None
        return q.lag_s if len(q.lags) >= q.min_lags else None

    async def _probe_free(self) -> None:
        now = time.time()
        i = self._best_known(now)
        i = i if i is not None else self._free_order()[0]
        lag = await self._probe(i)
        if lag is not None:
            self.endpoint_lag[i] = (lag, time.time())
        self.probe_log.append((time.time(), self._hostname(self.ws_urls[i]), lag))

    def _backup_left_mb(self, now: float) -> float:
        if self.backup_idx is None:
            return 0.0
        if now > self.backup_bank_ts:                # refill at the daily rate, up to the bank's size
            self.backup_bank = min(self.backup_bank_cap, self.backup_bank + self.backup_mb_per_day * (now - self.backup_bank_ts) / 86400)
            self.backup_bank_ts = now
        return self.backup_bank

    # Seen 2026-10-05 10:17-10:37: with the allowance spent, the refill (~0.8 MB a minute) made the backup look usable
    # every 30 s; the bot moved over, spent it in seconds and moved back: 33 switches an hour, each pausing entries.
    BACKUP_MIN_MB = 100     # move onto the backup only with this much left (~10-15 minutes of the stream)

    def _backup_ok(self, now: float) -> bool:
        return self.backup_idx is not None and self.ws_idx != self.backup_idx and self._backup_left_mb(now) >= self.BACKUP_MIN_MB

    def _free_order(self) -> list[int]:
        """The free endpoints in rotation order after the current one (the backup is never just 'next')."""
        n = len(self.ws_urls)
        return [i for i in ((self.ws_idx + k) % n for k in range(1, n + 1)) if i != self.backup_idx]

    def _known_lag(self, i: int, now: float) -> float | None:
        v = self.endpoint_lag.get(i)
        return v[0] if v and now - v[1] < self.LAG_MEMORY_S else None

    def _faster_endpoint(self, now: float) -> int | None:
        """The next endpoint not measured slower than this one lately, or None (stay: the rest are worse)."""
        cur = self.quality.lag_s or 0.0
        for i in self._free_order():
            if i == self.ws_idx:
                continue
            lag = self._known_lag(i, now)
            if lag is None or lag < cur:
                return i
        return None

    def stream_stats(self, now: float | None = None) -> dict:
        """Reconnects and switches in the last hour, and the stream's size (uncompressed MB per hour)."""
        now = now or time.time()
        hour = [s for s in self.switches if now - s[0] < 3600]
        mins = [b for m, b in self.bytes_in if m < int(now // 60)]          # whole minutes only
        return {"reconnects_1h": sum(1 for s in hour if s[1] == s[2]), "switches_1h": sum(1 for s in hour if s[1] != s[2]),
                "mb_per_hour": round(sum(mins) / len(mins) * 60 / 1e6, 1) if mins else None,
                "up_for_s": round(now - self.connected_since) if self.trades_up and self.connected_since else 0,
                "backup": None if self.backup_idx is None else {
                    "host": self._hostname(self.ws_urls[self.backup_idx]), "on": self.ws_idx == self.backup_idx,
                    "mb_today": round(self.backup_used.get(time.strftime("%Y-%m-%d", time.gmtime(now)), 0.0), 1),
                    "mb_left": round(self._backup_left_mb(now), 1), "mb_per_day": self.backup_mb_per_day, "mb_bank": self.backup_bank_cap,
                    "last_probe": None if not self.probe_log else {"ago_s": round(now - self.probe_log[-1][0]),
                                                                    "host": self.probe_log[-1][1], "lag_s": self.probe_log[-1][2]}}}

    def _check_stream(self, now: float, connected: float) -> str:
        """Why the current endpoint should be dropped, or ''."""
        if self.backup_idx is not None and self.ws_idx == self.backup_idx:   # on the metered backup
            if self._backup_left_mb(now) <= 0:
                return "backup allowance used up"
            last = self.probe_log[-1] if self.probe_log else None
            if last and last[0] >= connected and last[2] is not None and last[2] <= self.GOOD_LAG_S:
                return "retry free"                      # a free endpoint measured good: hand the stream back
            busy = self._probe_task is not None and not self._probe_task.done()
            if not busy and now - max(connected, last[0] if last else 0) > self.BACKUP_RETRY_S:
                try:
                    self._probe_task = asyncio.get_running_loop().create_task(self._probe_free())
                except RuntimeError:                    # (no loop: called outside the feed, e.g. a test)
                    pass
        if now - self.last_trade > self.stall_s:
            return f"no pump.fun trades for {now - self.last_trade:.0f}s"
        if self.quality.bad:
            return f"{self.quality.gap_pct:.1f}% of trades missing"
        if len(self.quality.lags) >= self.quality.min_lags:
            self.endpoint_lag[self.ws_idx] = (self.quality.lag_s, now)
            self._save_lags(now)
        if self.quality.slow and (self._faster_endpoint(now) is not None or self._backup_ok(now)):
            return f"{self.quality.lag_s:.0f}s behind the chain"
        # (slow but every other endpoint was slower: stay put; entries stay paused until it catches up)
        if self.ws_idx != 0 and self.n_configured and now - connected > self.RETRY_PRIMARY_S:
            first, cur = self._known_lag(0, now), self.quality.lag_s
            if first is None or (cur is not None and first < cur):
                return "retry"
        if self.quality.measured and not self.quality.bad and not self.quality.slow:
            self._good = (self.ws_idx, now)              # this endpoint, measured fine just now
        return ""

    # Measured 2026-10-04/05 from the per-minute health records: entries were paused 77 and 109 minutes a day
    # "checking trade data" after reconnects, mostly hang-ups of the public RPC followed by a reconnect to the same
    # endpoint. Back on the endpoint that measured fine moments ago, its measurement stands until a new one is in.
    TRUST_S = 90

    def _trusted(self) -> bool:
        idx, ts = getattr(self, "_good", (-1, 0.0))
        return idx == self.ws_idx and time.time() - ts < self.TRUST_S

    QUICK_CLOSES = 4        # an endpoint that hangs up this often within CLOSE_WINDOW_S is given up on for now
    CLOSE_WINDOW_S = 180

    def _dropped(self, why: str) -> bool:
        """The connection failed (closed, refused, timed out), as opposed to the data being slow or incomplete."""
        return not why or not any(k in why for k in ("behind the chain", "trades missing", "no pump.fun trades", "retry"))

    def _best_known(self, now: float) -> int | None:
        lags = [(lag, i) for i in range(len(self.ws_urls)) if i != self.backup_idx and (lag := self._known_lag(i, now)) is not None]
        return min(lags)[1] if lags else None

    def _next_endpoint(self, why: str, now: float) -> int:
        if why in ("retry free", "backup allowance used up"):
            best = self._best_known(now)
            return best if best is not None else self._free_order()[0]
        if why == "retry":
            return 0
        if self._dropped(why):
            # Seen 2026-10-05 00:29-01:05: the public RPC hung up every 20 s-5 min; the bot moved to PublicNode
            # (10 s behind), measured that, moved back, and paused entries each time: dozens of switches an hour.
            # A hang-up on the fastest endpoint we know is a reconnect to it, unless it keeps hanging up.
            q = self.closes.setdefault(self.ws_idx, deque(maxlen=20))
            q.append(now)
            recent = sum(1 for t in q if now - t < self.CLOSE_WINDOW_S)
            if self.n_configured and self.ws_idx != 0:          # on a fallback: back to the endpoint you configured
                first, cur = self._known_lag(0, now), self._known_lag(self.ws_idx, now)
                if first is None or cur is None or first <= cur:
                    return 0
            if self.ws_idx == self.backup_idx:                  # the backup hung up: once more, else a free one
                return self.ws_idx if recent < self.QUICK_CLOSES and self._backup_left_mb(now) > 0 else self._free_order()[0]
            best = self._best_known(now)
            if recent < self.QUICK_CLOSES and (best is None or best == self.ws_idx):
                return self.ws_idx
            if recent >= self.QUICK_CLOSES and self._backup_ok(now):
                return self.backup_idx                          # the fast free one keeps hanging up: the backup
        if "behind the chain" in why:
            i = self._faster_endpoint(now)
            if i is not None:
                return i
            if self._backup_ok(now):
                return self.backup_idx
        return self._free_order()[0]

    async def _trades(self, q: asyncio.Queue) -> None:
        import aiohttp

        backoff = 1
        while True:
            why = ""
            try:
                async with aiohttp.ClientSession() as session, \
                        session.ws_connect(self.ws_url, heartbeat=20, max_msg_size=0) as ws:
                    await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                        "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": self.commitment}]})
                    connected = self.last_trade = self.connected_since = time.time()
                    self.quality.reset()
                    while not why:
                        msg = await ws.receive(timeout=self.STALL_S)
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            why = f"connection closed ({msg.type.name})"
                            break
                        now = time.time()
                        minute = int(now // 60)
                        if self.ws_idx == self.backup_idx:              # metered: count it against today's budget
                            day = time.strftime("%Y-%m-%d", time.gmtime(now))
                            self.backup_used[day] = self.backup_used.get(day, 0.0) + len(msg.data) / 1e6
                            self._backup_left_mb(now)
                            self.backup_bank = max(0.0, self.backup_bank - len(msg.data) / 1e6)
                            if len(self.backup_used) > 3:
                                self.backup_used.pop(min(self.backup_used))
                            self._save_usage(now)
                        if self.bytes_in and self.bytes_in[-1][0] == minute:
                            self.bytes_in[-1][1] += len(msg.data)
                        else:
                            self.bytes_in.append([minute, len(msg.data)])
                            while self.bytes_in and self.bytes_in[0][0] < minute - 60:
                                self.bytes_in.popleft()
                        try:
                            result = json.loads(msg.data)["params"]["result"]
                            value = result["value"]
                            slot = (result.get("context") or {}).get("slot", 0)
                        except (ValueError, KeyError, TypeError, AttributeError):
                            why = self._check_stream(now, connected)
                            continue                    # subscription reply etc.
                        self.trades_up = True
                        for t in self.parse_logs(value, now, slot):
                            self.last_trade = now
                            backoff = 1
                            self.quality.observe(t)
                            if t.mint in self.watched or t.trader in self.accounts:
                                if not self._is_dev_buy(t):
                                    q.put_nowait(t)
                            else:
                                self._hold(t)
                        why = self._check_stream(now, connected)
            except Exception as err:                    # anything: never leave a dead stream marked up
                why = f"HTTP {err.status} {err.message}" if hasattr(err, "status") else f"{type(err).__name__}: {err}"
            self.trades_up = False
            old, old_idx = self.host, self.ws_idx
            self.ws_idx = self._next_endpoint(why, time.time())
            self.switches.append((time.time(), old, self.host, why[:80]))
            if self.ws_idx == old_idx and self._dropped(why):
                backoff = 1                                  # a hang-up: straight back to the same fast endpoint
            print(f"[feed] trade logs {old}: {why[:200] if why != 'retry' else 'trying the first endpoint again'}"
                  f"; switching to {self.host} in {backoff}s")
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


async def read_capped(resp, cap: int) -> bytes | None:
    """The whole body, or None if it's longer than cap. (StreamReader.read(n) returns whatever has arrived so far,
    which can be a fraction of the body.)"""
    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > cap:
            return None
    return bytes(body)


async def fetch_metadata(uri: str, fields: tuple = ("twitter", "telegram", "website")) -> dict:
    """Token metadata JSON (twitter/telegram/website, or e.g. image). Best effort, 3 s budget.

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
                        body = await read_capped(r, METADATA_MAX_BYTES)
                        break
                if body is None or len(body) > METADATA_MAX_BYTES:
                    return {}
        d = json.loads(body)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: d[k][:300] for k in fields if isinstance(d.get(k), str)}


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
            try:
                f = gzip.open(p, "rt") if p.suffix == ".gz" else p.open()
            except FileNotFoundError:
                # the recorder compresses a finished day (writes the .gz, then removes the .jsonl): a replay queued
                # with the plain path reads the archive. Neither there: fail loudly, never skip a day
                if p.suffix != ".jsonl" or not p.with_name(p.name + ".gz").exists():
                    raise
                f = gzip.open(p.with_name(p.name + ".gz"), "rt")
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
