"""Risk manager agent: sizing, exposure limits, daily loss limit, drawdown kill switch.
Owns the portfolio state (persisted to data/state.json)."""
from __future__ import annotations

import dataclasses
import json
import time

from ..journal import DATA
from ..models import Candidate, Fill, Order, Position, Signal

STATE = DATA / "state.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@dataclasses.dataclass
class Portfolio:
    sol: float
    start_sol: float
    positions: dict[str, Position] = dataclasses.field(default_factory=dict)
    day: str = dataclasses.field(default_factory=_today)
    day_realized_sol: float = 0.0
    cooldown_until: dict[str, float] = dataclasses.field(default_factory=dict)
    halted: str = ""            # non-empty = kill switch tripped, reason

    def equity(self, prices: dict[str, float]) -> float:
        return self.sol + sum(p.tokens / 10 ** p.decimals * prices.get(m, p.entry_price_sol)
                              for m, p in self.positions.items())

    def save(self) -> None:
        DATA.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(dataclasses.asdict(self), indent=2))

    @classmethod
    def load(cls, starting_sol: float) -> "Portfolio":
        if not STATE.exists():
            return cls(sol=starting_sol, start_sol=starting_sol)
        d = json.loads(STATE.read_text())
        d["positions"] = {m: Position(**p) for m, p in d["positions"].items()}
        return cls(**d)


class RiskManager:
    def __init__(self, params, pf: Portfolio):
        self.p = params
        self.pf = pf

    def roll_day(self) -> None:
        if self.pf.day != _today():
            self.pf.day, self.pf.day_realized_sol = _today(), 0.0

    def check_kill_switch(self, prices: dict[str, float]) -> str:
        eq = self.pf.equity(prices)
        dd = (1 - eq / self.pf.start_sol) * 100
        if dd >= self.p.capital.max_drawdown_pct and not self.pf.halted:
            self.pf.halted = f"drawdown {dd:.1f}% >= {self.p.capital.max_drawdown_pct}%"
        return self.pf.halted

    def can_open_any(self) -> str:
        """Empty string = new entries allowed; otherwise the reason they are blocked."""
        c = self.p.capital
        if self.pf.halted:
            return "halted: " + self.pf.halted
        if -self.pf.day_realized_sol >= c.daily_loss_limit_sol:
            return f"daily loss limit hit ({self.pf.day_realized_sol:.3f} SOL)"
        if len(self.pf.positions) >= c.max_open_positions:
            return "max open positions"
        if self.pf.sol - c.per_trade_sol < c.min_sol_reserve:
            return "insufficient SOL above reserve"
        return ""

    def excluded_mints(self) -> set[str]:
        now = time.time()
        return set(self.pf.positions) | {m for m, t in self.pf.cooldown_until.items() if t > now}

    def approve(self, c: Candidate, sig: Signal, decimals: int) -> tuple[Order | None, str]:
        why = self.can_open_any()
        if why:
            return None, why
        if c.mint in self.excluded_mints():
            return None, "already held or cooling down"
        return Order(c.mint, "buy", sol_amount=self.p.capital.per_trade_sol, decimals=decimals, symbol=c.symbol,
                     ref_price_sol=c.price_native, reason=f"score {sig.score}"), ""

    def apply_fill(self, f: Fill) -> None:
        o = f.order
        self.pf.sol += f.sol_delta
        if o.side == "buy":
            self.pf.positions[o.mint] = Position(
                mint=o.mint, symbol=o.symbol, entry_price_sol=f.price_sol, initial_tokens=f.token_delta,
                tokens=f.token_delta, cost_sol=-f.sol_delta, decimals=o.decimals, peak_price_sol=f.price_sol)
            return
        pos = self.pf.positions[o.mint]
        sold = -f.token_delta
        cost_part = pos.cost_sol * sold / pos.tokens if pos.tokens else 0.0
        pnl = f.sol_delta - cost_part
        pos.cost_sol -= cost_part
        pos.tokens -= sold
        pos.realized_sol += pnl
        self.pf.day_realized_sol += pnl
        if pos.tokens <= 0:
            del self.pf.positions[o.mint]
            self.pf.cooldown_until[o.mint] = time.time() + self.p.loop.cooldown_minutes_after_exit * 60
