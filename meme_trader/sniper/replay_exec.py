"""Execution and censoring over recorded trades: one implementation for replays and the forward test.

A sixth review (2026-10-07) found the historical T9 replays booking trades that could not have happened: an exit
with no recorded trade after it was filled at the trigger price, and a position whose holding period ran past the end
of the recording was sold at its last print. Here a fill is only ever a recorded trade at or after its target time,
inside what the recording observed. Anything else gets a status, never a price:

- `closed_measured`: entry and exit both filled at recorded trades.
- `censored`: the pool went DEAD_S without a trade around a due fill: no executable sale was observed. The last
  print is kept as a *mark* for sensitivity, never booked as P&L.
- `pending`: the recording ended (or the position is still open) before the fill could be observed.
- `execution_unmeasured`: the fill window falls in a recording gap (the recorder was down).

Fill rules (the forward test's, `revival.py`): an entry fills at the first trade at or after decision + delay. A timed
exit is due at fill + hold (pushed to the end of a gap it falls in) and fills at the first trade at or after due +
delay. A stop is noticed at the trade that crosses it and fills at the first trade at or after that + delay.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

DEAD_S = 1800                    # due, and no trade this long: censored (no executable sale observed)

CLOSED, CENSORED, PENDING, UNMEASURED = "closed_measured", "censored", "pending", "execution_unmeasured"


@dataclass
class Coverage:
    """What the recording observed: [start, end] minus gaps (recorder pauses or outages)."""
    start: float
    end: float
    gaps: list = field(default_factory=list)          # [(a, b)], sorted

    def gap_overlapping(self, a: float, b: float):
        for g in self.gaps:
            if g[0] < b and g[1] > a:
                return g
        return None


def timer_due(fill_t: float, hold: float, gaps=()) -> float:
    """A timed exit's due time: fill + hold, or the end of a gap it falls in (it sells when observation resumes)."""
    due = fill_t + hold
    for a, b in gaps:
        if a <= due <= b:
            due = b
    return due


def is_censored(now: float, last_trade_t: float, due: float, dead_s: float = DEAD_S) -> bool:
    """Streaming form: due, and no trade for dead_s since the later of the last trade and the due time."""
    return now >= due and now - max(last_trade_t, due) >= dead_s


@dataclass
class Fill:
    status: str                  # CLOSED (filled), CENSORED, PENDING or UNMEASURED
    t: float | None = None       # the recorded trade it filled at
    px: float | None = None
    mark_t: float | None = None  # when not filled: the last print before the window, for sensitivity only
    mark_px: float | None = None
    why: str = ""


def fill_at(ts: list, px: list, target: float, cov: Coverage, due: float | None = None,
            dead_s: float = DEAD_S) -> Fill:
    """The first recorded trade at or after `target`, if the recording shows it could happen.

    `due` (default target) starts the quiet-pool clock: if no trade comes within dead_s of the later of the last trade
    and `due`, the fill is censored. A window running into a gap is unmeasured; past the recording's end, pending."""
    due = target if due is None else due
    i = bisect.bisect_left(ts, target)
    j = i - 1                                          # the last print before the target (the mark)
    mark_t, mark_px = (ts[j], px[j]) if j >= 0 else (None, None)
    # the quiet-pool clock: from the later of `due` and the last trade before target
    clock = max(due, ts[j]) if j >= 0 else due
    deadline = clock + dead_s
    if i < len(ts) and ts[i] <= cov.end:
        g = cov.gap_overlapping(min(target, clock), ts[i])
        if g is not None:
            return Fill(UNMEASURED, mark_t=mark_t, mark_px=mark_px, why=f"recording gap {g[0]:.0f}-{g[1]:.0f}")
        if ts[i] - clock < dead_s:                     # trades before the target keep the pool's clock alive
            return Fill(CLOSED, t=ts[i], px=px[i])
        return Fill(CENSORED, mark_t=mark_t, mark_px=mark_px, why=f"no trade for {dead_s:.0f} s: no executable sale")
    if cov.end - clock >= dead_s:                      # observed long enough after: nothing came
        g = cov.gap_overlapping(clock, deadline)
        if g is not None:
            return Fill(UNMEASURED, mark_t=mark_t, mark_px=mark_px, why=f"recording gap {g[0]:.0f}-{g[1]:.0f}")
        return Fill(CENSORED, mark_t=mark_t, mark_px=mark_px, why=f"no trade for {dead_s:.0f} s: no executable sale")
    return Fill(PENDING, mark_t=mark_t, mark_px=mark_px, why="the recording ends before this could be observed")


@dataclass
class Outcome:
    status: str
    entry_t: float | None = None
    entry_px: float | None = None
    exit_why: str = ""
    exit_trigger_t: float | None = None
    exit_t: float | None = None
    exit_px: float | None = None
    ret: float | None = None     # net of cost; only when closed_measured
    mark_ret: float | None = None   # sensitivity only: the last print against the entry, net of cost
    why: str = ""


def simulate(ts: list, px: list, decided_t: float, delay: float, hold: float, cost: float, cov: Coverage,
             stop: float | None = None, take: float | None = None, trail: tuple | None = None,
             dead_s: float = DEAD_S) -> Outcome:
    """One position over one pool's recorded trades: enter at decided_t + delay; leave on the first of stop (fraction
    down from the fill), take (fraction up), trail ((arm, give-back)) or the timer. Every exit fills at a recorded trade
    at or after its trigger + delay, or the outcome says why it couldn't be measured."""
    e = fill_at(ts, px, decided_t + delay, cov, dead_s=dead_s)
    if e.status != CLOSED:
        return Outcome(e.status, why="entry: " + e.why)
    p0, te = e.px, e.t
    due = timer_due(te, hold, cov.gaps)
    trigger, why = due, "time"
    peak = p0
    k = bisect.bisect_right(ts, te)                    # trades after the entry fill
    while k < len(ts) and ts[k] < due:
        t, p = ts[k], px[k]
        peak = max(peak, p)
        g = p / p0 - 1
        if stop is not None and g <= -stop:
            trigger, why = t, "stop"
            break
        if take is not None and g >= take:
            trigger, why = t, "take"
            break
        if trail is not None and peak / p0 - 1 >= trail[0] and p <= peak * (1 - trail[1]):
            trigger, why = t, "trail"
            break
        k += 1
    x = fill_at(ts, px, trigger + delay, cov, due=trigger, dead_s=dead_s)
    if x.status != CLOSED:
        mark = (x.mark_px / p0 - 1 - cost) if x.mark_px else None
        return Outcome(x.status, te, p0, why, trigger, mark_ret=mark, why="exit: " + x.why)
    return Outcome(CLOSED, te, p0, why, trigger, x.t, x.px, ret=x.px / p0 - 1 - cost)
