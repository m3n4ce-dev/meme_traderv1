"""Executor agent: pre-trade quote checks (price impact, honeypot round-trip) and fills.

paper: simulated fills. Uses live Jupiter quotes when JUPITER_API_KEY is set, otherwise
       the DexScreener price plus params.execution.paper_fee_pct.
live:  Jupiter quote -> swap tx -> sign locally -> send via RPC -> confirm -> measure balances.
"""
from __future__ import annotations

from ..clients import jupiter
from ..journal import record
from ..models import SOL_MINT, Fill, Order

LAMPORTS = 1_000_000_000


class Executor:
    def __init__(self, params, wallet=None):
        self.p = params
        self.e = params.execution
        self.wallet = wallet
        if params.mode == "live" and (wallet is None or not jupiter.has_key()):
            raise RuntimeError("live mode needs a wallet and JUPITER_API_KEY")
        if params.mode == "live" and not getattr(params, "allow_unproven_live_executor", False):
            # Two external reviews (2026-10-06): this executor confirms and measures balances, but has none of the
            # pump.fun engine's unresolved-order lifecycle (signature history, blockhash expiry, no re-send while
            # unknown). Until it shares that lifecycle, it doesn't trade real money.
            raise RuntimeError("the DexScreener bot's live executor is blocked until it shares the pump.fun engine's "
                               "unresolved-order handling (see docs/BUILD_LOG.md #62). Paper mode works.")

    # ---- pre-trade checks -------------------------------------------------
    def _quote(self, order: Order) -> dict:
        if order.side == "buy":
            return jupiter.quote(SOL_MINT, order.mint, int(order.sol_amount * LAMPORTS), self.e.slippage_bps)
        return jupiter.quote(order.mint, SOL_MINT, order.token_amount, self.e.slippage_bps)

    def precheck_buy(self, order: Order) -> tuple[dict | None, str]:
        """Returns (buy quote, '') or (None, rejection reason). No-op without a Jupiter key (paper only)."""
        if not jupiter.has_key():
            return None, ""
        q = self._quote(order)
        impact = float(q.get("priceImpactPct") or 0) * 100   # API returns a fraction
        if impact > self.e.max_price_impact_pct:
            return None, f"price impact {impact:.2f}% > {self.e.max_price_impact_pct}%"
        back = jupiter.quote(order.mint, SOL_MINT, int(q["outAmount"]), self.e.slippage_bps)
        rt_loss = (1 - int(back["outAmount"]) / int(q["inAmount"])) * 100
        if rt_loss > self.p.safety.max_roundtrip_loss_pct:
            return None, f"round-trip loss {rt_loss:.1f}% > {self.p.safety.max_roundtrip_loss_pct}% (honeypot/tax?)"
        return q, ""

    # ---- execution ----------------------------------------------------------
    def execute(self, order: Order, quote: dict | None = None) -> Fill:
        try:
            fill = self._live(order, quote) if self.p.mode == "live" else self._paper(order, quote)
        except Exception as e:
            fill = Fill(order, ok=False, error=str(e))
        record("executor", "fill" if fill.ok else "fill_failed", mode=self.p.mode, fill=fill)
        return fill

    def _paper(self, order: Order, quote: dict | None) -> Fill:
        unit = 10 ** order.decimals
        if quote is None and jupiter.has_key():
            quote = self._quote(order)
        if quote is not None:
            in_amt, out_amt = int(quote["inAmount"]), int(quote["outAmount"])
            if order.side == "buy":
                return Fill(order, True, sol_delta=-in_amt / LAMPORTS, token_delta=out_amt,
                            price_sol=(in_amt / LAMPORTS) / (out_amt / unit))
            return Fill(order, True, sol_delta=out_amt / LAMPORTS, token_delta=-in_amt,
                        price_sol=(out_amt / LAMPORTS) / (in_amt / unit))
        # No Jupiter key: DexScreener reference price with a haircut each way.
        cost = self.e.paper_fee_pct / 2 / 100
        if order.side == "buy":
            px = order.ref_price_sol * (1 + cost)
            return Fill(order, True, sol_delta=-order.sol_amount, token_delta=int(order.sol_amount / px * unit),
                        price_sol=px)
        px = order.ref_price_sol * (1 - cost)
        return Fill(order, True, sol_delta=order.token_amount / unit * px, token_delta=-order.token_amount,
                    price_sol=px)

    def _live(self, order: Order, quote: dict | None) -> Fill:
        from ..wallet import confirm

        w = self.wallet
        quote = quote or self._quote(order)
        sol_before, tok_before = w.sol_balance(), w.token_balance(order.mint)
        tx = jupiter.swap_tx(quote, w.pubkey, self.e.priority_fee_lamports)
        sig = w.sign_and_send(tx)
        record("executor", "sent", signature=sig, side=order.side, mint=order.mint)
        landed = confirm(sig)
        sol_delta = w.sol_balance() - sol_before          # includes network + priority fees
        tok_delta = w.token_balance(order.mint) - tok_before
        moved = tok_delta > 0 if order.side == "buy" else tok_delta < 0
        if not landed and not moved:                     # a late landing still moved tokens -> book it
            return Fill(order, False, signature=sig, error="not confirmed / failed on-chain")
        price = abs(sol_delta) / (abs(tok_delta) / 10 ** order.decimals) if tok_delta else 0.0
        return Fill(order, True, sol_delta=sol_delta, token_delta=tok_delta, price_sol=price, signature=sig)
