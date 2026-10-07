"""Power, null and stress simulation of the WHOLE proposed T9-E1 procedure (a sixth review, 2026-10-07: the first
simulation, power_t9.py, modelled one gate on independent trades - not the registered test).

    python research/power_t9_e1.py [--sims 200] [--boots 400] [--json out.json]        (needs numpy)

Each simulated test runs the registered procedure day by day:
- revival episodes arrive per day ~ Poisson(rate x a day factor ~ Gamma(4, 1/4)): busy and quiet days, not independent
  trades; an episode gives 1 + Poisson(0.22) signals at least 2 h apart (the replay: 1.22 a coin);
- a signal enters 60 s later for 0.25 SOL and exits after 1 h, inside a 9 SOL account with at most 4 open positions,
  one per coin, and no new entries after 0.5 SOL of realized losses in the UTC day;
- a winner episode's first trade returns +R net (R is the NET return: +5.0 = +500%, proceeds 6x the cost); others draw
  from the ordinary distribution, fitted to the corrected replay's trades outside its top 3 episodes (log of gross
  return ~ N(-0.076, 0.416): median -7%, mean +1%), or a pessimistic one with mean -5%;
- each signal gets a matched control on a separate identical account: ordinary coins with no revival (log gross ~
  N(-0.02, 0.15));
- a trade's exit is unmeasured with probability u: excluded from the primary P&L but counted in coverage, and valued at
  zero recovery minus fees in the conservative treatment.

Verdict, as registered:
  PASS if all of: the day bootstrap's 5th percentile of mean daily P&L > 0; of mean daily (signal - control) > 0;
  total net P&L >= the hurdle; coverage >= 80%; and the conservative treatment's total > 0.
  FAIL if the 95th percentile of mean daily P&L <= 0, or the total is below the control's.
  INCONCLUSIVE otherwise.
Scenarios: null (no winners; the signal's trades break even after costs, with the replay's spread), and winner frequencies of a
quarter, a half and all of the replay's (one winner episode a day), with +1.5% extra cost per trade as the stress.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np

SIZE, BANK, MAX_OPEN, DAY_STOP = 0.25, 9.0, 4, 0.5
HOLD_S, DELAY_S, COOLDOWN_S = 3600, 60, 7200
EPISODES_PER_DAY = 35.4              # the corrected replay, primary-like rule
WIN_EPISODES_PER_DAY = 1.0           # its top 3 episodes in ~3.1 days
WIN_NET = (5.4, 8.31, 14.44)         # their winning trades' net returns
FEE = 0.012                          # round trip
CONTROL = (-0.02, 0.15)
# null: breakeven after costs (mean gross 1 + FEE), with the replay's spread - no edge, lottery-shaped noise
ORDINARY = {"base": (-0.0758, 0.4164), "pessimistic": (-0.1368, 0.4164),
            "null": (math.log(1 + FEE) - 0.4164 ** 2 / 2, 0.4164)}


def one_test(rng, days, win_per_day, ordinary, extra, u, hurdle, boots):
    mu, sd = ORDINARY[ordinary]
    s_day, c_day = np.zeros(days), np.zeros(days)
    s_cons = 0.0
    attempted = measured = 0
    for d in range(days):
        n_ep = rng.poisson(EPISODES_PER_DAY * rng.gamma(4.0, 0.25))
        p_win = min(win_per_day / EPISODES_PER_DAY, 1.0)
        sigs = []
        for e in range(n_ep):
            t = rng.uniform(0, 86400)
            win = rng.random() < p_win
            for k in range(1 + rng.poisson(0.22)):
                sigs.append((t + k * (COOLDOWN_S + rng.uniform(0, 7200)), e, win and k == 0))
        sigs = [x for x in sigs if x[0] < 86400]
        sigs.sort()
        for book, is_ctl in ((s_day, False), (c_day, True)):
            open_, lost = [], 0.0
            for t, ep, win in sigs:
                still = []
                for (te, ep2, pnl) in open_:
                    if te <= t:
                        book[d] += pnl
                        lost += max(-pnl, 0.0)
                    else:
                        still.append((te, ep2, pnl))
                open_ = still
                if len(open_) >= MAX_OPEN or any(o[1] == ep for o in open_) or lost >= DAY_STOP:
                    continue
                if is_ctl:
                    r = math.exp(rng.normal(*CONTROL)) - 1 - FEE - extra
                    open_.append((t + DELAY_S + HOLD_S, ep, r * SIZE))
                    continue
                attempted += 1
                r = (rng.choice(WIN_NET) - extra) if win else (math.exp(rng.normal(mu, sd)) - 1 - FEE - extra)
                if rng.random() < u:                   # its exit wasn't measured
                    s_cons += (-1.0 - FEE - extra) * SIZE
                    continue
                measured += 1
                s_cons += r * SIZE
                open_.append((t + DELAY_S + HOLD_S, ep, r * SIZE))
            for (te, ep2, pnl) in open_:              # exits after midnight land on the next day (or this, if last)
                book[min(d + 1, days - 1)] += pnl
    idx = rng.integers(0, days, size=(boots, days))
    m = s_day[idx].mean(1)
    diff = (s_day - c_day)[idx].mean(1)
    lo, hi, dlo = np.percentile(m, 5), np.percentile(m, 95), np.percentile(diff, 5)
    cov = measured / attempted if attempted else 0.0
    total, ctl = s_day.sum(), c_day.sum()
    passed = lo > 0 and dlo > 0 and total >= hurdle and cov >= 0.8 and s_cons > 0
    failed = hi <= 0 or total < ctl
    return ("pass" if passed else "fail" if failed else "inconclusive"), total, cov


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=200)
    ap.add_argument("--boots", type=int, default=400)
    ap.add_argument("--hurdle", type=float, default=0.0, help="SOL the total must reach (the economic gate)")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    rng = np.random.default_rng(20261007)
    rows = []
    scenarios = [("null", 0.0, "null"), ("quarter", 0.25, "base"), ("half", 0.5, "base"),
                 ("replay", 1.0, "base"), ("half", 0.5, "pessimistic"), ("replay", 1.0, "pessimistic")]
    for name, frac, ordinary in scenarios:
        for extra, u in ((0.0, 0.02), (0.015, 0.05)):
            for days in (60, 90, 120):
                out = [one_test(rng, days, WIN_EPISODES_PER_DAY * frac, ordinary, extra, u, a.hurdle, a.boots)
                       for _ in range(a.sims)]
                v = [o[0] for o in out]
                rows.append({"scenario": name, "winners_per_day": WIN_EPISODES_PER_DAY * frac, "ordinary": ordinary,
                             "extra_cost": extra, "unmeasured": u, "days": days, "sims": a.sims,
                             "pass": round(v.count("pass") / len(v), 3), "fail": round(v.count("fail") / len(v), 3),
                             "inconclusive": round(v.count("inconclusive") / len(v), 3),
                             "median_total_sol": round(float(np.median([o[1] for o in out])), 2)})
                r = rows[-1]
                print(f"{name:8} {r['winners_per_day']:4.2f}/d {ordinary:12} cost+{extra:.3f} u={u:.2f} {days:3}d: "
                      f"pass {r['pass']:.2f} fail {r['fail']:.2f} inconclusive {r['inconclusive']:.2f} | "
                      f"median total {r['median_total_sol']:+.2f} SOL", flush=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"seed": 20261007, "hurdle_sol": a.hurdle, "rows": rows}, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
