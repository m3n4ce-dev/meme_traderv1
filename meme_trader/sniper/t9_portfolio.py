"""The T9-E1 paper account, event by event: ONE implementation for the future paper runner and for the power
simulation (a seventh review, 2026-10-07: the simulation had re-implemented the procedure and lost the bankroll,
the clock across midnight and the capital tied up in unmeasured exits).

Time is continuous (seconds from the window's start); UTC days are `int(t // 86400)`. A position:
- enters at its signal time + DELAY_S, debiting SIZE from cash, if: the account has SIZE in cash, fewer than MAX_OPEN
  positions are open (unmeasured ones included), none is open on the same coin, the UTC day's realized losses are
  under DAY_STOP, and it can close inside the window (no entry later than end - DELAY_S - HOLD_S - RETRY_S);
- exits HOLD_S after entering. A measured exit credits SIZE x (1 + return) on the day it lands and counts toward
  that day's realized loss. An unmeasured exit keeps its capital tied up for RETRY_S more, then is written off:
  excluded from the primary P&L (it isn't a measured outcome), charged at zero recovery minus fees in the
  conservative total, and never returned to cash.
`trace` records every entry, skip and exit with its time, cash and open count.
"""
from __future__ import annotations

import math
from collections import defaultdict

BANK, SIZE, MAX_OPEN, DAY_STOP = 9.0, 0.25, 4, 0.5
HOLD_S, DELAY_S, RETRY_S = 3600, 60, 900
FEE = 0.012                          # round trip, when a scenario doesn't model fees itself


class Portfolio:
    def __init__(self, end_t: float, bank: float = BANK, size: float = SIZE, max_open: int = MAX_OPEN,
                 day_stop: float = DAY_STOP, trace: bool = False):
        self.end_t, self.cash, self.size, self.max_open, self.day_stop = end_t, bank, size, max_open, day_stop
        self.open: list[dict] = []
        self.realized: dict[int, float] = defaultdict(float)     # UTC day -> realized P&L of measured exits
        self.lost: dict[int, float] = defaultdict(float)         # UTC day -> realized losses (the daily stop)
        self.conservative = 0.0                                  # unmeasured at zero recovery minus fees
        self.attempted = self.measured = self.trapped = 0
        self.trace: list | None = [] if trace else None

    def _log(self, kind: str, t: float, **kw) -> None:
        if self.trace is not None:
            self.trace.append({"kind": kind, "t": t, "cash": round(self.cash, 6), "open": len(self.open), **kw})

    def settle(self, t: float) -> None:
        """Every exit due by t, in time order, booked on the UTC day it happens."""
        due = sorted((p for p in self.open if p["free_t"] <= t), key=lambda p: p["free_t"])
        for p in due:
            self.open.remove(p)
            day = int(p["exit_t"] // 86400)
            if p["ret"] is None:                                 # unmeasured: written off, never back in cash
                self.trapped += 1
                self._log("written_off", p["free_t"], coin=p["coin"])
                continue
            pnl = self.size * p["ret"]
            self.cash += self.size + pnl
            self.realized[day] += pnl
            self.lost[day] += max(-pnl, 0.0)
            self._log("exit", p["exit_t"], coin=p["coin"], pnl=round(pnl, 6))

    def try_enter(self, signal_t: float, coin, ret: float | None, fee: float = FEE) -> str:
        """A signal at signal_t on `coin` whose trade would return `ret` (net), or None if its exit can't be
        measured. Returns "" if entered, else why not."""
        t = signal_t + DELAY_S
        self.settle(t)
        day = int(t // 86400)
        why = ("window" if t + HOLD_S + RETRY_S > self.end_t else
               "max open" if len(self.open) >= self.max_open else
               "same coin" if any(p["coin"] == coin for p in self.open) else
               "daily stop" if self.lost[day] >= self.day_stop else
               "cash" if self.cash < self.size - 1e-12 else "")
        if why:
            self._log("skip", t, coin=coin, why=why)
            return why
        self.cash -= self.size
        self.attempted += 1
        exit_t = t + HOLD_S
        if ret is None:
            self.conservative += self.size * (-1.0 - fee)
            self.open.append({"coin": coin, "exit_t": exit_t, "free_t": exit_t + RETRY_S, "ret": None})
        else:
            self.measured += 1
            self.conservative += self.size * ret
            self.open.append({"coin": coin, "exit_t": exit_t, "free_t": exit_t, "ret": ret})
        self._log("enter", t, coin=coin, measured=ret is not None)
        return ""

    def close(self) -> None:
        self.settle(math.inf)

    def daily(self, days: int) -> list[float]:
        return [self.realized.get(d, 0.0) for d in range(days)]
