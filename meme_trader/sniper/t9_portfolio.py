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
- an exit due at HOLD_S that fails and is filled by a later retry (`exit_at`, within RETRY_S) holds its cash until
  that fill, is booked on the fill's day at the fill's return, and is a failed exit meanwhile (a ninth review: the
  simulation had credited it at the intended time, inside the outage that delayed it);
- the daily RISK budget (DAY_STOP) counts measured losses and impairments on their day, plus a reservation for every
  open position whose due exit has passed without a fill (being retried);
- `measured_daily` is a diagnostic (measured trades only), never the primary statistic.

The risk POLICY (an eleventh review: the registered stop is an entry-halt trigger, not a maximum daily loss - four
fresh positions can all be admitted before any loss is realized, then lose 1.0 SOL past a 0.5 threshold). `risk`:
- "gross" (the registration's, the default): entries halt once the day's gross losses and impairments, plus a
  reservation for each overdue exit, reach DAY_STOP;
- "hard": a strict worst-case budget - a position is admitted only if the day's net economic loss so far, plus the
  total loss (stake + fees) of EVERY open position, plus this one's, stays within DAY_STOP: the day can't lose more;
- "net+cap": entries halt once the day's NET economic loss (plus overdue reservations) reaches DAY_STOP, or its gross
  losses reach an outer cap of OUTER_CAP x DAY_STOP (frozen here; any other budget is the owner's to choose).

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
RISK_POLICIES = ("gross", "hard", "net+cap")
OUTER_CAP = 2.0                      # net+cap's outer gross-loss cap, in DAY_STOPs
HOLD_S, DELAY_S, RETRY_S = 3600, 60, 900
FEE = 0.012                          # round trip, when a scenario doesn't model fees itself


class Portfolio:
    def __init__(self, end_t: float, bank: float = BANK, size: float = SIZE, max_open: int = MAX_OPEN,
                 day_stop: float = DAY_STOP, trace: bool = False, risk: str = "gross"):
        if risk not in RISK_POLICIES:
            raise ValueError(f"risk policy {risk!r}: one of {RISK_POLICIES}")
        self.end_t, self.bank, self.cash = end_t, bank, bank
        self.size, self.max_open, self.day_stop, self.risk = size, max_open, day_stop, risk
        self.open: list[dict] = []
        self.quarantined: set = set()                            # coins whose tokens were impaired, not disposed of
        self.economic: dict[int, float] = defaultdict(float)     # UTC day -> economic P&L (the primary series)
        self.measured_pnl: dict[int, float] = defaultdict(float)  # UTC day -> measured exits only (diagnostic)
        self.lost: dict[int, float] = defaultdict(float)         # UTC day -> risk losses (measured + impairments)
        self.impairments = 0.0                                   # SOL written off (stakes and their fees)
        self.attempted = self.measured = self.trapped = 0
        self.exits = self.late_exits = 0                         # measured exits executed, and those filled late
        self.skips: dict[str, int] = defaultdict(int)            # signals not entered, by reason
        self.stop_at: dict[int, float] = {}                      # UTC day -> when its risk stop first refused one
        self.max_open_seen = 0
        self.pnls: list[float] = []                              # each measured exit's P&L (concentration)
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
        due = sorted((p for p in self.open if p["free_t"] <= t), key=lambda p: (p["free_t"], p["n"]))
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
            day = int(p["free_t"] // 86400)              # the day it filled: due, or a later retry
            pnl = self.size * p["ret"]
            self.cash += self.size + pnl
            self.economic[day] += pnl
            self.measured_pnl[day] += pnl
            self.lost[day] += max(-pnl, 0.0)
            self.exits += 1
            self.late_exits += int(p["free_t"] > p["exit_t"])      # (a plain int, whatever the times' types)
            self.pnls.append(pnl)
            self._log("exit", p["free_t"], coin=p["coin"], pnl=round(pnl, 6), late_s=round(p["free_t"] - p["exit_t"], 3))

    def risk_used(self, t: float) -> float:
        """The UTC day's risk losses at t, with a reservation for each position whose due exit has passed without a
        fill - from the first failed attempt until it fills or is impaired."""
        day = int(t // 86400)
        reserved = sum(self.size * (1.0 + p["fee"]) for p in self.open if p["exit_t"] <= t)
        return self.lost[day] + reserved

    def _stopped(self, t: float, fee: float) -> bool:
        """The risk policy refuses a new position at t (see the module notes)."""
        day = int(t // 86400)
        if self.risk == "gross":
            return self.risk_used(t) >= self.day_stop - 1e-12
        overdue = sum(self.size * (1.0 + p["fee"]) for p in self.open if p["exit_t"] <= t)
        net_loss = max(-self.economic[day], 0.0)
        if self.risk == "hard":
            worst = sum(self.size * (1.0 + p["fee"]) for p in self.open) + self.size * (1.0 + fee)
            return net_loss + worst > self.day_stop + 1e-12
        return net_loss + overdue >= self.day_stop - 1e-12 or \
            self.lost[day] + overdue >= OUTER_CAP * self.day_stop - 1e-12

    def try_enter(self, signal_t: float, coin, ret: float | None, fee: float = FEE, exit_at: float | None = None) -> str:
        """A signal at signal_t on `coin` whose trade would return `ret` (net) when its exit fills at `exit_at` (None:
        on time, HOLD_S after entering; later: a retry within RETRY_S filled it), or ret None if no exit can be
        measured. Returns "" if entered, else why not."""
        t = signal_t + DELAY_S
        self.settle(t)
        why = ("window" if t + HOLD_S + RETRY_S > self.end_t else
               "max open" if len(self.open) >= self.max_open else
               "same coin" if coin in self.quarantined or any(p["coin"] == coin for p in self.open) else
               "daily stop" if self._stopped(t, fee) else
               "cash" if self.cash < self.size - 1e-12 else "")
        if why:
            self.skips[why] += 1
            if why == "daily stop":
                self.stop_at.setdefault(int(t // 86400), t)
            self._log("skip", t, coin=coin, why=why)
            return why
        exit_t = t + HOLD_S
        if ret is not None and exit_at is not None and not exit_t <= exit_at <= exit_t + RETRY_S:
            raise ValueError(f"exit_at {exit_at} outside the exit's retry window [{exit_t}, {exit_t + RETRY_S}]")
        self.cash -= self.size
        self.attempted += 1
        self._n = getattr(self, "_n", 0) + 1
        if ret is None:
            self.open.append({"coin": coin, "exit_t": exit_t, "free_t": exit_t + RETRY_S, "ret": None, "fee": fee,
                              "n": self._n})
        else:
            self.measured += 1
            self.open.append({"coin": coin, "exit_t": exit_t, "free_t": exit_t if exit_at is None else exit_at,
                              "ret": ret, "fee": fee, "n": self._n})
        self.max_open_seen = max(self.max_open_seen, len(self.open))
        self._log("enter", t, coin=coin, measured=ret is not None)
        return ""

    def close(self) -> None:
        self.settle(math.inf)

    def daily(self, days: int) -> list[float]:
        """The primary series: economic P&L per UTC day, every day of the window (zero-trade days included)."""
        return [self.economic.get(d, 0.0) for d in range(days)]

    def measured_daily(self, days: int) -> list[float]:
        return [self.measured_pnl.get(d, 0.0) for d in range(days)]
