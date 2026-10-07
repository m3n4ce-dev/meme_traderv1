"""The T9 revival signal: ONE implementation, shared by the replay (research/exploratory/t9_replay_v2.py) and the
future T9-E1 runner, frozen by its version and source hash (a seventh review, 2026-10-07, found the registered prose
and the replay's code disagreeing at the exact 5-minute boundary).

For a pool's trades `p` = [(t, price, is_buy, sol), ...] in time order, a check runs at a trade whose time t is at
least 30 s after the previous check, once the pool has 65 minutes of history in the recording (t - first >= 3900):

- price 5 min ago  = the last trade price AT OR BEFORE t - 300 (a trade exactly at t - 300 counts);
- price 15 min ago = the last trade price AT OR BEFORE t - 900;
- ret5  = price / price 5 min ago - 1; ret15 likewise;
- v5    = SOL volume of the trades with t - 300 <= ts, up to and including the checked trade;
- v60   = SOL volume of the trades with t - 3900 <= ts < t - 300 (the 60 minutes before the last 5);
- surge = v5 / (v60 / 12), or 0 when v60 is 0.
A check without a price at or before t - 300 and t - 900 yields nothing.
"""
from __future__ import annotations

import bisect
import hashlib
from pathlib import Path

SIGNAL_VERSION = "t9-signal-2"          # 1: the first replay (the price strictly BEFORE t - 300)
HISTORY_S, CHECK_S = 3900, 30


def signals(p: list) -> list[tuple]:
    """Every check of one pool: (t, ret5, ret15, surge)."""
    ts = [x[0] for x in p]
    cum = [0.0]
    for x in p:
        cum.append(cum[-1] + x[3])
    out, last = [], -1e18
    for i, (t, px, _buy, _sol) in enumerate(p):
        if t - last < CHECK_S or not p or t - p[0][0] < HISTORY_S:
            continue
        last = t
        k5 = bisect.bisect_right(ts, t - 300) - 1          # the last trade at or before t - 300
        k15 = bisect.bisect_right(ts, t - 900) - 1
        if k5 < 0 or k15 < 0:
            continue
        j5 = bisect.bisect_left(ts, t - 300)               # the volume windows' edges
        j65 = bisect.bisect_left(ts, t - 3900)
        v5 = cum[i + 1] - cum[j5]
        v60 = cum[j5] - cum[j65]
        out.append((t, px / p[k5][1] - 1, px / p[k15][1] - 1, v5 / (v60 / 12) if v60 > 0 else 0.0))
    return out


def source_hash() -> str:
    """sha256 of this file: frozen with a registration, so the signal can't change under it."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
