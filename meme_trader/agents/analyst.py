"""Analyst agent: scores momentum/flow 0-100. Deterministic rules for now;
an LLM narrative/social scorer can be added as another weighted component later."""
from __future__ import annotations

from ..models import Candidate, Signal


def evaluate(c: Candidate, a) -> Signal:
    """a: params.analyst"""
    notes: list[str] = []
    hard_fail = False

    ratio = c.buys_5m / max(c.sells_5m, 1)
    if c.volume_5m_usd < a.min_volume_5m_usd:
        notes.append(f"5m volume ${c.volume_5m_usd:,.0f} < ${a.min_volume_5m_usd:,.0f}")
        hard_fail = True
    if ratio < a.min_buy_sell_ratio_5m:
        notes.append(f"buy/sell {ratio:.2f} < {a.min_buy_sell_ratio_5m}")
        hard_fail = True
    if c.price_change_5m_pct < a.min_price_change_5m_pct:
        notes.append(f"5m change {c.price_change_5m_pct}% < {a.min_price_change_5m_pct}%")
        hard_fail = True
    if c.price_change_1h_pct > a.max_price_change_1h_pct:
        notes.append(f"1h change {c.price_change_1h_pct}% > {a.max_price_change_1h_pct}% (overextended)")
        hard_fail = True

    # Each component scaled to 0..1 relative to its threshold, capped.
    def scaled(x: float, threshold: float, cap: float = 3.0) -> float:
        return min(x / threshold, cap) / cap if threshold > 0 else 0.0

    # Liquidity depth vs valuation: liquidity >= 20% of FDV scores full marks.
    depth = min(c.liquidity_usd / c.fdv_usd / 0.2, 1.0) if c.fdv_usd else 0.0
    score = 100 * (
        0.35 * scaled(c.volume_5m_usd, a.min_volume_5m_usd)
        + 0.30 * scaled(ratio, a.min_buy_sell_ratio_5m)
        + 0.20 * scaled(max(c.price_change_5m_pct, 0), a.min_price_change_5m_pct)
        + 0.15 * depth
    )
    score = round(score, 1)
    if score < a.min_score:
        notes.append(f"score {score} < {a.min_score}")
    return Signal(c.mint, score, not hard_fail and score >= a.min_score, notes)
