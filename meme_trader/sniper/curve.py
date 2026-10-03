"""Pump.fun bonding-curve math (constant product on virtual reserves).

Amounts are in UI units (SOL, whole tokens) - the same units PumpPortal reports.
A token starts at 30 virtual SOL / 1.073B virtual tokens, 793.1M of which are sellable
on the curve. The curve "graduates" (migrates to PumpSwap) when those are sold out,
at ~85 real SOL raised.
"""
from __future__ import annotations

from dataclasses import dataclass

INITIAL_V_SOL = 30.0
INITIAL_V_TOKENS = 1_073_000_000.0
CURVE_TOKENS = 793_100_000.0
TOTAL_SUPPLY = 1_000_000_000.0
FINAL_V_TOKENS = INITIAL_V_TOKENS - CURVE_TOKENS


@dataclass
class Curve:
    v_sol: float = INITIAL_V_SOL
    v_tokens: float = INITIAL_V_TOKENS

    @property
    def price(self) -> float:
        """SOL per whole token."""
        return self.v_sol / self.v_tokens

    @property
    def market_cap_sol(self) -> float:
        return self.price * TOTAL_SUPPLY

    @property
    def progress(self) -> float:
        """0..1 share of curve tokens sold (1 = graduation)."""
        return min(max((INITIAL_V_TOKENS - self.v_tokens) / CURVE_TOKENS, 0.0), 1.0)

    @property
    def real_sol(self) -> float:
        return self.v_sol - INITIAL_V_SOL

    def quote_buy(self, sol_in: float, fee_pct: float) -> float:
        """Tokens received for sol_in (fee taken from the SOL going in)."""
        net = sol_in * (1 - fee_pct / 100)
        k = self.v_sol * self.v_tokens
        out = self.v_tokens - k / (self.v_sol + net)
        return min(out, self.v_tokens - FINAL_V_TOKENS)

    def quote_sell(self, tokens_in: float, fee_pct: float) -> float:
        """SOL received for tokens_in (fee taken from the SOL coming out)."""
        k = self.v_sol * self.v_tokens
        gross = self.v_sol - k / (self.v_tokens + tokens_in)
        return gross * (1 - fee_pct / 100)

    def apply(self, sol_delta: float, token_delta: float) -> None:
        """Move reserves by a trade: buy = (+sol, -tokens), sell = (-sol, +tokens)."""
        self.v_sol += sol_delta
        self.v_tokens += token_delta
