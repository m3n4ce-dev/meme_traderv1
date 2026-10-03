"""Insider-cluster detection from the wallet funding graph.

Insiders split supply across fresh wallets they fund themselves, so those wallets dodge the
top-holder and bundle-timing gates - but they share a funder (often the dev, or a wallet the
dev funded). Research: in one well-known case 23 deployer-linked wallets held 40% of supply.

Lookups (cached, recorded into the feed file so backtests replay them):
  helius - GET api.helius.xyz/v1/wallet/{w}/funded-by (paid plan, 100 credits/call; labels exchanges)
  rpc    - any RPC: the wallet's oldest signature -> its first SOL transfer in. Cheap; best on fresh
           wallets (exactly what insiders use). Wallets with 1000+ txs are left "unknown".
Exchanges fund thousands of unrelated wallets, so a funder labelled as an exchange, or seen funding
many wallets, never counts as a link.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import Counter

from .curve import TOTAL_SUPPLY
from .events import Funding


class FundingResolver:
    def __init__(self, backend: str, max_per_min: int):
        self.backend = backend
        self.helius_key = os.environ.get("HELIUS_API_KEY", "")
        self.rpc_url = os.environ.get("SOLANA_RPC_URL", "")
        self.max_per_min = max_per_min
        self.calls: list[float] = []
        self.sem = asyncio.Semaphore(8)

    @property
    def available(self) -> bool:
        return bool(self.helius_key) if self.backend == "helius" else bool(self.rpc_url)

    def _budget_ok(self) -> bool:
        now = time.time()
        self.calls = [t for t in self.calls if t > now - 60]
        return len(self.calls) < self.max_per_min

    async def lookup(self, session, wallet: str) -> Funding | None:
        if not self._budget_ok():
            return None
        self.calls.append(time.time())
        async with self.sem:
            try:
                if self.backend == "helius":
                    return await self._helius(session, wallet)
                return await self._rpc(session, wallet)
            except Exception:
                return None

    async def _helius(self, session, wallet: str) -> Funding:
        url = f"https://api.helius.xyz/v1/wallet/{wallet}/funded-by"
        async with session.get(url, params={"api-key": self.helius_key}) as r:
            if r.status == 404:                      # never funded
                return Funding(wallet, time.time())
            if r.status != 200:
                raise RuntimeError(f"helius funded-by {r.status}")
            d = await r.json(content_type=None)
        return Funding(wallet, time.time(), d.get("funder") or "", (d.get("funderType") or "").lower())

    async def _rpc(self, session, wallet: str) -> Funding:
        async def call(method, params):
            async with session.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                        "params": params}) as r:
                d = await r.json(content_type=None)
            if r.status != 200 or "error" in d:      # rate limit etc.: don't cache this wallet as "unknown"
                raise RuntimeError(f"{method}: {d.get('error') or r.status}")
            return d.get("result")

        sigs = await call("getSignaturesForAddress", [wallet, {"limit": 1000}]) or []
        if not sigs or len(sigs) >= 1000:            # unknown: no history, or too old to be a fresh insider
            return Funding(wallet, time.time())
        landed = [x for x in reversed(sigs) if not x.get("err")][:3]   # oldest successful first
        for x in landed:
            tx = await call("getTransaction", [x["signature"],
                                               {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if tx is None:                           # not retrievable right now: retry later, don't cache
                raise RuntimeError("transaction not available yet")
            src = first_sol_source(tx, wallet)
            if src:
                return Funding(wallet, time.time(), src)
        return Funding(wallet, time.time())


def first_sol_source(tx: dict | None, wallet: str) -> str:
    """Sender of the SOL transfer / account creation that funded `wallet` in a jsonParsed transaction.
    A failed transaction moved nothing, so it never counts as funding."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return ""
    ixs = list(tx.get("transaction", {}).get("message", {}).get("instructions", []))
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs += inner.get("instructions", [])
    for ix in ixs:
        p = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(p, dict):
            continue
        info = p.get("info", {})
        if p.get("type") in ("transfer", "transferWithSeed", "transferChecked") and info.get("destination") == wallet \
                and "lamports" in info:
            return info.get("source", "")
        if p.get("type") in ("createAccount", "createAccountWithSeed") and info.get("newAccount") == wallet:
            return info.get("source", "")
    return ""


def cohort(s, size: int) -> list[str]:
    """Wallets that matter for insider analysis: the first `size` non-dev buyers + the current top 10."""
    out: list[str] = []
    for t in s.trades:
        w = t[4]
        if t[2] == "buy" and w != s.creator and w not in out:
            out.append(w)
            if len(out) >= size:
                break
    for w, _ in sorted(s.holders.items(), key=lambda kv: -kv[1])[:10]:
        if w != s.creator and w not in out:
            out.append(w)
    return out


def cluster_report(s, wallets: list[str], funders: dict, fanout: Counter, exchange_fanout: int) -> dict:
    """Supply held by wallets linked to the dev or to each other through funding."""
    def usable(f: str, ftype: str) -> bool:
        return bool(f) and ftype != "exchange" and fanout[f] < exchange_fanout

    dev = s.creator
    dev_funder = funders.get(dev, ("", ""))[0] if dev else ""
    groups: dict[str, list[str]] = {}
    linked: set[str] = set()
    cohort_set = set(wallets)
    for w in wallets:
        f, ftype = funders.get(w, ("", ""))
        if not usable(f, ftype):
            continue
        groups.setdefault(f, []).append(w)
        if f == dev or (dev_funder and f == dev_funder) or f in cohort_set:
            linked.add(w)                         # funded by the dev, the dev's funder, or another early buyer
    for f, ws in groups.items():
        if len(ws) >= 2:
            linked.update(ws)                     # several early buyers share one (non-exchange) funder
    pct = sum(s.holders.get(w, 0.0) for w in linked) / TOTAL_SUPPLY * 100
    biggest = max((len(v) for v in groups.values()), default=0)
    known = sum(1 for w in wallets if funders.get(w, ("", ""))[0])
    return {"pct": pct, "linked": len(linked), "biggest_group": biggest, "known": known, "cohort": len(wallets)}
