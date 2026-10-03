"""Callout agent: picks coins worth calling out, holds the platform's $1 minimum, writes a factual
callout card, and tracks every call's outcome.

How pump.fun callouts work (owner, 2026-10-03): you can only call out a coin you bought and hold
at least $1 of; Callout Rewards pay on the volume your calls attract; one callout per 2 minutes.
So the business is picking coins people will click AND that hold up - the position itself stays
at the $1 minimum and is not traded against the followers a call brings in.

Posting: no public pump.fun callout API is known to us, so the dashboard shows each card with
Copy + "Open on pump.fun" (two clicks). Optional Telegram posting mirrors the older callout bot.
Card text is built from measured numbers only - no "window closes fast" urgency, no promises.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

RED_FLAGS = ("dev sold", "dev bought", "bundle", "early buyers dumped", "serial deployer", "copycat", "insider cluster")


def is_red_flag(note: str) -> bool:
    return note.startswith(RED_FLAGS)


@dataclass
class Callout:
    mint: str
    symbol: str
    ts: float
    mcap_sol: float
    price: float
    text: str
    score: float
    posted: str = ""                   # "" | telegram | manual
    outcomes: dict = field(default_factory=dict)   # {"5m": pct, "1h": pct, "peak": pct}
    peak: float = 0.0


def eligible(s, now: float, c, red_flag_ctx: dict) -> tuple[bool, float, str]:
    """c: params.sniper.callouts. Returns (ok, clickability score 0-100, reason)."""
    age, prog = s.age(now), s.curve.progress * 100
    # cheap checks first: this runs over every live token on each scan
    if not (c.min_age_s <= age <= c.max_age_s):
        return False, 0, "age"
    if not (c.min_curve_pct <= prog <= c.max_curve_pct) or s.migrated:
        return False, 0, "curve"
    if len(s.holders) < c.min_holders:
        return False, 0, "holders"
    if s.dev_sold:
        return False, 0, "dev sold"
    if s.bundle_pct() > red_flag_ctx["max_bundle_pct"] or s.early_sold_ratio() > red_flag_ctx["max_early_sold_ratio"]:
        return False, 0, "insider pattern"
    if red_flag_ctx["creator_launches"] > red_flag_ctx["max_creator_launches_24h"]:
        return False, 0, "serial deployer"
    if s.cluster and s.cluster["pct"] > red_flag_ctx.get("max_cluster_pct", 100):
        return False, 0, "insider cluster"
    rate = s.buyers_in(now, 60)
    flow = s.net_flow_sol(now, 60)
    if rate < c.min_new_buyers_per_min or flow <= 0:
        return False, 0, "momentum"
    if s.top_holders_pct(10) > c.max_top10_pct:
        return False, 0, "concentrated"

    def cap(x, full):
        return max(0.0, min(x / full, 1.0))
    smart = len({x.author for x in s.socials if x.source == "wallet"})
    near_high = s.curve.price / s.peak_price if s.peak_price else 0
    score = 100 * (0.35 * cap(rate, 40) + 0.2 * cap(flow, 5) + 0.2 * cap(prog, 80)
                   + 0.15 * cap(smart, 2) + 0.1 * cap(near_high - 0.8, 0.2))
    return score >= c.min_score, round(score, 1), "ok"


def compose(s, now: float, sol_usd: float) -> str:
    """Factual card. Every number is measured; nothing is a prediction."""
    smart = len({x.author for x in s.socials if x.source == "wallet"})
    mcap_usd = s.market_cap_sol * sol_usd
    parts = [
        f"{s.symbol}: {len(s.holders)} holders, +{s.buyers_in(now, 60)} new buyers in the last minute",
        f"curve {s.curve.progress:.0%} filled ({s.curve.real_sol:.1f} SOL in), MC ${mcap_usd / 1000:.1f}K",
        f"{s.age(now) / 60:.0f} min old",
        f"top 10 hold {s.top_holders_pct(10):.0f}%, dev {s.dev_pct():.1f}%",
    ]
    if smart:
        parts.append(f"{smart} tracked profitable wallet{'s' if smart > 1 else ''} in")
    if s.launch and (s.launch.twitter or s.launch.telegram):
        parts.append("socials linked")
    return ". ".join(parts) + ". Data, not advice; I hold a $1 callout position."


class CalloutBook:
    def __init__(self):
        self.calls: list[Callout] = []
        self.called: set[str] = set()
        self.last_ts = -1e12

    def add(self, c: Callout) -> None:
        self.calls.append(c)
        self.called.add(c.mint)
        self.last_ts = c.ts

    def settle(self, now: float, price_of) -> None:
        for c in self.calls[-200:]:
            p = price_of(c.mint)
            if not p or not c.price:
                continue
            c.peak = max(c.peak, p)
            c.outcomes["peak"] = (c.peak / c.price - 1) * 100
            for label, secs in (("5m", 300), ("1h", 3600)):
                if label not in c.outcomes and now - c.ts >= secs:
                    c.outcomes[label] = (p / c.price - 1) * 100

    def stats(self) -> dict:
        five = [c.outcomes["5m"] for c in self.calls if "5m" in c.outcomes]
        peaks = [c.outcomes.get("peak", 0) for c in self.calls]
        return {"calls": len(self.calls),
                "settled_5m": len(five),
                "up_after_5m": sum(1 for x in five if x > 0) / len(five) if five else 0.0,
                "avg_5m_pct": sum(five) / len(five) if five else 0.0,
                "avg_peak_pct": sum(peaks) / len(peaks) if peaks else 0.0}


async def post_telegram(text: str, mint: str) -> bool:
    """Optional: post the card to your Telegram channel (TELEGRAM_BOT_TOKEN + TELEGRAM_CHANNEL_ID)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHANNEL_ID")
    if not (token and chat):
        return False
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            r = await s.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": f"{text}\nhttps://pump.fun/coin/{mint}"})
            return r.status == 200
    except Exception:
        return False
