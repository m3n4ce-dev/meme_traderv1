"""One feature vector per token, at one moment - shared by the probability model, the token detail
view and the AI desk, so they all see the same numbers.

The set follows the published evidence on what predicts pump.fun outcomes:
  * SOL locked in the curve is the key state variable (arXiv 2602.14860)
  * first-minutes trading features - buy/sell counts and values, unique buyers/sellers, price
    volatility, timing - predict rugs with AUC-PR ~0.76-0.80 (arXiv 2608.20271)
  * social presence: Telegram 8.9x, all three links 17.4x graduation lift (arXiv 2607.02823)
  * broad retail participation raises graduation odds (arXiv 2602.14860)
One pass over the last 60 s of trades; everything is a plain float.
"""
from __future__ import annotations

import math

FEATURES = [
    "age_min", "curve_pct", "real_sol", "log_mcap_sol", "log_buyers", "log_holders",
    "buyers_20s", "buyers_60s", "sellers_60s", "net_flow_20s", "net_flow_60s", "vol_60s",
    "buy_sell_ratio_60s", "avg_buy_sol_60s", "retail_share_60s", "price_change_60s",
    "volatility_60s", "near_high", "drawdown_from_high", "dev_initial_pct", "dev_holds_pct",
    "dev_sold", "bundle_pct", "sniper_pct", "insider_pct", "early_sold_ratio", "top10_pct",
    "fees_paid_sol", "has_twitter", "has_telegram", "has_website", "socials_count",
    "smart_wallets", "creator_launches", "symbol_dupes", "cluster_pct",
]


def _log1p(x: float) -> float:
    return math.log1p(max(x, 0.0))


def extract(s, now: float, ctx: dict | None = None) -> dict[str, float]:
    """s: TokenState. ctx: creator_launches, symbol_dupes (from the engine's counters)."""
    ctx = ctx or {}
    buys60 = sells60 = buys20 = 0
    vbuy60 = vsell60 = vbuy20 = vsell20 = 0.0
    buyers60: set = set()
    buyers20: set = set()
    sellers60: set = set()
    small60 = 0
    prices: list[float] = []
    for ts, price, side, sol, trader in reversed(s.trades):
        if ts < now - 60:
            break
        prices.append(price)
        recent = ts >= now - 20
        if side == "buy":
            buys60 += 1
            vbuy60 += sol
            buyers60.add(trader)
            small60 += sol < 0.5
            if recent:
                buys20 += 1
                vbuy20 += sol
                buyers20.add(trader)
        else:
            sells60 += 1
            vsell60 += sol
            sellers60.add(trader)
            if recent:
                vsell20 += sol
    prices.reverse()
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 if rets else 0.0
    price = s.curve.price
    change = math.log(price / prices[0]) if prices and prices[0] > 0 and price > 0 else 0.0
    L = s.launch
    tw, tg, web = (bool(L and L.twitter), bool(L and L.telegram), bool(L and L.website))
    smart = len({x.author for x in s.socials if x.source == "wallet"})
    return {
        "age_min": s.age(now) / 60,
        "curve_pct": s.curve.progress * 100,
        "real_sol": s.curve.real_sol,
        "log_mcap_sol": _log1p(s.curve.market_cap_sol),
        "log_buyers": _log1p(len(s.buyers)),
        "log_holders": _log1p(len(s.holders)),
        "buyers_20s": float(len(buyers20)),
        "buyers_60s": float(len(buyers60)),
        "sellers_60s": float(len(sellers60)),
        "net_flow_20s": vbuy20 - vsell20,
        "net_flow_60s": vbuy60 - vsell60,
        "vol_60s": vbuy60 + vsell60,
        "buy_sell_ratio_60s": buys60 / max(sells60, 1),
        "avg_buy_sol_60s": vbuy60 / buys60 if buys60 else 0.0,
        "retail_share_60s": small60 / buys60 if buys60 else 0.0,
        "price_change_60s": change,
        "volatility_60s": vol,
        "near_high": price / s.peak_price if s.peak_price else 1.0,
        "drawdown_from_high": 1 - price / s.peak_price if s.peak_price else 0.0,
        "dev_initial_pct": s.dev_initial_pct(),
        "dev_holds_pct": s.dev_pct(),
        "dev_sold": 1.0 if s.dev_sold > 0 else 0.0,
        "bundle_pct": s.bundle_pct(),
        "sniper_pct": s.sniper_pct(),
        "insider_pct": s.insider_pct(),
        "early_sold_ratio": s.early_sold_ratio(),
        "top10_pct": s.top_holders_pct(10),
        "fees_paid_sol": s.fees_paid_sol(),
        "has_twitter": float(tw),
        "has_telegram": float(tg),
        "has_website": float(web),
        "socials_count": float(tw + tg + web),
        "smart_wallets": float(smart),
        "creator_launches": float(ctx.get("creator_launches", 0)),
        "symbol_dupes": float(ctx.get("symbol_dupes", 0)),
        "cluster_pct": float(s.cluster["pct"]) if s.cluster else 0.0,
    }


def social_strength(s) -> float:
    """0..1, weighted by the survival-analysis evidence: Telegram carries most of the lift."""
    L = s.launch
    if not L:
        return 0.0
    return 0.5 * bool(L.telegram) + 0.25 * bool(L.twitter) + 0.25 * bool(L.website)
