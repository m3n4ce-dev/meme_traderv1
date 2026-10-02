"""Position monitor agent: stop loss, take-profit ladder, trailing stop, time stop."""
from __future__ import annotations

import time

from ..models import Order, Position

TP_REASON = "take profit"


def evaluate(pos: Position, price_sol: float, x, now: float | None = None) -> Order | None:
    """x: params.exits. Updates pos.peak_price_sol. Returns at most one sell order.
    The caller advances pos.tp_levels_hit only after a take-profit sell actually fills."""
    now = now or time.time()
    pos.peak_price_sol = max(pos.peak_price_sol, price_sol)
    gain = (price_sol / pos.entry_price_sol - 1) * 100

    def sell(units: int, reason: str) -> Order:
        return Order(pos.mint, "sell", token_amount=min(units, pos.tokens), decimals=pos.decimals,
                     symbol=pos.symbol, ref_price_sol=price_sol, reason=reason)

    if gain <= -x.stop_loss_pct:
        return sell(pos.tokens, f"stop loss {gain:.1f}%")
    if pos.tp_levels_hit > 0:
        drop = (1 - price_sol / pos.peak_price_sol) * 100
        if drop >= x.trailing_stop_pct:
            return sell(pos.tokens, f"trailing stop -{drop:.1f}% from peak")
    if (now - pos.opened_at) / 60 >= x.max_hold_minutes:
        return sell(pos.tokens, "time stop")
    ladder = x.take_profit_ladder
    if pos.tp_levels_hit < len(ladder) and gain >= ladder[pos.tp_levels_hit]["gain_pct"]:
        step = ladder[pos.tp_levels_hit]
        return sell(int(pos.initial_tokens * step["sell_fraction"]), f"{TP_REASON} +{gain:.1f}%")
    return None
