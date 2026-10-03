"""Sniper execution.

PaperExecutor  - fills against the token's live bonding-curve reserves (exact constant-product
                 math incl. our own price impact), plus pump.fun + PumpPortal fees and an
                 adverse latency haircut. Our paper size does not move the real market.
LiveExecutor   - PumpPortal local-transaction API: it builds the tx, we sign locally (the key
                 never leaves this machine) and send through our own RPC.
"""
from __future__ import annotations

import asyncio
import time
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
        # fixed SOL per transaction: priority fee + 5000-lamport base fee. At $5 sizes this is several % of
        # the trade, so paper results must pay it too.
        self.priority = ex.priority_fee_sol

    def _tx_cost(self, priority: float | None) -> float:
        return (self.priority if priority is None else priority) + 0.000005

    async def buy(self, mint: str, curve: Curve, sol: float, priority: float | None = None) -> SniperFill:
        tokens = curve.quote_buy(sol, self.fee) * (1 - self.slip)
        return SniperFill(tokens > 0, sol=sol + self._tx_cost(priority), tokens=tokens,
                          error="" if tokens > 0 else "curve full")

    async def sell(self, mint: str, curve: Curve, tokens: float, priority: float | None = None) -> SniperFill:
        sol = max(curve.quote_sell(tokens, self.fee) * (1 - self.slip) - self._tx_cost(priority), 0.0)
        return SniperFill(True, sol=sol, tokens=tokens)


class LiveExecutor:
    """Live trades via PumpPortal's local-transaction API.

    * pool=auto: PumpPortal routes to the bonding curve or, after graduation, the PumpSwap AMM -
      a curve-only order would fail once a token migrates (intel brief §5).
    * Every attempt asks PumpPortal for a FRESH transaction (= a re-quote). Buys are never retried
      blindly; sells step up slippage (execution.sell_slippage_steps) because being stuck in a
      dumping token costs more than a worse fill.
    * Fills are measured from the confirmed transaction's own pre/post balances, so trades running
      at the same time can't pollute each other, and the ~0.002 SOL token-account rent (returned when
      the account is closed) is kept out of the cost basis.
    * Late landings: if confirmation times out we still look the transaction up, so a fill that
      landed late is booked instead of becoming an untracked position.
    * Nothing here raises: any error comes back as a failed fill.
    """
    URL = "https://pumpportal.fun/api/trade-local"

    def __init__(self, ex, wallet):
        self.ex = ex
        self.wallet = wallet

    def _attempt(self, mint: str, action: str, amount, in_sol: bool, slippage: float,
                 estimate_sol: float = 0.0, priority: float | None = None) -> SniperFill:
        import base64

        import httpx

        from ..wallet import confirm

        w = self.wallet
        priority = self.ex.priority_fee_sol if priority is None else priority
        try:
            r = httpx.post(self.URL, timeout=10, data={
                "publicKey": w.pubkey, "action": action, "mint": mint, "amount": amount,
                "denominatedInSol": "true" if in_sol else "false", "slippage": slippage,
                "priorityFee": priority, "pool": "auto",
            })
            if r.status_code != 200:
                return SniperFill(False, error=f"pumpportal {r.status_code}: {r.text[:200]}")
            sig = w.sign_and_send(base64.b64encode(r.content).decode())
        except Exception as e:                       # incl. preflight/simulation failures from sendTransaction
            return SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        try:
            landed = confirm(sig, timeout_s=30)
        except Exception:
            landed = False
        d = None
        for _ in range(4):                           # the tx can take a moment to be queryable
            try:
                d = w.tx_deltas(sig, mint)
            except Exception:
                d = None
            if d is not None or not landed:
                break
            time.sleep(1.5)
        if d is not None:
            if d["failed"]:
                return SniperFill(False, signature=sig, error="transaction failed on-chain")
            tokens = abs(d["dtok"]) / 1e6           # pump.fun tokens have 6 decimals
            moved = d["dtok"] > 0 if action == "buy" else d["dtok"] < 0
            if not moved or tokens <= 0:
                return SniperFill(False, signature=sig, error="landed but no token change for this wallet")
            sol = abs(d["dsol"]) - d["rent"] if action == "buy" else max(d["dsol"], 0.0)
            return SniperFill(True, sol=max(sol, 0.0), tokens=tokens, signature=sig)
        if not landed:
            return SniperFill(False, signature=sig, error="not confirmed / failed on-chain")
        # confirmed but the transaction itself isn't retrievable: estimate rather than lose the fill
        if action == "buy":
            try:
                tokens = w.token_balance(mint) / 1e6         # we never buy a mint we already hold
            except Exception:
                tokens = 0.0
            if tokens <= 0:
                return SniperFill(False, signature=sig, error="confirmed but token balance not visible yet")
            return SniperFill(True, sol=float(amount), tokens=tokens, signature=sig, error="estimated")
        return SniperFill(True, sol=estimate_sol, tokens=float(amount), signature=sig, error="estimated")

    def _close_if_empty(self, mint: str) -> None:
        if getattr(self.ex, "close_empty_accounts", True):
            try:
                self.wallet.close_empty_token_accounts(mint)    # reclaims ~0.002 SOL rent per account
            except Exception as e:
                print(f"[live] close account for {mint[:6]} failed (harmless): {e}")

    async def buy(self, mint: str, curve: Curve, sol: float, priority: float | None = None) -> SniperFill:
        return await asyncio.to_thread(self._attempt, mint, "buy", sol, True, self.ex.slippage_pct, 0.0, priority)

    async def sell(self, mint: str, curve: Curve, tokens: float, priority: float | None = None) -> SniperFill:
        fee = self.ex.curve_fee_pct + self.ex.platform_fee_pct
        estimate = curve.quote_sell(tokens, fee) if curve is not None else 0.0

        def run() -> SniperFill:
            fill = SniperFill(False, error="no attempt")
            steps = list(self.ex.sell_slippage_steps)
            prios = [priority] * len(steps) if priority is not None else \
                list(self.ex.sell_priority_fee_steps) + [self.ex.sell_priority_fee_steps[-1]] * len(steps)
            for slip, prio in zip(steps, prios):     # each retry: more slippage room AND more priority
                fill = self._attempt(mint, "sell", round(tokens, 6), False, slip, estimate, prio)
                if fill.ok:
                    try:
                        if self.wallet.token_balance(mint) == 0:
                            self._close_if_empty(mint)
                    except Exception:
                        pass
                    return fill
            return fill
        return await asyncio.to_thread(run)
