"""Sizing agent: dollar-denominated buy size from signal strength, with a hard cap.

Owner's rules (intel brief §5): base $5 for anything that passes the full filter stack,
scale toward $20 with signal strength, HARD cap $20 per buy, size copies smaller than direct
signals. Strength blends the pre-checks we can measure live:
  rule score above the bar, AI-desk conviction, buy pressure, smart wallets already in,
  momentum (price near its high). Size is also capped as a % of the SOL in the curve so a
  buy doesn't move the price too much or advertise itself to snipers.
"""
from __future__ import annotations

import math
import os
import time

from ..models import SOL_MINT


class SolPrice:
    """SOL/USD with a fallback. Jupiter Price v3 (needs JUPITER_API_KEY) -> CoinGecko -> config fallback.
    (lite-api.jup.ag, used by the older callout bot, was shut down 2026-01-31.)"""

    def __init__(self, fallback_usd: float):
        self.usd = float(fallback_usd)
        self.source = "fallback"
        self.ts = 0.0

    async def refresh(self) -> None:
        import aiohttp

        sources = []
        if os.environ.get("JUPITER_API_KEY"):
            sources.append(("jupiter", f"https://api.jup.ag/price/v3?ids={SOL_MINT}",
                            {"x-api-key": os.environ["JUPITER_API_KEY"]},
                            lambda d: d[SOL_MINT]["usdPrice"]))
        sources.append(("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                        {}, lambda d: d["solana"]["usd"]))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            for name, url, headers, pick in sources:
                try:
                    async with s.get(url, headers=headers) as r:
                        px = float(pick(await r.json(content_type=None)))
                    if 1 < px < 100_000:
                        self.usd, self.source, self.ts = px, name, time.time()
                        return
                except Exception:
                    continue


def strength(rule_score: float, min_score: float, buy_sell_ratio: float, near_high: float,
             smart_buyers: int, desk_mult: float | None, edge: float | None = None) -> tuple[float, list[str]]:
    """0..1 signal strength + the components, for the log. edge: fractional-Kelly bankroll share from the
    probability model (0.05 = full marks); None when no model is loaded."""
    def clamp(x: float) -> float:
        return max(0.0, min(1.0, x))

    parts = {
        "score": clamp((rule_score - min_score) / max(100 - min_score, 1)),
        "pressure": clamp((buy_sell_ratio - 1) / 2),
        "momentum": clamp((near_high - 0.8) / 0.2),
        "smart": clamp(smart_buyers / 3),
    }
    weights = {"score": 0.3, "pressure": 0.2, "momentum": 0.15, "smart": 0.2}
    if desk_mult is not None:
        parts["desk"] = clamp(desk_mult - 0.5)        # desk size_mult 0.5..1.5 -> 0..1
        weights["desk"] = 0.3
    if edge is not None:
        parts["edge"] = clamp(edge / 0.05)
        weights["edge"] = 0.3
    total = sum(weights.values())
    st = sum(parts[k] * w for k, w in weights.items()) / total
    return st, [f"{k} {v:.2f}" for k, v in parts.items()]


def size_usd(z, st: float, kind: str, curve_real_sol: float, sol_usd: float, mult: float = 1.0,
             defense: float = 1.0) -> tuple[float, str]:
    """z: params.sniper.sizing. Returns (usd, why). Never exceeds z.max_usd or the curve-liquidity cap;
    0.0 means "no trade" (the cap is below the smallest order worth paying fees on: half the base size)."""
    usd = z.base_usd + (z.max_usd - z.base_usd) * st
    why = f"strength {st:.2f}"
    if kind == "copy":
        usd *= z.copy_multiplier
        why += f", copy x{z.copy_multiplier}"
    elif kind == "late" and mult != 1.0:
        usd *= mult
        why += f", late x{mult}"
    if defense < 1.0:
        usd *= defense
        why += f", defense x{defense}"
    liq_cap = max(curve_real_sol, 0.0) * z.max_pct_of_curve_sol / 100 * sol_usd
    min_order = min(z.base_usd, z.max_usd) * 0.5
    if usd > liq_cap:
        if liq_cap < min_order:
            return 0.0, why + f", liquidity cap ${liq_cap:.2f} < minimum ${min_order:.2f}: no trade"
        usd = liq_cap
        why += f", liquidity cap ${liq_cap:.2f}"
    return math.floor(min(usd, z.max_usd) * 100) / 100, why     # round down: never above either cap
