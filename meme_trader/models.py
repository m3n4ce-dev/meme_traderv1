"""Data passed between agents."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class Candidate:
    """Scout output: a token worth looking at, with market data from DexScreener."""
    mint: str
    symbol: str = ""
    pair_address: str = ""
    price_usd: float = 0.0
    price_native: float = 0.0          # price in SOL
    liquidity_usd: float = 0.0
    fdv_usd: float = 0.0
    volume_5m_usd: float = 0.0
    volume_1h_usd: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    price_change_5m_pct: float = 0.0
    price_change_1h_pct: float = 0.0
    pair_created_at: float = 0.0       # unix seconds
    source: str = ""

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.pair_created_at) / 60 if self.pair_created_at else 0.0


@dataclass
class SafetyReport:
    mint: str
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why it failed (empty if passed)
    raw: dict = field(default_factory=dict)


@dataclass
class Signal:
    """Analyst output."""
    mint: str
    score: float
    passed: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Order:
    mint: str
    side: str            # buy | sell
    sol_amount: float = 0.0      # buy: SOL in
    token_amount: int = 0        # sell: raw token units in
    decimals: int = 6
    symbol: str = ""
    ref_price_sol: float = 0.0   # market price (SOL per whole token) when the order was made
    reason: str = ""


@dataclass
class Fill:
    order: Order
    ok: bool
    sol_delta: float = 0.0       # +SOL received, -SOL spent
    token_delta: int = 0         # raw units
    price_sol: float = 0.0       # SOL per whole token
    signature: str = ""
    error: str = ""


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_sol: float
    initial_tokens: int          # raw units at entry
    tokens: int                  # raw units remaining
    cost_sol: float
    decimals: int = 6
    opened_at: float = field(default_factory=time.time)
    peak_price_sol: float = 0.0
    tp_levels_hit: int = 0
    realized_sol: float = 0.0
