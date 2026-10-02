"""DexScreener public API (no key; ~300 req/min on pair/token endpoints, 60/min on profiles/boosts)."""
from __future__ import annotations

from ..models import Candidate
from .http import get

BASE = "https://api.dexscreener.com"


def latest_profiles() -> list[str]:
    return [x["tokenAddress"] for x in get(f"{BASE}/token-profiles/latest/v1") if x.get("chainId") == "solana"]


def latest_boosts() -> list[str]:
    return [x["tokenAddress"] for x in get(f"{BASE}/token-boosts/latest/v1") if x.get("chainId") == "solana"]


def pairs_for(mints: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(mints), 30):  # endpoint takes up to 30 addresses
        out += get(f"{BASE}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []
    return out


def best_pair_by_mint(mints: list[str]) -> dict[str, dict]:
    """Most liquid SOL-quoted pair per mint."""
    best: dict[str, dict] = {}
    for p in pairs_for(mints):
        if p.get("quoteToken", {}).get("symbol") not in ("SOL", "WSOL"):
            continue
        mint = p["baseToken"]["address"]
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if mint not in best or liq > ((best[mint].get("liquidity") or {}).get("usd") or 0):
            best[mint] = p
    return best


def to_candidate(p: dict, source: str = "") -> Candidate:
    txns5 = (p.get("txns") or {}).get("m5") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    return Candidate(
        mint=p["baseToken"]["address"],
        symbol=p["baseToken"].get("symbol", ""),
        pair_address=p.get("pairAddress", ""),
        price_usd=float(p.get("priceUsd") or 0),
        price_native=float(p.get("priceNative") or 0),
        liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
        fdv_usd=float(p.get("fdv") or 0),
        volume_5m_usd=float(vol.get("m5") or 0),
        volume_1h_usd=float(vol.get("h1") or 0),
        buys_5m=int(txns5.get("buys") or 0),
        sells_5m=int(txns5.get("sells") or 0),
        price_change_5m_pct=float(chg.get("m5") or 0),
        price_change_1h_pct=float(chg.get("h1") or 0),
        pair_created_at=(p.get("pairCreatedAt") or 0) / 1000,
        source=source,
    )
