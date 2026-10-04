"""Watch-only portfolio: wallets you list (yours or anyone's), their SOL and tokens priced in dollars, and their
latest transactions. Addresses only, never keys: nothing here can move funds. The list lives in
data/portfolio.json (git-ignored). Balances come from SOLANA_RPC_URL (else the public RPC), prices and logos
from DexScreener.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

from ..config import ROOT

PATH = ROOT / "data" / "portfolio.json"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
MAX_WALLETS = 25
STALE_S = 60


class PortfolioError(ValueError):
    pass


def valid_address(addr: str) -> bool:
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(addr)
        return 32 <= len(addr) <= 44
    except Exception:
        return False


async def _rpc(s: aiohttp.ClientSession, url: str, method: str, params: list):
    async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
        if r.status == 429:
            raise PortfolioError("the RPC is rate-limiting: add your own SOLANA_RPC_URL under API keys")
        if r.status != 200:
            raise PortfolioError(f"RPC {method}: HTTP {r.status}")
        d = await r.json(content_type=None)
    if "error" in d:
        raise PortfolioError(f"RPC {method}: {str(d['error'].get('message', d['error']))[:120]}")
    return d["result"]


def best_pairs(rows: list) -> dict[str, dict]:
    """mint -> its most liquid DexScreener pair (where it's the base token)."""
    out: dict[str, dict] = {}
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        m = (p.get("baseToken") or {}).get("address")
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if m and (m not in out or liq > ((out[m].get("liquidity") or {}).get("usd") or 0)):
            out[m] = p
    return out


class Portfolio:
    def __init__(self, path: Path = PATH, sol_usd=lambda: None):
        self.path = Path(path)
        self.sol_usd = sol_usd
        self.wallets: list[dict] = []
        if self.path.exists():
            try:
                self.wallets = [w for w in json.loads(self.path.read_text()) if valid_address(w.get("address", ""))]
            except ValueError:
                self.wallets = []
        self.cache: dict[str, dict] = {}
        self._busy: set[str] = set()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.wallets, indent=1))
        tmp.replace(self.path)

    def add(self, address: str, label: str = "", mine: bool = False) -> None:
        address = (address or "").strip()
        if not valid_address(address):
            raise PortfolioError("that isn't a Solana address")
        if any(w["address"] == address for w in self.wallets):
            raise PortfolioError("already watching that wallet")
        if len(self.wallets) >= MAX_WALLETS:
            raise PortfolioError(f"at most {MAX_WALLETS} wallets")
        label = " ".join(str(label or "").split())[:40] or address[:4] + "…" + address[-4:]
        self.wallets.append({"address": address, "label": label, "mine": bool(mine), "added": time.time()})
        self._save()

    def remove(self, address: str) -> None:
        self.wallets = [w for w in self.wallets if w["address"] != address]
        self.cache.pop(address, None)
        self._save()

    def view(self) -> dict:
        ws = []
        for w in self.wallets:
            c = self.cache.get(w["address"]) or {}
            ws.append({**w, **c, "loading": w["address"] in self._busy})
        mine = sum(w.get("total_usd") or 0 for w in ws if w.get("mine"))
        return {"wallets": ws, "mine_usd": mine, "sol_usd": self.sol_usd()}

    def stale(self) -> list[str]:
        now = time.time()
        return [w["address"] for w in self.wallets if w["address"] not in self._busy
                and now - (self.cache.get(w["address"]) or {}).get("updated", 0) > STALE_S]

    async def refresh(self, addresses: list[str] | None = None) -> None:
        for a in addresses if addresses is not None else self.stale():
            if a in self._busy or not any(w["address"] == a for w in self.wallets):
                continue
            self._busy.add(a)
            try:
                self.cache[a] = await self.fetch(a)
            except (PortfolioError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                prev = self.cache.get(a) or {}
                self.cache[a] = {**prev, "error": str(e)[:200] or type(e).__name__, "updated": time.time()}
            finally:
                self._busy.discard(a)

    async def fetch(self, addr: str) -> dict:
        url = os.environ.get("SOLANA_RPC_URL") or PUBLIC_RPC
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            lamports = (await _rpc(s, url, "getBalance", [addr]))["value"]
            accounts = []
            for prog in TOKEN_PROGRAMS:
                res = await _rpc(s, url, "getTokenAccountsByOwner",
                                 [addr, {"programId": prog}, {"encoding": "jsonParsed"}])
                accounts += res["value"]
            sigs = await _rpc(s, url, "getSignaturesForAddress", [addr, {"limit": 12}])
            held: dict[str, dict] = {}
            for a in accounts:
                info = (((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                amt = (info.get("tokenAmount") or {})
                ui = float(amt.get("uiAmount") or 0)
                m = info.get("mint")
                if m and ui > 0:
                    h = held.setdefault(m, {"mint": m, "amount": 0.0})
                    h["amount"] += ui
            pairs: dict[str, dict] = {}
            mints = list(held)
            for i in range(0, len(mints), 30):
                try:
                    async with s.get(DS_TOKENS + ",".join(mints[i:i + 30])) as r:
                        pairs.update(best_pairs(await r.json(content_type=None) if r.status == 200 else []))
                except (aiohttp.ClientError, ValueError):
                    pass
        sol_usd = self.sol_usd() or 0.0
        tokens = []
        for m, h in held.items():
            p = pairs.get(m) or {}
            price = float(p.get("priceUsd") or 0) or None
            tokens.append({**h, "symbol": (p.get("baseToken") or {}).get("symbol") or "", "name": (p.get("baseToken") or {}).get("name") or "",
                           "price_usd": price, "value_usd": h["amount"] * price if price else None,
                           "change_24h": (p.get("priceChange") or {}).get("h24"),
                           "liquidity_usd": (p.get("liquidity") or {}).get("usd"), "dex": p.get("dexId"),
                           "url": p.get("url") if str(p.get("url", "")).startswith("https://dexscreener.com/") else ""})
        tokens.sort(key=lambda t: -(t["value_usd"] or 0))
        sol = lamports / 1e9
        priced = sum(t["value_usd"] or 0 for t in tokens)
        return {"sol": sol, "sol_value_usd": sol * sol_usd if sol_usd else None, "tokens": tokens,
                "tokens_usd": priced, "total_usd": priced + (sol * sol_usd if sol_usd else 0),
                "unpriced": sum(1 for t in tokens if t["value_usd"] is None),
                "activity": [{"sig": x["signature"], "ts": x.get("blockTime"), "ok": x.get("err") is None,
                              "memo": (x.get("memo") or "")[:80]} for x in sigs],
                "updated": time.time(), "error": ""}
