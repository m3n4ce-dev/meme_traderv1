"""Orchestrator: runs one cycle of the agent pipeline.

    Monitor (exits first)  ->  Risk gate  ->  Scout  ->  Safety  ->  Analyst  ->  Risk approve  ->  Executor
"""
from __future__ import annotations

import time

from .agents import analyst, monitor, safety, scout
from .agents.executor import Executor
from .agents.risk import Portfolio, RiskManager
from .clients import dexscreener, rugcheck
from .journal import record
from .models import Order


class Orchestrator:
    def __init__(self, params, wallet=None):
        self.p = params
        self.pf = Portfolio.load(params.capital.starting_sol)
        self.risk = RiskManager(params, self.pf)
        self.exec = Executor(params, wallet)

    def prices(self) -> dict[str, float]:
        if not self.pf.positions:
            return {}
        out = {}
        for m, p in dexscreener.best_pair_by_mint(list(self.pf.positions)).items():
            px = float(p.get("priceNative") or 0)
            if px > 0:                       # a missing price must fall back to entry, never value the bag at 0
                out[m] = px
        return out

    def manage_positions(self, prices: dict[str, float]) -> None:
        for mint, pos in list(self.pf.positions.items()):
            px = prices.get(mint)
            if not px:
                record("monitor", "no_price", mint=mint, symbol=pos.symbol)
                continue
            order = monitor.evaluate(pos, px, self.p.exits)
            if self.pf.halted and not order:
                order = Order(mint, "sell", token_amount=pos.tokens, decimals=pos.decimals,
                                      symbol=pos.symbol, ref_price_sol=px, reason="kill switch")
            if not order or order.token_amount <= 0:
                continue
            record("monitor", "exit_signal", mint=mint, symbol=pos.symbol, reason=order.reason, price=px)
            fill = self.exec.execute(order)
            if fill.ok:
                if order.reason.startswith(monitor.TP_REASON):
                    pos.tp_levels_hit += 1
                self.risk.apply_fill(fill)

    def look_for_entries(self) -> None:
        blocked = self.risk.can_open_any()
        if blocked:
            record("risk", "entries_blocked", reason=blocked)
            return
        for c in scout.scan(self.p, self.risk.excluded_mints()):
            if self.risk.can_open_any():
                break
            try:
                rc = rugcheck.report(c.mint)
            except Exception as e:
                record("safety", "error", mint=c.mint, error=str(e))
                continue
            rep = safety.evaluate(c, rc, self.p.safety)
            if not rep.passed:
                record("safety", "reject", mint=c.mint, symbol=c.symbol, reasons=rep.reasons)
                continue
            sig = analyst.evaluate(c, self.p.analyst)
            record("analyst", "pass" if sig.passed else "reject", mint=c.mint, symbol=c.symbol,
                   score=sig.score, notes=sig.notes, safety=rep.raw)
            if not sig.passed:
                continue
            decimals = int((rc.get("token") or {}).get("decimals") or 6)
            order, why = self.risk.approve(c, sig, decimals)
            if not order:
                record("risk", "reject", mint=c.mint, reason=why)
                continue
            quote, why = self.exec.precheck_buy(order)
            if why:
                record("executor", "precheck_reject", mint=c.mint, symbol=c.symbol, reason=why)
                continue
            fill = self.exec.execute(order, quote)
            if fill.ok:
                self.risk.apply_fill(fill)

    def cycle(self) -> None:
        self.risk.roll_day()
        prices = self.prices()
        halted = self.risk.check_kill_switch(prices)
        self.manage_positions(prices)
        if not halted:
            self.look_for_entries()
        self.pf.save()
        record("orchestrator", "cycle_done", sol=round(self.pf.sol, 4),
               equity=round(self.pf.equity(prices), 4), open=len(self.pf.positions),
               day_pnl=round(self.pf.day_realized_sol, 4), halted=self.pf.halted)

    def run(self, once: bool = False) -> None:
        while True:
            try:
                self.cycle()
            except Exception as e:
                record("orchestrator", "cycle_error", error=repr(e))
            if once:
                return
            time.sleep(self.p.loop.poll_seconds)
