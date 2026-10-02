"""Sniper execution.

PaperExecutor  - fills against the token's live bonding-curve reserves (exact constant-product
                 math incl. our own price impact), plus pump.fun + PumpPortal fees and an
                 adverse latency haircut. Our paper size does not move the real market.
LiveExecutor   - PumpPortal local-transaction API: it builds the tx, we sign locally (the key
                 never leaves this machine) and send through our own RPC.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .curve import Curve


@dataclass
class SniperFill:
    ok: bool
    sol: float = 0.0       # SOL spent (buy) or received (sell), always positive
    tokens: float = 0.0    # tokens received (buy) or sold (sell), always positive
    signature: str = ""
    error: str = ""

    @property
    def price(self) -> float:
        return self.sol / self.tokens if self.tokens else 0.0


class PaperExecutor:
    def __init__(self, ex):
        self.fee = ex.curve_fee_pct + ex.platform_fee_pct
        self.slip = ex.paper_latency_slippage_pct / 100

    async def buy(self, mint: str, curve: Curve, sol: float) -> SniperFill:
        tokens = curve.quote_buy(sol, self.fee) * (1 - self.slip)
        return SniperFill(tokens > 0, sol=sol, tokens=tokens, error="" if tokens > 0 else "curve full")

    async def sell(self, mint: str, curve: Curve, tokens: float) -> SniperFill:
        sol = curve.quote_sell(tokens, self.fee) * (1 - self.slip)
        return SniperFill(True, sol=sol, tokens=tokens)


class LiveExecutor:
    URL = "https://pumpportal.fun/api/trade-local"

    def __init__(self, ex, wallet):
        self.ex = ex
        self.wallet = wallet

    async def _trade(self, mint: str, action: str, amount, in_sol: bool) -> SniperFill:
        return await asyncio.to_thread(self._trade_sync, mint, action, amount, in_sol)

    def _trade_sync(self, mint: str, action: str, amount, in_sol: bool) -> SniperFill:
        import base64

        import httpx

        from ..wallet import confirm

        w = self.wallet
        sol0, tok0 = w.sol_balance(), w.token_balance(mint)
        r = httpx.post(self.URL, timeout=10, data={
            "publicKey": w.pubkey, "action": action, "mint": mint, "amount": amount,
            "denominatedInSol": "true" if in_sol else "false", "slippage": self.ex.slippage_pct,
            "priorityFee": self.ex.priority_fee_sol, "pool": "pump",
        })
        if r.status_code != 200:
            return SniperFill(False, error=f"pumpportal {r.status_code}: {r.text[:200]}")
        sig = w.sign_and_send(base64.b64encode(r.content).decode())
        if not confirm(sig, timeout_s=30):
            return SniperFill(False, signature=sig, error="not confirmed / failed on-chain")
        # Measure what actually happened (fees, slippage) from balances. Token amounts in UI units.
        dsol = w.sol_balance() - sol0
        dtok = (w.token_balance(mint) - tok0) / 1e6   # pump.fun tokens have 6 decimals
        return SniperFill(True, sol=abs(dsol), tokens=abs(dtok), signature=sig)

    async def buy(self, mint: str, curve: Curve, sol: float) -> SniperFill:
        return await self._trade(mint, "buy", sol, True)

    async def sell(self, mint: str, curve: Curve, tokens: float) -> SniperFill:
        return await self._trade(mint, "sell", round(tokens, 6), False)
