"""Social signals: contract addresses (CAs) posted on Telegram / X.

Research note: call channels' insiders often buy ~1-2 min BEFORE the post and sell into the
followers it brings. A call is therefore treated as a weak, *learned* signal: every caller
gets a running score from what the token did after their past calls, and buying purely on
a call is off by default (entry.allow_unknown_launch).

Adapters (both optional, enabled by env vars):
  Telegram - Telethon user session reading the channels in sniper.signals.telegram_channels
             (TELEGRAM_API_ID, TELEGRAM_API_HASH from my.telegram.org; pip install telethon)
  X        - X API v2 filtered stream with from:<account> rules (X_BEARER_TOKEN;
             pay-per-use tier allows 1 connection / 1,000 rules)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .events import Social

# base58, 32-44 chars; pump.fun vanity mints end in "pump"
CA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")


def extract_mints(text: str) -> list[str]:
    seen: list[str] = []
    for m in CA_RE.findall(text or ""):
        if m not in seen:
            seen.append(m)
    # prefer pump.fun mints; drop obvious non-mints (e.g. tx signatures are 87-88 chars and won't match)
    return sorted(seen, key=lambda m: not m.endswith("pump"))


@dataclass
class CallerStats:
    calls: int = 0
    avg_return: float = 0.0      # average % move from call price to price 5 min later
    pending: list = field(default_factory=list)   # [(mint, call_ts, call_price)]

    @property
    def weight(self) -> float:
        """0..1 confidence; neutral 0.25 until we've seen enough calls."""
        if self.calls < 5:
            return 0.25
        return max(0.0, min(1.0, 0.5 + self.avg_return / 200))


class CallerBook:
    """Tracks each caller's track record. Persisted to data/callers.json."""

    def __init__(self, path: Path | None, horizon_s: float = 300):
        """path None = in-memory only (replays: every caller starts neutral, nothing learned today leaks in)."""
        self.path = path
        self.horizon = horizon_s
        self.stats: dict[str, CallerStats] = {}
        if path is not None and path.exists():
            for k, v in json.loads(path.read_text()).items():
                self.stats[k] = CallerStats(v["calls"], v["avg_return"])

    def on_call(self, caller: str, mint: str, ts: float, price: float) -> None:
        self.stats.setdefault(caller, CallerStats()).pending.append((mint, ts, price))

    def settle(self, now: float, price_of: Callable[[str], float | None]) -> None:
        for st in self.stats.values():
            keep = []
            for mint, ts, p0 in st.pending:
                if now - ts < self.horizon:
                    keep.append((mint, ts, p0))
                    continue
                p1 = price_of(mint)
                if p0 and p1:
                    r = (p1 / p0 - 1) * 100
                    st.avg_return = (st.avg_return * st.calls + r) / (st.calls + 1)
                    st.calls += 1
            st.pending = keep

    def weight(self, caller: str) -> float:
        return self.stats.get(caller, CallerStats()).weight

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps({k: {"calls": v.calls, "avg_return": v.avg_return}
                                         for k, v in self.stats.items()}, indent=1))


Emit = Callable[[Social], Awaitable[None]]


async def run_telegram(channels: list, emit: Emit) -> None:
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    if not (channels and api_id and api_hash):
        return
    from telethon import TelegramClient, events  # optional dependency

    client = TelegramClient(str(Path("data") / "telegram"), int(api_id), api_hash)
    await client.start()

    @client.on(events.NewMessage(chats=channels))
    async def handler(ev):
        chat = getattr(ev.chat, "username", None) or str(ev.chat_id)
        for mint in extract_mints(ev.raw_text):
            await emit(Social(mint=mint, ts=time.time(), source="telegram", author=chat, text=ev.raw_text[:280]))

    await client.run_until_disconnected()


X_RULE_TAG = "meme_trader"


class XStreamFatal(Exception):
    """Credentials/permissions problem: retrying won't help."""


def x_retry_delay(status: int, headers, attempt: int) -> float:
    """Seconds to wait before reconnecting after an HTTP error from the X stream."""
    if status == 429:
        try:
            reset = float(headers.get("x-rate-limit-reset", 0))
        except (TypeError, ValueError):
            reset = 0
        if reset > time.time():
            return min(reset - time.time() + 1, 900)
        return min(60 * 2 ** attempt, 900)
    return min(5 * 2 ** attempt, 320)


async def sync_x_rules(s, base: str, accounts: list) -> None:
    """Replace only this bot's (tagged) rules. Rules other apps keep on the same X project survive."""
    async with s.get(f"{base}/rules") as r:
        if r.status in (401, 403):
            raise XStreamFatal(f"X rules: HTTP {r.status} (check X_BEARER_TOKEN and its access level)")
        if r.status != 200:
            raise RuntimeError(f"X rules: HTTP {r.status}")
        mine = [x["id"] for x in (await r.json()).get("data", []) if x.get("tag") == X_RULE_TAG]
    if mine:
        async with s.post(f"{base}/rules", json={"delete": {"ids": mine}}) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"X rules delete: HTTP {r.status}")
    rules = [{"value": " OR ".join(f"from:{a}" for a in accounts[i:i + 20]), "tag": X_RULE_TAG}
             for i in range(0, len(accounts), 20)]
    async with s.post(f"{base}/rules", json={"add": rules}) as r:
        if r.status in (401, 403):
            raise XStreamFatal(f"X rules add: HTTP {r.status}")
        if r.status not in (200, 201):
            raise RuntimeError(f"X rules add: HTTP {r.status}")


async def run_x_stream(accounts: list, emit: Emit) -> None:
    token = os.environ.get("X_BEARER_TOKEN")
    if not (accounts and token):
        return
    import aiohttp

    base = "https://api.x.com/2/tweets/search/stream"
    headers = {"Authorization": f"Bearer {token}"}
    attempt = 0
    async with aiohttp.ClientSession(headers=headers) as s:
        while True:
            try:
                await sync_x_rules(s, base, accounts)
                break
            except XStreamFatal as e:
                print(f"[x] {e} - X signals disabled")
                return
            except Exception as e:
                delay = min(15 * 2 ** attempt, 600)
                attempt += 1
                print(f"[x] rule setup failed ({e!r}); retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
        attempt = 0
        while True:
            delay = 15.0
            try:
                async with s.get(base, params={"expansions": "author_id", "user.fields": "username"},
                                 timeout=aiohttp.ClientTimeout(total=None, sock_read=90)) as r:
                    if r.status in (401, 403):
                        print(f"[x] stream: HTTP {r.status} - X signals disabled (check X_BEARER_TOKEN)")
                        return
                    if r.status != 200:
                        delay = x_retry_delay(r.status, r.headers, attempt)
                        attempt += 1
                        print(f"[x] stream: HTTP {r.status}; reconnecting in {delay:.0f}s")
                        await asyncio.sleep(delay)
                        continue
                    attempt = 0
                    async for line in r.content:
                        if not line.strip():
                            continue
                        d = json.loads(line)
                        if "errors" in d and "data" not in d:
                            print(f"[x] stream error message: {str(d['errors'])[:200]}")
                            continue
                        users = {u["id"]: u["username"] for u in d.get("includes", {}).get("users", [])}
                        tw = d.get("data", {})
                        for mint in extract_mints(tw.get("text", "")):
                            await emit(Social(mint=mint, ts=time.time(), source="x",
                                              author=users.get(tw.get("author_id"), "?"), text=tw.get("text", "")[:280]))
            except Exception as e:
                delay = min(15 * 2 ** attempt, 600)
                attempt += 1
                print(f"[x] stream error {e!r}; reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
