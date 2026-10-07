"""How many days a prospective T9-E1 test needs (a fifth review, 2026-10-07: "simulate uncertainty/power under several
plausible rare-winner frequencies and magnitudes" before registering).

    python research/power_t9.py [--sims 400] [--boots 500] [--json out.json]      (needs numpy)

The model, from the exploratory replay (2026-10-04..06; +40% in 5 min on 4x volume, fills 60 s late, hold 1 h,
1.2% round-trip cost): per trade, either an ordinary outcome - (1 + r) lognormal with median 0.904 and mean 0.997
(the replay's median -9.6% and its average without the best 3, -0.3%) - or, with probability p, a revival of +M
(the replay's three: +664% to +1318%). Trades per day are Poisson. Each simulated test sums 0.25 SOL trades by
calendar day and passes when a day-clustered bootstrap's 5th percentile of the mean daily P&L is above zero - the
project's edge check. A cell is also run at the p that makes the expected return exactly zero (the false-positive
rate) and with 1.5% extra cost per trade (stress). Deterministic (seeded).
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np

SIZE = 0.25
BASE_MEDIAN, BASE_MEAN = 0.904, 0.997                 # of (1 + r) for ordinary trades
SIGMA = math.sqrt(2 * math.log(BASE_MEAN / BASE_MEDIAN))
MU = math.log(BASE_MEDIAN)


def simulate(rng, days: int, per_day: float, p: float, mult: float, extra_cost: float, sims: int, boots: int):
    n = rng.poisson(per_day, size=(sims, days))
    daily = np.zeros((sims, days))
    top_share = np.zeros(sims)
    for s in range(sims):
        k = int(n[s].sum())
        r = np.exp(rng.normal(MU, SIGMA, k)) - 1 - extra_cost
        win = rng.random(k) < p
        r[win] = mult - extra_cost
        pnl = r * SIZE
        day = np.repeat(np.arange(days), n[s])
        daily[s] = np.bincount(day, weights=pnl, minlength=days)
        tot = pnl.sum()
        top_share[s] = pnl.max() / tot if tot > 0 and k else np.nan
    idx = rng.integers(0, days, size=(boots, days))
    lo = np.array([np.percentile(daily[s][idx].mean(1), 5) for s in range(sims)])
    total = daily.sum(1)
    return {"pass": float((lo > 0).mean()), "p_total_positive": float((total > 0).mean()),
            "median_total_sol": round(float(np.median(total)), 2),
            "p10_total_sol": round(float(np.percentile(total, 10)), 2),
            "median_top_trade_share": round(float(np.nanmedian(top_share)), 2) if np.isfinite(top_share).any() else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--boots", type=int, default=500)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    rng = np.random.default_rng(20261007)
    base_mean = BASE_MEAN - 1
    rows = []
    for per_day in (10, 25):
        for mult in (3.0, 6.0, 9.0):
            null_p = -base_mean / (mult - base_mean)    # expected return exactly zero
            for p in (null_p, 0.005, 0.01, 0.02):
                for extra in (0.0, 0.015):
                    ev = (1 - p) * (base_mean - extra) + p * (mult - extra)
                    for days in (28, 60, 120):
                        r = simulate(rng, days, per_day, p, mult, extra, a.sims, a.boots)
                        rows.append({"trades_per_day": per_day, "winner_x": mult, "winner_p": round(p, 4),
                                     "null": p == null_p, "extra_cost": extra, "ev_per_trade_pct": round(ev * 100, 2),
                                     "days": days, **r})
    print(f"{'trades/d':>8} {'win':>5} {'p':>7} {'cost+':>6} {'EV/trade':>9} {'days':>5} {'pass':>6} {'P(tot>0)':>9} "
          f"{'median SOL':>10} {'p10 SOL':>8} {'top share':>9}")
    for r in rows:
        tag = " (null)" if r["null"] else ""
        print(f"{r['trades_per_day']:>8} {r['winner_x']:>4.0f}x {r['winner_p']:>7.4f} {r['extra_cost']:>6.3f} "
              f"{r['ev_per_trade_pct']:>8.2f}% {r['days']:>5} {r['pass']:>6.2f} {r['p_total_positive']:>9.2f} "
              f"{r['median_total_sol']:>10} {r['p10_total_sol']:>8} {str(r['median_top_trade_share']):>9}{tag}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"model": {"size_sol": SIZE, "base_median": BASE_MEDIAN, "base_mean": BASE_MEAN,
                                 "sigma": SIGMA, "seed": 20261007, "sims": a.sims, "boots": a.boots}, "rows": rows},
                      f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
