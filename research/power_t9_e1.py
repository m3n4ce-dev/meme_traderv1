"""Power, null and stress simulation of the WHOLE proposed T9-E1 procedure, run through the same account model as
the paper runner (meme_trader/sniper/t9_portfolio.py).

    python research/power_t9_e1.py [--sims 150] [--boots 400] [--hurdle 1.0] [--json out.json]     (needs numpy)

Version 2 (a seventh review, 2026-10-07): the first version re-implemented the account and lost its bankroll, its
clock across midnight and the capital tied up in unmeasured exits; it ran at H = 0, not the proposed 1 SOL; and its
missingness was 2-5% independent, where the historical replay had 17% of entered trades unmeasured.

Each simulated test, over `days` UTC days:
- revival episodes per day ~ Poisson(35.4 x a day factor ~ Gamma(4, 1/4)), each 1 + Poisson(0.22) signals, 2-4 h apart
  (episode arrivals and signals per episode are calibrated separately from the corrected replay);
- every signal goes through `Portfolio.try_enter` (9 SOL, 0.25 SOL a trade, 4 open, one per coin, a 0.5 SOL daily
  realized-loss stop, nothing that can't close inside the window); exits land on their own UTC day;
- a winner episode's first trade returns R NET (+5.4 = +540%); other trades draw log(1 + r + FEE) ~ N(mu, sd);
- an exit is unmeasured with probability u, independently, or - `cluster` - concentrated in outage days: with
  probability `cluster` a day is an outage day whose trades are unmeasured with probability min(1, u / cluster);
- each signal's control runs on its own identical account: log gross ~ N(-0.02, 0.15), never a revival.
Verdict, as registered: PASS needs all of the day bootstrap's 5th percentile of mean daily P&L > 0, of mean daily
(signal - control) > 0, total >= H, coverage >= 80% and the conservative total > 0; FAIL if the 95th percentile of
mean daily P&L <= 0 or the total is below the control's; INCONCLUSIVE otherwise.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meme_trader.sniper import t9_portfolio as tp  # noqa: E402
from meme_trader.sniper.t9_portfolio import BANK, MAX_OPEN, SIZE, Portfolio  # noqa: E402

__all__ = ["one_test", "wilson", "BANK", "MAX_OPEN", "SIZE"]      # (the account constants, for callers)

EPISODES_PER_DAY = 35.4              # the corrected replay, primary-like rule, ~3.1 days of coverage
WIN_EPISODES_PER_DAY = 1.0           # its top 3 episodes in that time (optimistic: selected on the same days)
WIN_NET = (5.4, 8.31, 14.44)         # their winning trades' NET returns
FEE = tp.FEE
CONTROL = (-0.02, 0.15)
ORDINARY = {"base": (-0.0758, 0.4164), "pessimistic": (-0.1368, 0.4164),
            "null": (math.log(1 + FEE) - 0.4164 ** 2 / 2, 0.4164)}      # breakeven after costs, the replay's spread


def one_test(rng, days, win_per_day, ordinary, extra, u, hurdle, boots, cluster=0.0):
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


def wilson(k: int, n: int, z: float = 1.96) -> list:
    """95% interval for a simulated proportion (Monte Carlo uncertainty of a cell)."""
    if n == 0:
        return [None, None]
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=150)
    ap.add_argument("--boots", type=int, default=400)
    ap.add_argument("--hurdle", type=float, default=1.0, help="SOL the total must reach (the registration's H)")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    rng = np.random.default_rng(20261007)
    scenarios = [("null", 0.0, "null"), ("quarter", 0.25, "base"), ("half", 0.5, "base"), ("replay", 1.0, "base"),
                 ("half", 0.5, "pessimistic")]
    coverage = [("5% independent", 0.05, 0.0), ("17% in outage days", 0.17, 0.25)]
    rows = []
    for name, frac, ordinary in scenarios:
        for cov_name, u, cluster in coverage:
            for extra in (0.0, 0.015):
                for days in (60, 90, 120):
                    out = [one_test(rng, days, WIN_EPISODES_PER_DAY * frac, ordinary, extra, u, a.hurdle, a.boots,
                                    cluster) for _ in range(a.sims)]
                    v = [o[0] for o in out]
                    n = len(v)
                    r = {"scenario": name, "winners_per_day": WIN_EPISODES_PER_DAY * frac, "ordinary": ordinary,
                         "missing": cov_name, "extra_cost": extra, "days": days, "sims": n,
                         **{k: round(v.count(k) / n, 3) for k in ("pass", "fail", "inconclusive")},
                         **{f"{k}_ci95": wilson(v.count(k), n) for k in ("pass", "fail", "inconclusive")},
                         "median_total_sol": round(float(np.median([o[1] for o in out])), 2),
                         "median_coverage": round(float(np.median([o[2] for o in out])), 3)}
                    rows.append(r)
                    print(f"{name:8} {ordinary:11} {cov_name:19} cost+{extra:.3f} {days:3}d: pass {r['pass']:.2f} "
                          f"{r['pass_ci95']} fail {r['fail']:.2f} inconcl {r['inconclusive']:.2f} | total "
                          f"{r['median_total_sol']:+.2f} SOL, coverage {r['median_coverage']:.2f}", flush=True)
    if a.json:
        try:
            rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                                 cwd=Path(__file__).parent).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            rev = ""
        import importlib.metadata as md
        with open(a.json, "w") as f:
            json.dump({"version": 2, "seed": 20261007, "sims": a.sims, "boots": a.boots, "hurdle_sol": a.hurdle,
                       "code_revision": rev, "packages": {"numpy": md.version("numpy")}, "python": sys.version.split()[0],
                       "constants": {"bank": tp.BANK, "size": tp.SIZE, "max_open": tp.MAX_OPEN, "day_stop": tp.DAY_STOP,
                                     "hold_s": tp.HOLD_S, "delay_s": tp.DELAY_S, "retry_s": tp.RETRY_S, "fee": FEE,
                                     "episodes_per_day": EPISODES_PER_DAY, "win_episodes_per_day": WIN_EPISODES_PER_DAY,
                                     "win_net": WIN_NET, "ordinary": ORDINARY, "control": CONTROL},
                       "control_policy": "an independent synthetic draw per signal (no revival); real matched-pool "
                                         "controls need the runner's pre-decision data",
                       "rows": rows}, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
