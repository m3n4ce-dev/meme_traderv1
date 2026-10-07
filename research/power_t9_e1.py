"""Power, null and stress simulation of the WHOLE proposed T9-E1 procedure, run through the same account model as
the paper runner (meme_trader/sniper/t9_portfolio.py).

    python research/power_t9_e1.py [--sims 150] [--null-sims 500] [--boots 400] [--hurdle 1.0] [--procs 8]
                                   [--json out.json]                                                 (needs numpy)

Version 6 (an eleventh review, 2026-10-07): PAIRED sensitivities. v5's drift cells had their own seeds, so a
difference between cells mixed the process change with Monte Carlo noise. Now each simulated window draws its signals,
outages, quotes, late-price shocks and bootstrap resamples ONCE, and runs every variant on them (common random
numbers): the late-price drift (centered / positive / negative) and the predeclared risk policies (t9_portfolio:
"gross" - the registration's entry-halt trigger; "hard" - a strict worst-case daily budget; "net+cap" - a net-loss stop
with an outer gross cap), at +0, +1.5 and +2.8 points of extra cost (+2.8 makes the round trip ~4%: the builder's
measured fee + impact + network estimate, unverified by the reviewer). Each variant reports its pass rate and the
PAIRED difference from the base (centered, gross) with a 95% interval and the discordant counts; and admitted trades,
stop days and times, total, worst day, peak drawdown, exposure, impairments and the top three trades' share.
`python research/power_t9_e1.py --version 5` reproduces version 5.

Version 5 (a tenth review, 2026-10-07). Version 4 moved a late fill's price by exp(sigma Z): with zero log drift
that has a POSITIVE expected price drift (+2.2% for a 15-minute delay), so even its "no edge" process gained by being
late. The late multiplier is now centered, 1 + sigma Z (a gross-price martingale whose Z = 0 path is no move); v4's
process and its mirror run as labelled sensitivities on a subset fixed in `cells`. The late-fill share now counts the exits the
account executed (not every quote opportunity), and each cell reports both arms' signals, entries, skips by reason,
measured and impaired exits. Version 4's output is kept, labelled historical.

Version 4 (a ninth review, 2026-10-07). Version 3 found that SOME retry within the window got a quote, then credited
the exit at its intended time - inside the very outage that delayed it - at the intended return; a failed-then-
recovered exit also skipped the failed-exit risk reservation; unquotable pools were drawn from the trade's eventual
return (a stress, not a base case); transient failures were independent per attempt (recovery looked too easy); and
only the signal arm had to reach the coverage gate. Now:
- each arm's exits are simulated attempt by attempt on the fixed 30 s retry clock (`_arm_quotes` returns the FIRST
  valid quote's time): the position holds its cash until then, is a failed exit (risk reserved) from the due time,
  and fills at the price then - the hour's return moved on by the extra minutes (the ordinary spread per hour);
- transient failures come in streaks (one failure blocks the next 30-300 s of attempts on that pool);
- an unquotable pool is drawn independently of the trade's return; the informative version (collapsing pools fail
  three times as often) is a separately labelled stress scenario;
- INVALID if observed hours < 80%, or EITHER arm's coverage is < 80%;
- availability scenarios follow the reviewer's planning envelope as a small, frozen joint grid (A1 best .. A5 worst),
  declared before running: they bracket engineering risk and are not estimates of any provider.
Everything else as version 3: the economic primary series for every gate, one-day bootstrap registered and 3-day
blocks beside it, the INVALID rule, provenance (sha256 of this script and the account model, the revision and any
uncommitted diff taken before the run, the output's own sha256). Version 3's output is kept, labelled historical.

`one_test` is version 2's procedure (independent or outage-day missingness, signal arm only), kept for its earlier
table and the reviewer's contracts on it; the account model under it is the current one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meme_trader.sniper import t9_portfolio as tp  # noqa: E402
from meme_trader.sniper.t9_portfolio import BANK, DELAY_S, HOLD_S, MAX_OPEN, RETRY_S, SIZE, Portfolio  # noqa: E402

__all__ = ["one_test", "one_test_v4", "one_test_v5", "one_test_v6", "wilson", "BANK", "MAX_OPEN", "SIZE"]      # (the account constants, for callers)

SEED = 20261007
EPISODES_PER_DAY = 35.4              # the corrected replay, primary-like rule, ~3.1 days of coverage
WIN_EPISODES_PER_DAY = 1.0           # its top 3 episodes in that time (optimistic: selected on the same days)
WIN_NET = (5.4, 8.31, 14.44)         # their winning trades' NET returns
FEE = tp.FEE
CONTROL = (-0.02, 0.15)
SD = 0.4164
ORDINARY = {"base": (-0.0758, SD), "pessimistic": (-0.1368, SD),
            "null": (math.log(1 + FEE) - SD ** 2 / 2, SD),            # breakeven after costs, the replay's spread
            "flat": (math.log(1 + FEE) - SD ** 2 / 2, SD),
            "mild": (math.log(1.01 + FEE) - SD ** 2 / 2, SD)}       # +1% a trade after costs
# (mean up h, mean outage min, long outages a day, long outage h, transient failure per attempt, unquotable pool)
AVAILABILITY = {
    # version 4's frozen joint grid (the reviewer's planning envelope, round 9): best to worst, declared in advance
    "A1": (168.0, 0.5, 0.0, 0.0, 0.01, 0.005),
    "A2": (48.0, 5.0, 1 / 30, 4.0, 0.05, 0.01),
    "A3": (24.0, 5.0, 1 / 30, 8.0, 0.10, 0.03),
    "A4": (12.0, 30.0, 1 / 7, 8.0, 0.10, 0.03),
    "A5": (6.0, 120.0, 1 / 7, 24.0, 0.20, 0.08),
    "A3 informative": (24.0, 5.0, 1 / 30, 8.0, 0.10, 0.03),
    # version 3's named scenarios (kept for reference)
    "good": (48.0, 10.0, 0.0, 0.0, 0.05, 0.01),
    "medium": (12.0, 30.0, 1 / 30, 4.0, 0.10, 0.03),
    "poor": (6.0, 60.0, 1 / 15, 8.0, 0.20, 0.08)}
INFORMATIVE = {"A3 informative"}     # collapsing pools (net return < -50%) are unquotable 3x as often: a stress
STREAK_S = (30.0, 300.0)             # a transient failure blocks that pool's attempts for this long (uniform)
LATE_SD = SD                         # the price keeps moving after the hour: this log spread per hour, per extra minute
ATTEMPT_S = 30                       # exit retries, from the exit time until RETRY_S after it (as registered)


def one_test(rng, days, win_per_day, ordinary, extra, u, hurdle, boots, cluster=0.0):
    """Version 2's procedure (see the module's docstring)."""
    mu, sd = ORDINARY[ordinary]
    end = days * 86400
    port, ctl = Portfolio(end), Portfolio(end)
    p_win = min(win_per_day / EPISODES_PER_DAY, 1.0)
    sigs = []
    for d in range(days):
        n_ep = rng.poisson(EPISODES_PER_DAY * rng.gamma(4.0, 0.25))
        outage = cluster > 0 and rng.random() < cluster
        u_day = (min(1.0, u / cluster) if outage else 0.0) if cluster > 0 else u
        for e in range(n_ep):
            t = d * 86400 + rng.uniform(0, 86400)
            win = rng.random() < p_win
            for k in range(1 + rng.poisson(0.22)):
                sigs.append((t + k * (7200 + rng.uniform(0, 7200)), (d, e), win and k == 0, u_day))
    sigs.sort()
    for t, coin, win, u_day in sigs:
        r = (rng.choice(WIN_NET) - extra) if win else (math.exp(rng.normal(mu, sd)) - 1 - FEE - extra)
        port.try_enter(t, coin, None if rng.random() < u_day else r)
        ctl.try_enter(t, coin, math.exp(rng.normal(*CONTROL)) - 1 - FEE - extra)
    port.close()
    ctl.close()
    s_day, c_day = np.array(port.daily(days)), np.array(ctl.daily(days))
    attempted, measured, s_cons = port.attempted, port.measured, port.conservative
    idx = rng.integers(0, days, size=(boots, days))
    m = s_day[idx].mean(1)
    diff = (s_day - c_day)[idx].mean(1)
    lo, hi, dlo = np.percentile(m, 5), np.percentile(m, 95), np.percentile(diff, 5)
    cov = measured / attempted if attempted else 0.0
    total, ctl_total = s_day.sum(), c_day.sum()
    passed = lo > 0 and dlo > 0 and total >= hurdle and cov >= 0.8 and s_cons > 0
    failed = hi <= 0 or total < ctl_total
    return ("pass" if passed else "fail" if failed else "inconclusive"), total, cov


# --------------------------------------------------------------------------- version 3
def outages(rng, end: float, profile: str) -> tuple[list, list, float]:
    """The provider's outage calendar over [0, end + slack): sorted, merged (starts, ends), and the observed share."""
    up_h, down_min, long_per_day, long_h = AVAILABILITY[profile][:4]
    horizon = end + 2 * 86400
    iv, t = [], rng.exponential(up_h * 3600)
    while t < horizon:
        d = rng.exponential(down_min * 60)
        iv.append((t, t + d))
        t += d + rng.exponential(up_h * 3600)
    if long_per_day:
        for _ in range(rng.poisson(long_per_day * horizon / 86400)):
            s0 = rng.uniform(0, horizon)
            iv.append((s0, s0 + long_h * 3600))
    iv.sort()
    merged: list = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    starts, ends = [a for a, _ in merged], [b for _, b in merged]
    down = sum(max(0.0, min(b, end) - a) for a, b in merged if a < end)
    return starts, ends, 1.0 - down / end


def _up(starts: list, ends: list, t: np.ndarray) -> np.ndarray:
    i = np.searchsorted(np.asarray(starts), t, side="right") - 1
    e = np.asarray(ends)
    return ~((i >= 0) & (t < e[np.clip(i, 0, None)])) if len(starts) else np.ones_like(t, dtype=bool)


def _arm_quotes(rng, starts, ends, t_entry: np.ndarray, ret: np.ndarray, profile: str) -> tuple[np.ndarray, np.ndarray]:
    """For each signal in one arm: (a quote was available at entry, the time of the FIRST valid exit quote - the
    due time or a later 30 s retry within RETRY_S - or 0.0 if none was). Attempts run in time order: the provider's
    calendar, a pool that can't be quoted at all, and transient failures that block the pool for a streak."""
    q_t, q_p = AVAILABILITY[profile][4:6]
    lo, hi = STREAK_S
    n = len(t_entry)
    entry_ok = _up(starts, ends, t_entry) & (rng.random(n) >= q_t)
    p_dead = np.where(ret < -0.5, min(1.0, 3 * q_p), q_p) if profile in INFORMATIVE else np.full(n, q_p)
    dead = rng.random(n) < p_dead
    first = np.zeros(n)
    blocked = np.full(n, -np.inf)                     # a transient failure's streak: no quote before this
    for i in range(int(RETRY_S // ATTEMPT_S) + 1):
        a = t_entry + HOLD_S + ATTEMPT_S * i
        live = (first == 0) & ~dead & _up(starts, ends, a) & (a >= blocked)
        fail = live & (rng.random(n) < q_t)
        if fail.any():
            blocked[fail] = a[fail] + rng.uniform(lo, hi, int(fail.sum()))
        ok = live & ~fail
        first[ok] = a[ok]
    return entry_ok, first


DRIFT = {"centered": "1 + sigma clip(Z, +-c), c = min(4.5, 0.95 / sigma): a symmetric clip keeps the mean exactly 1, "
                     "the multiplier positive, and Z = 0 no move (the base case)",
         "positive": "exp(sigma Z): version 4's process, mean exp(sigma^2 / 2) > 1 (+2.2% over 15 min)",
         "negative": "exp(sigma Z - sigma^2): its mirror, mean exp(-sigma^2 / 2) < 1"}
LATE_CLIP = 4.5


def _late_multiplier(sigma: np.ndarray, z: np.ndarray, drift: str) -> np.ndarray:
    if drift == "centered":
        with np.errstate(divide="ignore"):
            c = np.minimum(LATE_CLIP, 0.95 / sigma)
        return 1 + sigma * np.clip(z, -c, c)
    if drift == "positive":
        return np.exp(sigma * z)
    if drift == "negative":
        return np.exp(sigma * z - sigma ** 2)
    raise ValueError(drift)


def _late_return(r: np.ndarray, extra: float, late_s: np.ndarray, z: np.ndarray, drift: str = "centered") -> np.ndarray:
    """A net return filled late_s after its due time: the gross price moved on by a draw of the hourly spread,
    sigma = LATE_SD x sqrt(late_s / 3600). Centered (the base), its expectation is the on-time return, so the delay
    itself adds no edge, and a Z of 0 leaves the price where it was. Version 4 used exp(sigma Z), which adds positive
    expected drift (a tenth review, 2026-10-07); it and its mirror run as labelled sensitivities, never chosen
    after the results. (The lognormal centering exp(sigma Z - sigma^2/2) would also be mean-preserving, but moves
    the Z = 0 path down by sigma^2/2; at sigma <= 0.21 the two spreads differ by under 0.1%.)"""
    gross = 1 + r + FEE + extra
    sigma = LATE_SD * np.sqrt(np.maximum(late_s, 0) / 3600)
    return gross * _late_multiplier(sigma, z, drift) - 1 - FEE - extra


def _run_arm(port: Portfolio, t_sig: np.ndarray, coin: list, r: np.ndarray, entry_ok: np.ndarray,
             first: np.ndarray, extra: float, z: np.ndarray, drift: str = "centered") -> None:
    """One arm's signals through its account, in time order: skipped without an entry quote; measured at the first
    valid exit quote's time and return; impaired when none came within the retries."""
    due = t_sig + DELAY_S + HOLD_S
    r_fill = np.where(first > due, _late_return(r, extra, first - due, z, drift), r)
    for i in range(len(t_sig)):
        if not entry_ok[i]:
            continue
        if first[i] > 0:
            port.try_enter(t_sig[i], coin[i], float(r_fill[i]), exit_at=float(first[i]))
        else:
            port.try_enter(t_sig[i], coin[i], None)


def _bootstrap(rng, x: np.ndarray, boots: int, block: int) -> np.ndarray:
    days = len(x)
    if block <= 1:
        return x[rng.integers(0, days, size=(boots, days))].mean(1)
    nb = -(-days // block)
    starts = rng.integers(0, days - block + 1, size=(boots, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(boots, -1)[:, :days]
    return x[idx].mean(1)


def one_test_v5(rng, days: int, win_frac: float, magnitude: float, ordinary: str, profile: str, extra: float,
                hurdle: float, boots: int, drift: str = "centered") -> dict:
    mu, sd = ORDINARY[ordinary]
    end = days * 86400
    p_win = min(WIN_EPISODES_PER_DAY * win_frac / EPISODES_PER_DAY, 1.0)
    t_sig, coin, win = [], [], []
    for d in range(days):
        n_ep = rng.poisson(EPISODES_PER_DAY * rng.gamma(4.0, 0.25))
        for e in range(n_ep):
            t = d * 86400 + rng.uniform(0, 86400)
            w = rng.random() < p_win
            for k in range(1 + rng.poisson(0.22)):
                t_sig.append(t + k * (7200 + rng.uniform(0, 7200)))
                coin.append((d, e))
                win.append(w and k == 0)
    order = np.argsort(t_sig, kind="stable")
    t_sig = np.asarray(t_sig)[order]
    coin = [coin[i] for i in order]
    win = np.asarray(win, dtype=bool)[order]
    n = len(t_sig)
    ordinary_r = np.exp(rng.normal(mu, sd, n)) - 1 - FEE - extra
    win_r = np.asarray(WIN_NET)[rng.integers(0, len(WIN_NET), n)] * magnitude - extra
    r_sig = np.where(win, win_r, ordinary_r)
    r_ctl = np.exp(rng.normal(*CONTROL, n)) - 1 - FEE - extra
    z_sig, z_ctl = rng.normal(size=n), rng.normal(size=n)
    starts, ends, observed = outages(rng, end, profile)
    t_entry = t_sig + DELAY_S
    s_entry, s_first = _arm_quotes(rng, starts, ends, t_entry, r_sig, profile)
    c_entry, c_first = _arm_quotes(rng, starts, ends, t_entry, r_ctl, profile)
    port, ctl = Portfolio(end), Portfolio(end)
    _run_arm(port, t_sig, coin, r_sig, s_entry, s_first, extra, z_sig, drift)
    _run_arm(ctl, t_sig, coin, r_ctl, c_entry, c_first, extra, z_ctl, drift)
    port.close()
    ctl.close()
    s_day, c_day = np.array(port.daily(days)), np.array(ctl.daily(days))
    cov = port.measured / port.attempted if port.attempted else 0.0
    c_cov = ctl.measured / ctl.attempted if ctl.attempted else 0.0
    total, ctl_total = float(s_day.sum()), float(c_day.sum())
    assert abs(total - (port.cash - BANK)) < 1e-6          # the primary series reconciles to cash
    out = {"total": total, "coverage": cov, "control_coverage": c_cov, "observed": observed,
           "control_total": ctl_total, "measured_only_total": float(sum(port.measured_daily(days))),
           "attempted": port.attempted, "entry_skipped": int((~s_entry).sum()),
           # of the exits the account actually executed (not every quote opportunity: a tenth review)
           "late_fill_share": port.late_exits / port.exits if port.exits else 0.0,
           "arms": {name: {"signals": n, "no_entry_quote": int((~ok).sum()), "attempted": a.attempted,
                           "skipped": dict(a.skips), "measured": a.measured, "impaired": a.trapped,
                           "executed_exits": a.exits, "late_exits": a.late_exits}
                    for name, a, ok in (("signal", port, s_entry), ("control", ctl, c_entry))}}
    invalid = observed < 0.8 or cov < 0.8 or c_cov < 0.8
    for name, block in (("verdict", 1), ("verdict_block3", 3)):
        m = _bootstrap(rng, s_day, boots, block)
        diff = _bootstrap(rng, s_day - c_day, boots, block)
        lo, hi, dlo = np.percentile(m, 5), np.percentile(m, 95), np.percentile(diff, 5)
        out[name] = ("invalid" if invalid else
                     "pass" if lo > 0 and dlo > 0 and total >= hurdle else
                     "fail" if hi <= 0 or total < ctl_total else "inconclusive")
    return out


def one_test_v4(rng, days, win_frac, magnitude, ordinary, profile, extra, hurdle, boots) -> dict:
    """Version 4's signature (the reviewer's contracts call it): the version-5 procedure, centered drift."""
    return one_test_v5(rng, days, win_frac, magnitude, ordinary, profile, extra, hurdle, boots)


VARIANTS = (("centered", "gross"), ("positive", "gross"), ("negative", "gross"), ("centered", "hard"),
            ("centered", "net+cap"))          # (late-price drift, risk policy); the first is the base


def _block_idx(rng, days: int, boots: int, block: int) -> np.ndarray:
    if block <= 1:
        return rng.integers(0, days, size=(boots, days))
    nb = -(-days // block)
    starts = rng.integers(0, days - block + 1, size=(boots, nb))
    return (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(boots, -1)[:, :days]


def _drawdown(day: np.ndarray) -> float:
    eq = np.concatenate([[0.0], np.cumsum(day)])
    return float((np.maximum.accumulate(eq) - eq).max())


def one_test_v6(rng, days: int, win_frac: float, magnitude: float, ordinary: str, profile: str, extra: float,
                hurdle: float, boots: int, variants=VARIANTS) -> dict:
    """Version 5's procedure, every variant on the SAME draws (paths, quotes, shocks, bootstrap resamples)."""
    mu, sd = ORDINARY[ordinary]
    end = days * 86400
    p_win = min(WIN_EPISODES_PER_DAY * win_frac / EPISODES_PER_DAY, 1.0)
    t_sig, coin, win = [], [], []
    for d in range(days):
        n_ep = rng.poisson(EPISODES_PER_DAY * rng.gamma(4.0, 0.25))
        for e in range(n_ep):
            t = d * 86400 + rng.uniform(0, 86400)
            w = rng.random() < p_win
            for k in range(1 + rng.poisson(0.22)):
                t_sig.append(t + k * (7200 + rng.uniform(0, 7200)))
                coin.append((d, e))
                win.append(w and k == 0)
    order = np.argsort(t_sig, kind="stable")
    t_sig = np.asarray(t_sig)[order]
    coin = [coin[i] for i in order]
    win = np.asarray(win, dtype=bool)[order]
    n = len(t_sig)
    ordinary_r = np.exp(rng.normal(mu, sd, n)) - 1 - FEE - extra
    win_r = np.asarray(WIN_NET)[rng.integers(0, len(WIN_NET), n)] * magnitude - extra
    r_sig = np.where(win, win_r, ordinary_r)
    r_ctl = np.exp(rng.normal(*CONTROL, n)) - 1 - FEE - extra
    z_sig, z_ctl = rng.normal(size=n), rng.normal(size=n)
    starts, ends, observed = outages(rng, end, profile)
    t_entry = t_sig + DELAY_S
    s_entry, s_first = _arm_quotes(rng, starts, ends, t_entry, r_sig, profile)
    c_entry, c_first = _arm_quotes(rng, starts, ends, t_entry, r_ctl, profile)
    idx = {1: _block_idx(rng, days, boots, 1), 3: _block_idx(rng, days, boots, 3)}
    out = {}
    for drift, risk in variants:
        port, ctl = Portfolio(end, risk=risk), Portfolio(end, risk=risk)
        _run_arm(port, t_sig, coin, r_sig, s_entry, s_first, extra, z_sig, drift)
        _run_arm(ctl, t_sig, coin, r_ctl, c_entry, c_first, extra, z_ctl, drift)
        port.close()
        ctl.close()
        s_day, c_day = np.array(port.daily(days)), np.array(ctl.daily(days))
        cov = port.measured / port.attempted if port.attempted else 0.0
        c_cov = ctl.measured / ctl.attempted if ctl.attempted else 0.0
        total, ctl_total = float(s_day.sum()), float(c_day.sum())
        assert abs(total - (port.cash - BANK)) < 1e-6
        top = sorted(port.pnls, reverse=True)[:3]
        r = {"total": total, "control_total": ctl_total, "coverage": cov, "control_coverage": c_cov,
             "observed": observed, "attempted": port.attempted, "skips": dict(port.skips),
             "stop_days": len(port.stop_at),
             "stop_time_of_day_h": float(np.median([t % 86400 for t in port.stop_at.values()]) / 3600)
             if port.stop_at else None,
             "worst_day": float(s_day.min()), "drawdown": _drawdown(s_day), "max_open": port.max_open_seen,
             "impaired": port.trapped, "impairments_sol": port.impairments,
             "top3_share": float(sum(top) / total) if total > 0 else None,
             "late_fill_share": port.late_exits / port.exits if port.exits else 0.0}
        invalid = observed < 0.8 or cov < 0.8 or c_cov < 0.8
        for name, block in (("verdict", 1), ("verdict_block3", 3)):
            m = s_day[idx[block]].mean(1)
            diff = (s_day - c_day)[idx[block]].mean(1)
            lo, hi, dlo = np.percentile(m, 5), np.percentile(m, 95), np.percentile(diff, 5)
            r[name] = ("invalid" if invalid else
                       "pass" if lo > 0 and dlo > 0 and total >= hurdle else
                       "fail" if hi <= 0 or total < ctl_total else "inconclusive")
        out[f"{drift}/{risk}"] = r
    return out


def wilson(k: int, n: int, z: float = 1.96) -> list:
    """95% interval for a simulated proportion (Monte Carlo uncertainty of a cell)."""
    if n == 0:
        return [None, None]
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


GRID = ("A1", "A2", "A3", "A4", "A5")


def cells(sims: int, null_sims: int) -> list[dict]:
    """Version 5: version 4's frozen grid with the centered late-price process (the base), plus the drift
    sensitivities on a fixed subset declared here before running: the nulls, and a quarter / half / the replay's
    winner rate with flat or pessimistic ordinary trades, at A2-A4, +0 cost, 90 days."""
    out = []
    for profile in GRID + ("A3 informative",):
        for extra in (0.0, 0.015):
            for days in (60, 90, 120):
                out.append(dict(scenario="null", win_frac=0.0, magnitude=1.0, ordinary="flat", profile=profile,
                                extra=extra, days=days, sims=null_sims, drift="centered"))
    for win_frac, name in ((0.125, "eighth"), (0.25, "quarter"), (0.5, "half"), (1.0, "replay")):
        for magnitude in (1.0, 0.5):
            for ordinary in ("pessimistic", "flat", "mild"):
                for profile in GRID:
                    for extra in (0.0, 0.015):
                        for days in (60, 90, 120):
                            out.append(dict(scenario=name, win_frac=win_frac, magnitude=magnitude, ordinary=ordinary,
                                            profile=profile, extra=extra, days=days, sims=sims, drift="centered"))
                for days in (60, 90, 120):                    # the informative-missingness stress, at +0 cost
                    out.append(dict(scenario=name, win_frac=win_frac, magnitude=magnitude, ordinary=ordinary,
                                    profile="A3 informative", extra=0.0, days=days, sims=sims, drift="centered"))
    for ordinary in ("pessimistic", "mild"):                  # no winners at all: ordinary trades only
        for profile in GRID:
            for days in (60, 90, 120):
                out.append(dict(scenario="no winners", win_frac=0.0, magnitude=1.0, ordinary=ordinary,
                                profile=profile, extra=0.0, days=days, sims=sims, drift="centered"))
    for drift in ("positive", "negative"):                    # the late-price drift sensitivities
        for profile in GRID + ("A3 informative",):
            for days in (60, 90, 120):
                out.append(dict(scenario="null", win_frac=0.0, magnitude=1.0, ordinary="flat", profile=profile,
                                extra=0.0, days=days, sims=null_sims, drift=drift))
        for win_frac, name in ((0.25, "quarter"), (0.5, "half"), (1.0, "replay")):
            for ordinary in ("pessimistic", "flat"):
                for profile in ("A2", "A3", "A4"):
                    out.append(dict(scenario=name, win_frac=win_frac, magnitude=1.0, ordinary=ordinary,
                                    profile=profile, extra=0.0, days=90, sims=sims, drift=drift))
    return out


def cells_v6(sims: int, null_sims: int) -> list[dict]:
    """Version 6's grid, declared before running: the null and a quarter / half / the replay's winner rate, flat or
    pessimistic ordinary trades, at A1-A4 (A5 is always INVALID), +0 / +1.5 / +2.8 points of cost, 90 days - each
    cell running every VARIANT on common random numbers."""
    out = []
    for profile in ("A1", "A2", "A3", "A4"):
        for extra in (0.0, 0.015, 0.028):
            out.append(dict(scenario="null", win_frac=0.0, magnitude=1.0, ordinary="flat", profile=profile,
                            extra=extra, days=90, sims=null_sims))
            for win_frac, name in ((0.25, "quarter"), (0.5, "half"), (1.0, "replay")):
                for ordinary in ("flat", "pessimistic"):
                    out.append(dict(scenario=name, win_frac=win_frac, magnitude=1.0, ordinary=ordinary,
                                    profile=profile, extra=extra, days=90, sims=sims))
    return out


def run_cell_v6(args) -> dict:
    i, cell, hurdle, boots = args
    rng = np.random.default_rng(np.random.SeedSequence(SEED + 6, spawn_key=(i,)))
    res = [one_test_v6(rng, cell["days"], cell["win_frac"], cell["magnitude"], cell["ordinary"], cell["profile"],
                       cell["extra"], hurdle, boots) for _ in range(cell["sims"])]
    n = len(res)
    base = f"{VARIANTS[0][0]}/{VARIANTS[0][1]}"
    row = {**cell, "cell": i, "variants": {}}
    for drift, risk in VARIANTS:
        v = f"{drift}/{risk}"
        rs = [r[v] for r in res]
        out = {}
        for key in ("verdict", "verdict_block3"):
            vs = [r[key] for r in rs]
            pre = "" if key == "verdict" else "block3_"
            for k in ("pass", "fail", "inconclusive", "invalid"):
                out[pre + k] = round(vs.count(k) / n, 4)
                out[pre + k + "_ci95"] = wilson(vs.count(k), n)
        if v != base:                                     # the paired difference from the base, same draws
            a = np.array([r[v]["verdict"] == "pass" for r in res], dtype=float)
            b = np.array([r[base]["verdict"] == "pass" for r in res], dtype=float)
            d = a - b
            se = float(d.std(ddof=1) / math.sqrt(n)) if n > 1 else 0.0
            out["paired_pass_diff"] = round(float(d.mean()), 4)
            out["paired_pass_diff_ci95"] = [round(float(d.mean()) - 1.96 * se, 4), round(float(d.mean()) + 1.96 * se, 4)]
            out["discordant"] = {"variant_only": int((d > 0).sum()), "base_only": int((d < 0).sum())}
            out["paired_total_diff_median"] = round(float(np.median([r[v]["total"] - r[base]["total"] for r in res])), 4)
        for k in ("total", "attempted", "stop_days", "worst_day", "drawdown", "max_open", "impaired", "coverage",
                  "observed", "late_fill_share"):
            out["median_" + k] = round(float(np.median([r[k] for r in rs])), 4)
        tod = [r["stop_time_of_day_h"] for r in rs if r["stop_time_of_day_h"] is not None]
        out["median_stop_time_of_day_h"] = round(float(np.median(tod)), 2) if tod else None
        t3 = [r["top3_share"] for r in rs if r["top3_share"] is not None]
        out["median_top3_share_when_positive"] = round(float(np.median(t3)), 3) if t3 else None
        sk: dict = {}
        for r in rs:
            for why, c in r["skips"].items():
                sk[why] = sk.get(why, 0) + c
        out["skips_summed"] = sk
        row["variants"][v] = out
    return row


def run_cell(args) -> dict:
    i, cell, hurdle, boots = args
    rng = np.random.default_rng(np.random.SeedSequence(SEED, spawn_key=(i,)))
    res = [one_test_v5(rng, cell["days"], cell["win_frac"], cell["magnitude"], cell["ordinary"], cell["profile"],
                       cell["extra"], hurdle, boots, cell["drift"]) for _ in range(cell["sims"])]
    row = {**cell, "cell": i}
    n = len(res)
    for key in ("verdict", "verdict_block3"):
        v = [r[key] for r in res]
        pre = "" if key == "verdict" else "block3_"
        for k in ("pass", "fail", "inconclusive", "invalid"):
            row[pre + k] = round(v.count(k) / n, 4)
            row[pre + k + "_ci95"] = wilson(v.count(k), n)
    for k in ("total", "measured_only_total", "coverage", "control_coverage", "observed", "control_total",
              "late_fill_share"):
        row["median_" + k] = round(float(np.median([r[k] for r in res])), 4)
    arms = {}                                                 # summed over the cell's simulations, both arms
    for r in res:
        for name, a in r["arms"].items():
            t = arms.setdefault(name, {"skipped": {}})
            for k, v in a.items():
                if k == "skipped":
                    for why, n2 in v.items():
                        t["skipped"][why] = t["skipped"].get(why, 0) + n2
                else:
                    t[k] = t.get(k, 0) + v
    row["arms"] = arms
    return row


def provenance(paths: list[Path]) -> dict:
    root = Path(__file__).resolve().parents[1]

    def git(*a):
        try:
            return subprocess.run(["git", *a], capture_output=True, text=True, timeout=10, cwd=root).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    diff = git("diff", "HEAD") + git("status", "--porcelain")
    import importlib.metadata as md
    return {"code_revision": git("rev-parse", "HEAD").strip(),
            "uncommitted_changes_sha256": hashlib.sha256(diff.encode()).hexdigest() if diff.strip() else "",
            "uncommitted_diff": diff if diff.strip() else "",
            "files_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            "packages": {"numpy": md.version("numpy")}, "python": sys.version.split()[0]}


def main_v6(a) -> int:
    root = Path(__file__).resolve().parents[1]
    prov = provenance([Path(__file__).resolve(), root / "meme_trader/sniper/t9_portfolio.py"])   # as run, not as left
    grid = cells_v6(a.sims, a.null_sims)
    jobs = [(i, c, a.hurdle, a.boots) for i, c in enumerate(grid)]
    if a.procs > 1:
        from multiprocessing import Pool
        with Pool(a.procs) as pool:
            rows = pool.map(run_cell_v6, jobs, chunksize=1)
    else:
        rows = [run_cell_v6(j) for j in jobs]
    for r in rows:
        for v, x in r["variants"].items():
            pair = (f" | paired {x['paired_pass_diff']:+.3f} {x['paired_pass_diff_ci95']}" if "paired_pass_diff" in x
                    else "")
            print(f"{r['scenario']:8} {r['ordinary']:11} {r['profile']} cost+{r['extra']:.3f} {v:17}: pass "
                  f"{x['pass']:.3f} {x['pass_ci95']}{pair} | trades {x['median_attempted']:.0f}, stop days "
                  f"{x['median_stop_days']:.0f}, total {x['median_total']:+.2f}, worst day {x['median_worst_day']:+.2f}, "
                  f"drawdown {x['median_drawdown']:.2f}", flush=True)
    if a.json:
        doc = {"version": 6, "seed": SEED + 6, "seeding": "numpy SeedSequence(seed, spawn_key=(cell index,)) per cell; "
               "within a cell every variant runs on the same draws (common random numbers)",
               "variants": [f"{d}/{r}" for d, r in VARIANTS], "base_variant": f"{VARIANTS[0][0]}/{VARIANTS[0][1]}",
               "risk_policies": {"gross": "entries halt once the day's gross losses + impairments + overdue reservations "
                                          "reach DAY_STOP (the registration's)",
                                 "hard": "admit only if the day's net loss + every open position's total loss + this "
                                         "one's stays within DAY_STOP",
                                 "net+cap": f"entries halt once the day's net loss + overdue reservations reach DAY_STOP, "
                                            f"or gross losses reach {tp.OUTER_CAP} x DAY_STOP"},
               "sims": a.sims, "null_sims": a.null_sims, "boots": a.boots, "hurdle_sol": a.hurdle, **prov,
               "constants": {"bank": tp.BANK, "size": tp.SIZE, "max_open": tp.MAX_OPEN, "day_stop": tp.DAY_STOP,
                             "outer_cap": tp.OUTER_CAP, "hold_s": tp.HOLD_S, "delay_s": tp.DELAY_S,
                             "retry_s": tp.RETRY_S, "fee": FEE, "extra_costs": [0.0, 0.015, 0.028],
                             "availability": AVAILABILITY, "late_sd_per_hour": LATE_SD, "late_drift": DRIFT,
                             "late_clip": LATE_CLIP},
               "paired_difference": "per simulation, pass(variant) - pass(base) on the same draws; mean with a 95% "
                                    "normal interval, and the discordant counts",
               "rows": rows}
        data = json.dumps(doc, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
        Path(a.json).write_text(data)
        Path(a.json + ".sha256").write_text(hashlib.sha256(data.encode()).hexdigest() + "  " + Path(a.json).name + "\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=150)
    ap.add_argument("--null-sims", type=int, default=500)
    ap.add_argument("--boots", type=int, default=400)
    ap.add_argument("--hurdle", type=float, default=1.0, help="SOL the total must reach (the registration's H)")
    ap.add_argument("--procs", type=int, default=1)
    ap.add_argument("--json", default="")
    ap.add_argument("--version", type=int, default=6, choices=(5, 6))
    a = ap.parse_args(argv)
    if a.version == 6:
        return main_v6(a)
    root = Path(__file__).resolve().parents[1]
    prov = provenance([Path(__file__).resolve(), root / "meme_trader/sniper/t9_portfolio.py"])   # as run, not as left
    grid = cells(a.sims, a.null_sims)
    jobs = [(i, c, a.hurdle, a.boots) for i, c in enumerate(grid)]
    if a.procs > 1:
        from multiprocessing import Pool
        with Pool(a.procs) as pool:
            rows = pool.map(run_cell, jobs, chunksize=1)
    else:
        rows = [run_cell(j) for j in jobs]
    for r in rows:
        print(f"{r['scenario']:10} x{r['magnitude']:.1f} {r['ordinary']:11} {r['profile']:14} cost+{r['extra']:.3f} "
              f"{r['days']:3}d {r['drift']:8}: pass {r['pass']:.3f} {r['pass_ci95']} fail {r['fail']:.2f} inconcl "
              f"{r['inconclusive']:.2f} invalid {r['invalid']:.2f} | block3 pass {r['block3_pass']:.3f} | total "
              f"{r['median_total']:+.2f} SOL, coverage {r['median_coverage']:.3f}, observed {r['median_observed']:.3f}",
              flush=True)
    if a.json:
        doc = {"version": 5, "seed": SEED, "seeding": "numpy SeedSequence(seed, spawn_key=(cell index,)) per cell",
               "sims": a.sims, "null_sims": a.null_sims, "boots": a.boots, "hurdle_sol": a.hurdle, **prov,
               "constants": {"bank": tp.BANK, "size": tp.SIZE, "max_open": tp.MAX_OPEN, "day_stop": tp.DAY_STOP,
                             "hold_s": tp.HOLD_S, "delay_s": tp.DELAY_S, "retry_s": tp.RETRY_S, "fee": FEE,
                             "attempt_s": ATTEMPT_S, "episodes_per_day": EPISODES_PER_DAY,
                             "win_episodes_per_day": WIN_EPISODES_PER_DAY, "win_net": WIN_NET, "ordinary": ORDINARY,
                             "control": CONTROL, "availability": AVAILABILITY,
                             "availability_fields": ["mean up h", "mean outage min", "long outages a day",
                                                     "long outage h", "transient failure per attempt",
                                                     "unquotable pool"],
                             "grid": GRID, "informative": sorted(INFORMATIVE), "streak_s": STREAK_S,
                             "late_sd_per_hour": LATE_SD, "late_drift": DRIFT, "late_clip": LATE_CLIP},
               "primary_statistic": "economic daily P&L (Portfolio.daily): measured exits + impairments at zero "
                                    "recovery and fees, on the day recognized; reconciles to cash",
               "verdict_rule": "INVALID if observed hours < 80% or either arm's coverage < 80%; PASS if the day bootstrap's 5th "
                               "percentile of mean daily P&L > 0, of mean daily (signal - control) > 0, and total >= "
                               "H; FAIL if its 95th percentile <= 0 or total < control total; else INCONCLUSIVE",
               "control_policy": "an independent synthetic draw per signal (no revival), through the same provider "
                                 "outage calendar with its own transient/pool failures and the same retry clock; real "
                                 "matched-pool controls need the runner's pre-decision data",
               "exit_clock": "exits fill at the first valid quote (due time or a 30 s retry within RETRY_S), at the "
                             "return then; cash, the UTC day and the risk reservation follow that time",
               "availability_note": "declared profiles, not estimates: replace them with a quote observer's "
                                    "reason-coded logs",
               "rows": rows}
        data = json.dumps(doc, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
        Path(a.json).write_text(data)
        Path(a.json + ".sha256").write_text(hashlib.sha256(data.encode()).hexdigest() + "  " + Path(a.json).name + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
