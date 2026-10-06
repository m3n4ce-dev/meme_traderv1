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
    sol: float = 0.0       # buy: SOL spent (cost basis, excl. rent). sell: net SOL received - can be NEGATIVE
                           # when fees exceed what a near-worthless remainder fetched
    tokens: float = 0.0    # tokens received (buy) or sold (sell), always positive
    signature: str = ""
    error: str = ""
    rent: float = 0.0            # buy: refundable SOL locked in a newly created token account
    rent_reclaimed: float = 0.0  # sell: SOL back from closing the emptied token account (net of its fee)
    fees_lost: float = 0.0       # SOL burned by attempts that landed on-chain but failed
    unknown: bool = False        # sent, but whether it landed isn't known yet (see Engine.unresolved)
    blockhash: str = ""          # the signed transaction's recent blockhash: proves when it can no longer land
    expired: bool = False        # unknown, with proof it never landed: not in the chain's history and past expiry
    landed: bool = False         # unknown amounts, but the chain shows it landed: keep waiting, never re-send
    timing: dict | None = None   # live: seconds to build / send / confirm, landing slot and block time

    @property
    def price(self) -> float:
        return self.sol / self.tokens if self.tokens else 0.0


UNKNOWN_EXPIRES_S = 150    # older than any blockhash lives (~60-90 s): with no blockhash recorded, the age proves it


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

    async def sell(self, mint: str, curve: Curve, tokens: float, priority: float | None = None,
                   steps: list | None = None) -> SniperFill:
        # not clamped at 0: selling a near-worthless remainder can cost more in fees than it fetches, as it does live
        sol = curve.quote_sell(tokens, self.fee) * (1 - self.slip) - self._tx_cost(priority)
        return SniperFill(True, sol=sol, tokens=tokens)


class LiveExecutor:
    """Live trades via PumpPortal's local-transaction API.

    * pool=auto: PumpPortal routes to the bonding curve or, after graduation, the PumpSwap AMM -
      a curve-only order would fail once a token migrates (intel brief §5).
    * The transaction PumpPortal builds is checked before we sign it: this wallet must be the fee payer,
      and a simulation must show it can't take more of our SOL than the order allows (verify_tx).
    * We sign locally first, so the signature is known before sending. If sending or confirming can't
      tell us whether it landed, the fill comes back `unknown` with that signature: the engine waits
      for the chain (resolve) instead of sending a fresh order that could fill twice.
    * Sells step up slippage and priority (execution.sell_slippage_steps) only after an attempt
      definitely did NOT land. Failed attempts that landed still cost fees: reported as fees_lost.
    * Fills are measured from the confirmed transaction's own pre/post balances, so trades running
      at the same time can't pollute each other. The ~0.002 SOL token-account rent is reported
      separately (refundable, not trading cost), and so is the rent reclaimed when we close it.
    * Nothing here raises: any error comes back as a failed (or unknown) fill.
    """
    URL = "https://pumpportal.fun/api/trade-local"

    def __init__(self, ex, wallet):
        self.ex = ex
        self.wallet = wallet

    def _check_payload(self, tx_b64: str, action: str, amount, in_sol: bool, slippage: float,
                       priority: float) -> str:
        """'' if the transaction may be signed, else why not. Bounds how much SOL it can move out of
        our wallet: a buy at most its amount plus slippage, fees and rent; a sell only fees."""
        if not getattr(self.ex, "verify_tx", True):
            return ""
        try:
            change = self.wallet.simulate_sol_change(tx_b64)
        except Exception as e:
            return f"pre-sign check failed: {e}"[:240]
        overhead = priority + 0.00001 + 0.0025         # base fees + one token-account rent + margin
        limit = (float(amount) * (1 + slippage / 100) * 1.02 if action == "buy" and in_sol else 0.0) + overhead
        if -change > limit:
            return f"refused to sign: transaction would move {-change:.6f} SOL out (allowed {limit:.6f})"
        return ""

    def _fill_from(self, d: dict, action: str, sig: str) -> SniperFill:
        """A transaction we found on-chain -> fill. d: Wallet.tx_deltas()."""
        if d["failed"]:
            return SniperFill(False, signature=sig, error="transaction failed on-chain", fees_lost=max(-d["dsol"], 0.0))
        tokens = abs(d["dtok"]) / 1e6               # pump.fun tokens have 6 decimals
        moved = d["dtok"] > 0 if action == "buy" else d["dtok"] < 0
        if not moved or tokens <= 0:
            return SniperFill(False, signature=sig, error="landed but no token change for this wallet",
                              fees_lost=max(-d["dsol"], 0.0))
        if action == "buy":
            return SniperFill(True, sol=max(abs(d["dsol"]) - d["rent"], 0.0), tokens=tokens, signature=sig,
                              rent=d["rent"])
        return SniperFill(True, sol=d["dsol"], tokens=tokens, signature=sig)   # may be < 0: fees > proceeds

    def _attempt(self, mint: str, action: str, amount, in_sol: bool, slippage: float,
                 estimate_sol: float = 0.0, priority: float | None = None) -> SniperFill:
        import base64

        import httpx

        from ..wallet import confirm

        w = self.wallet
        priority = self.ex.priority_fee_sol if priority is None else priority
        t0 = time.time()
        timing: dict = {"started": t0}
        try:
            r = httpx.post(self.URL, timeout=10, data={
                "publicKey": w.pubkey, "action": action, "mint": mint, "amount": amount,
                "denominatedInSol": "true" if in_sol else "false", "slippage": slippage,
                "priorityFee": priority, "pool": "auto",
            })
            if r.status_code != 200:
                return SniperFill(False, error=f"pumpportal {r.status_code}: {r.text[:200]}")
            tx_b64 = base64.b64encode(r.content).decode()
            why = self._check_payload(tx_b64, action, amount, in_sol, slippage, priority)
            if why:
                return SniperFill(False, error=why)
            raw, sig = w.sign(tx_b64)
            try:
                bh = w.blockhash_of(raw)
            except Exception:
                bh = ""
            timing["build_s"] = round(time.time() - t0, 3)
        except Exception as e:                       # nothing was sent
            return SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        try:
            w.send(raw)
            timing["sent"] = time.time()
        except RuntimeError as e:                    # the RPC answered with an error (e.g. preflight): not sent
            if str(e).startswith("RPC sendTransaction"):
                return SniperFill(False, signature=sig, error=str(e)[:240])
            return SniperFill(False, unknown=True, signature=sig, blockhash=bh, error=f"send: {e}"[:240])
        except Exception as e:                       # timeout / connection: it may or may not be out there
            return SniperFill(False, unknown=True, signature=sig, blockhash=bh, error=f"send: {type(e).__name__}: {e}"[:240])
        try:
            landed = confirm(sig, timeout_s=30)
        except Exception:
            landed = False
        timing["confirm_s"] = round(time.time() - timing.get("sent", t0), 3)
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
            fill = self._fill_from(d, action, sig)
            timing.update(slot=d.get("slot"), block_time=d.get("block_time"))
            if d.get("block_time") and timing.get("sent"):   # landing measured by the chain, not our polling
                timing["landed_after_send_s"] = round(d["block_time"] - timing["sent"], 1)
            fill.timing = timing
            return fill
        if not landed:                               # not confirmed and not found: we don't know yet
            return SniperFill(False, unknown=True, signature=sig, blockhash=bh, error="not confirmed yet - outcome unknown")
        # confirmed but the transaction itself isn't retrievable: estimate rather than lose the fill
        if action == "buy":
            try:
                tokens = w.token_balance(mint) / 1e6         # we never buy a mint we already hold
            except Exception:
                tokens = 0.0
            if tokens <= 0:
                return SniperFill(False, unknown=True, landed=True, signature=sig, blockhash=bh,
                                  error="confirmed but token balance not visible yet")
            return SniperFill(True, sol=float(amount), tokens=tokens, signature=sig, error="estimated")
        return SniperFill(True, sol=estimate_sol, tokens=float(amount), signature=sig, error="estimated")

    def _close_if_empty(self, mint: str) -> float:
        """Close the emptied token account; SOL actually reclaimed (only once its close is confirmed)."""
        if not getattr(self.ex, "close_empty_accounts", True):
            return 0.0
        from ..wallet import confirm

        try:
            sig, held = self.wallet.close_empty_token_accounts(mint)
            if sig and held > 0 and confirm(sig, timeout_s=20):
                return max(held - 0.000005, 0.0)
        except Exception as e:
            print(f"[live] close account for {mint[:6]} failed (harmless, rent stays locked): {e}")
        return 0.0

    async def buy(self, mint: str, curve: Curve, sol: float, priority: float | None = None) -> SniperFill:
        return await asyncio.to_thread(self._attempt, mint, "buy", sol, True, self.ex.slippage_pct, 0.0, priority)

    async def sell(self, mint: str, curve: Curve, tokens: float, priority: float | None = None,
                   steps: list | None = None) -> SniperFill:
        fee = self.ex.curve_fee_pct + self.ex.platform_fee_pct
        estimate = curve.quote_sell(tokens, fee) if curve is not None else 0.0

        def run() -> SniperFill:
            fill = SniperFill(False, error="no attempt")
            lost = 0.0
            slips = list(steps or self.ex.sell_slippage_steps)
            prios = [priority] * len(slips) if priority is not None else \
                list(self.ex.sell_priority_fee_steps) + [self.ex.sell_priority_fee_steps[-1]] * len(slips)
            for slip, prio in zip(slips, prios):     # each retry: more slippage room AND more priority
                fill = self._attempt(mint, "sell", round(tokens, 6), False, slip, estimate, prio)
                lost += fill.fees_lost
                if fill.unknown:                     # a fresh sell now could sell twice: stop and resolve
                    break
                if fill.ok:
                    try:
                        if self.wallet.token_balance(mint) == 0:
                            fill.rent_reclaimed = self._close_if_empty(mint)
                    except Exception:
                        pass
                    break
            fill.fees_lost = lost
            return fill
        return await asyncio.to_thread(run)

    async def resolve(self, sig: str, mint: str, action: str, blockhash: str = "", age_s: float = 0.0) -> SniperFill:
        """What happened to a sent transaction whose outcome was unknown: a fill, a failure (with its fees), or still
        unknown. "Never landed" (expired=True) needs proof: the chain's signature history (searched, not just recent
        status) doesn't have it AND it can't land anymore (its blockhash is no longer valid; without a recorded
        blockhash, older than any blockhash lives). An RPC error, a missing transaction body or a still-valid
        blockhash prove nothing: the order stays unresolved, so its cash stays reserved and no sell is re-sent."""
        def unknown(why: str, **kw) -> SniperFill:
            return SniperFill(False, unknown=True, signature=sig, blockhash=blockhash, error=why[:240], **kw)

        def run() -> SniperFill:
            try:
                d = self.wallet.tx_deltas(sig, mint)
            except Exception as e:
                return unknown(f"transaction lookup failed: {type(e).__name__}: {e}")
            if d is None:
                try:
                    st = self.wallet.signature_status(sig)
                except Exception as e:
                    return unknown(f"status lookup failed: {type(e).__name__}: {e}")
                if st is not None:
                    if st.get("err"):                # landed and failed: no tokens moved, its fee is gone
                        return SniperFill(False, signature=sig, error="failed on-chain (details not retrievable yet)",
                                          fees_lost=self.ex.priority_fee_sol + 0.000005)
                    return unknown("landed; its amounts aren't retrievable yet", landed=True)
                if blockhash:
                    try:
                        valid = self.wallet.blockhash_valid(blockhash)
                    except Exception as e:
                        return unknown(f"blockhash check failed: {type(e).__name__}: {e}")
                    if valid:
                        return unknown("not on-chain yet; it can still land")
                    return unknown("not in the chain's history and its blockhash has expired: it never landed", expired=True)
                if age_s >= UNKNOWN_EXPIRES_S:
                    return unknown("not in the chain's history, long past any blockhash's life: it never landed", expired=True)
                return unknown("not on-chain yet")
            fill = self._fill_from(d, action, sig)
            if fill.ok and action == "sell":
                try:
                    if self.wallet.token_balance(mint) == 0:
                        fill.rent_reclaimed = self._close_if_empty(mint)
                except Exception:
                    pass
            return fill
        return await asyncio.to_thread(run)
