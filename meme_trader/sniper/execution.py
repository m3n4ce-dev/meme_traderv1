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
    """Live trades via PumpPortal's local-transaction API.

    * pool=auto: PumpPortal routes to the bonding curve or, after graduation, the PumpSwap AMM -
      a curve-only order would fail once a token migrates (intel brief §5).
    * Every attempt asks PumpPortal for a FRESH transaction (= a re-quote). Buys are never retried
      blindly; sells step up slippage (execution.sell_slippage_steps) because being stuck in a
      dumping token costs more than a worse fill.
    * Late landings: if confirmation times out we still re-read balances, so a fill that landed
      late is booked instead of becoming an untracked position.
    """
    URL = "https://pumpportal.fun/api/trade-local"

    def __init__(self, ex, wallet):
        self.ex = ex
        self.wallet = wallet

    def _attempt(self, mint: str, action: str, amount, in_sol: bool, slippage: float) -> SniperFill:
        import base64

        import httpx

        from ..wallet import confirm

        w = self.wallet
        sol0, tok0 = w.sol_balance(), w.token_balance(mint)
        r = httpx.post(self.URL, timeout=10, data={
            "publicKey": w.pubkey, "action": action, "mint": mint, "amount": amount,
            "denominatedInSol": "true" if in_sol else "false", "slippage": slippage,
            "priorityFee": self.ex.priority_fee_sol, "pool": "auto",
        })
        if r.status_code != 200:
            return SniperFill(False, error=f"pumpportal {r.status_code}: {r.text[:200]}")
        sig = w.sign_and_send(base64.b64encode(r.content).decode())
        landed = confirm(sig, timeout_s=30)
        # Measure what actually happened (fees, slippage) from balances. Token amounts in UI units.
        dsol = w.sol_balance() - sol0
        dtok = (w.token_balance(mint) - tok0) / 1e6   # pump.fun tokens have 6 decimals
        moved = dtok > 0 if action == "buy" else dtok < 0
        if not landed and not moved:
            return SniperFill(False, signature=sig, error="not confirmed / failed on-chain")
        return SniperFill(True, sol=abs(dsol), tokens=abs(dtok), signature=sig)

    def _close_if_empty(self, mint: str) -> None:
        if getattr(self.ex, "close_empty_accounts", True):
            try:
                self.wallet.close_empty_token_accounts(mint)    # reclaims ~0.002 SOL rent per account
            except Exception as e:
                print(f"[live] close account for {mint[:6]} failed (harmless): {e}")

    async def buy(self, mint: str, curve: Curve, sol: float) -> SniperFill:
        return await asyncio.to_thread(self._attempt, mint, "buy", sol, True, self.ex.slippage_pct)

    async def sell(self, mint: str, curve: Curve, tokens: float) -> SniperFill:
        def run() -> SniperFill:
            fill = SniperFill(False, error="no attempt")
            for slip in self.ex.sell_slippage_steps:
                fill = self._attempt(mint, "sell", round(tokens, 6), False, slip)
                if fill.ok:
                    if self.wallet.token_balance(mint) == 0:
                        self._close_if_empty(mint)
                    return fill
            return fill
        return await asyncio.to_thread(run)
