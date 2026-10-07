"""The T9-E1 paper account, event by event: ONE implementation for the future paper runner and for the power
simulation (a seventh review, 2026-10-07: the simulation had re-implemented the procedure and lost the bankroll,
the clock across midnight and the capital tied up in unmeasured exits).

The account's contract (an eighth review, 2026-10-07: the primary P&L had left out written-off capital, so a run
could lose cash while reporting zero, and the daily stop didn't see the loss):
- the PRIMARY series is the account's economic P&L by UTC day, and it reconciles to cash: once nothing is open and
  nothing flowed in or out, sum(daily) == cash - bank. Every pass statistic (the bootstrap, the hurdle) uses it;
- a measured exit books its net result on the day it lands;
- an exit that can't be measured is retried for RETRY_S; then it's IMPAIRED: zero recovery plus its attributable fees
  (the round-trip fee assumption, debited from cash as well), booked on the day the retry runs out - the day it's
  recognized, not the day the exit was due. Its tokens stay as quarantined inventory: a paper write-off isn't proof
  they're gone, so the same coin can't be entered again in the window;
- the daily RISK budget (DAY_STOP) counts measured losses and impairments on their day, plus a reservation for every
  exit that has already failed and is still being retried;
- `measured_daily` is a diagnostic (measured trades only), never the primary statistic.

Time is continuous (seconds from the window's start); UTC days are `int(t // 86400)`. A position:
- enters at its signal time + DELAY_S, debiting SIZE from cash, if: it can close inside the window (no entry later
  than end - DELAY_S - HOLD_S - RETRY_S), fewer than MAX_OPEN positions are open (unmeasured ones still retrying
  included), the coin isn't open or quarantined, the UTC day's risk losses are under DAY_STOP, and there's SIZE in cash;
- exits HOLD_S after entering.
`trace` records every entry, skip, exit and impairment with its time, cash and open count.
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
        self.end_t, self.bank, self.cash = end_t, bank, bank
        self.size, self.max_open, self.day_stop = size, max_open, day_stop
        self.open: list[dict] = []
        self.quarantined: set = set()                            # coins whose tokens were impaired, not disposed of
        self.economic: dict[int, float] = defaultdict(float)     # UTC day -> economic P&L (the primary series)
        self.measured_pnl: dict[int, float] = defaultdict(float)  # UTC day -> measured exits only (diagnostic)
        self.lost: dict[int, float] = defaultdict(float)         # UTC day -> risk losses (measured + impairments)
        self.impairments = 0.0                                   # SOL written off (stakes and their fees)
        self.attempted = self.measured = self.trapped = 0
        self.trace: list | None = [] if trace else None

    # (names kept for callers: `realized` is the primary series; `conservative` its total)
    @property
    def realized(self) -> dict[int, float]:
        return self.economic

    @property
    def conservative(self) -> float:
        return sum(self.economic.values())

    def _log(self, kind: str, t: float, **kw) -> None:
        if self.trace is not None:
            self.trace.append({"kind": kind, "t": t, "cash": round(self.cash, 6), "open": len(self.open), **kw})

    def settle(self, t: float) -> None:
        """Every measured exit and every impairment due by t, in time order, each booked on the UTC day it happens."""
        due = sorted((p for p in self.open if p["free_t"] <= t), key=lambda p: p["free_t"])
        for p in due:
            self.open.remove(p)
            if p["ret"] is None:                                 # the retries ran out: zero recovery plus its fees
                day = int(p["free_t"] // 86400)
                loss = self.size * (1.0 + p["fee"])
                self.cash -= self.size * p["fee"]                # (the stake left cash at entry)
                self.economic[day] -= loss
                self.lost[day] += loss
                self.impairments += loss
                self.trapped += 1
                self.quarantined.add(p["coin"])
                self._log("impaired", p["free_t"], coin=p["coin"], pnl=round(-loss, 6))
                continue
            day = int(p["exit_t"] // 86400)
            pnl = self.size * p["ret"]
            self.cash += self.size + pnl
            self.economic[day] += pnl
            self.measured_pnl[day] += pnl
            self.lost[day] += max(-pnl, 0.0)
            self._log("exit", p["exit_t"], coin=p["coin"], pnl=round(pnl, 6))

    def risk_used(self, t: float) -> float:
        """The UTC day's risk losses at t, with a reservation for each failed exit still being retried."""
        day = int(t // 86400)
        reserved = sum(self.size * (1.0 + p["fee"]) for p in self.open if p["ret"] is None and p["exit_t"] <= t)
        return self.lost[day] + reserved

    def try_enter(self, signal_t: float, coin, ret: float | None, fee: float = FEE) -> str:
        """A signal at signal_t on `coin` whose trade would return `ret` (net), or None if its exit can't be
        measured. Returns "" if entered, else why not."""
        t = signal_t + DELAY_S
        self.settle(t)
        why = ("window" if t + HOLD_S + RETRY_S > self.end_t else
               "max open" if len(self.open) >= self.max_open else
               "same coin" if coin in self.quarantined or any(p["coin"] == coin for p in self.open) else
               "daily stop" if self.risk_used(t) >= self.day_stop - 1e-12 else
               "cash" if self.cash < self.size - 1e-12 else "")
        if why:
            self._log("skip", t, coin=coin, why=why)
            return why
        self.cash -= self.size
        self.attempted += 1
        exit_t = t + HOLD_S
        if ret is None:
            self.open.append({"coin": coin, "exit_t": exit_t, "free_t": exit_t + RETRY_S, "ret": None, "fee": fee})
        else:
            self.measured += 1
            self.open.append({"coin": coin, "exit_t": exit_t, "free_t": exit_t, "ret": ret, "fee": fee})
        self._log("enter", t, coin=coin, measured=ret is not None)
        return ""

    def close(self) -> None:
        self.settle(math.inf)

    def daily(self, days: int) -> list[float]:
        """The primary series: economic P&L per UTC day, every day of the window (zero-trade days included)."""
        return [self.economic.get(d, 0.0) for d in range(days)]

    def measured_daily(self, days: int) -> list[float]:
        return [self.measured_pnl.get(d, 0.0) for d in range(days)]
