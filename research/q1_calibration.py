"""Calibrating Q1's availability gate under common provider outages (a thirteenth review, R13-D).

    python research/q1_calibration.py [--windows 400] [--procs 8] [--json out.json]           (needs numpy)

Q1 asks whether the price watcher's exit quotes are available >= 95% eventually (>= 90% first try). Its first gate
bootstrapped (pool, UTC day) clusters - but a provider outage hits every pool at once, so pools aren't independent
evidence, and an all-success sample bootstraps to a bound of 1.0. This simulates whole 14-day windows under COMMON
outage processes and runs the gate code itself (`meme_trader.sniper.q1`: `block_bound`, `cluster_lower`) on them.

The availability process (declared here, before Q1's window - its parameters are a stress family, not estimates):
- one provider timeline shared by every pool and cell: alternating up / down periods, exponential, with mean outage
  D (2 min .. 24 h) and a long-run down share u; plus Sol's day model (each UTC day wholly up or down);
- each quote attempt also fails independently with probability eps (transient errors);
- jobs arrive in bursts (one signal fires 1-3 rules on the same pool at once) at an uneven, time-varying rate,
  ~25 matured exits a day in each of the 8 exit cells (arm x delay x hold), on ~60 pools of unequal popularity;
- an exit is tried at its due time and every 30 s for 15 min (Q1's retry rule): EVENTUAL success if any try lands
  in an up period and isn't a transient failure; FIRST-TRY success if the first one does.
TRUE availability is each scenario's job-weighted success rate over many windows. A gate's false-pass rate is its
pass rate in scenarios whose true availability is below target; its power, its pass rate where it's comfortably above.
Synthetic: says what a gate can certify under each process, nothing about the real provider's uptime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meme_trader.sniper import q1  # noqa: E402

DAYS, CELLS, PER_DAY, POOLS = 14, 8, 25, 60
RETRY_EVERY, RETRY_FOR = 30, 900
SEED = 20261008
BLOCKS = (1, 3, 6, 24)


def timeline(rng, days: int, D: float, u: float, day_model: bool):
    """Down intervals [(start, end)] in seconds over the window (+ a day's margin for retries)."""
    end = (days + 1) * 86400
    if u <= 0:
        return np.empty((0, 2))
    if day_model:
        down = rng.random(days + 1) < u
        return np.array([(d * 86400, (d + 1) * 86400) for d in range(days + 1) if down[d]]).reshape(-1, 2)
    up_mean = D * (1 - u) / u
    t, out = 0.0, []
    t += rng.exponential(up_mean) * rng.random()                 # start somewhere in an up period
    while t < end:
        d = rng.exponential(D)
        out.append((t, t + d))
        t += d + rng.exponential(up_mean)
    return np.array(out).reshape(-1, 2)


def is_down(times: np.ndarray, down: np.ndarray) -> np.ndarray:
    if len(down) == 0:
        return np.zeros(times.shape, dtype=bool)
    i = np.searchsorted(down[:, 0], times, side="right") - 1
    ok = i >= 0
    res = np.zeros(times.shape, dtype=bool)
    res[ok] = times[ok] < down[i[ok], 1]
    return res


def jobs(rng, days: int):
    """Exit jobs per cell: bursty signals at a time-varying rate, each firing 1-3 jobs on one pool at once."""
    out = []
    pop = 1 / np.arange(1, POOLS + 1)
    pop /= pop.sum()
    for cell in range(CELLS):
        hours = days * 24
        rate = PER_DAY / 24 * rng.gamma(2.0, 0.5, hours)         # uneven: some hours busy, some quiet
        for h in range(hours):
            for _ in range(rng.poisson(rate[h] / 1.6)):
                t = h * 3600 + rng.uniform(0, 3600)
                pool = int(rng.choice(POOLS, p=pop))
                for k in range(1 + rng.binomial(2, 0.3)):
                    out.append((cell, t + k * 0.5, pool))
    return out


def outcomes(rng, js, down, eps: float):
    """(first-try ok, eventual ok) per job, Q1's retry clock."""
    t = np.array([j[1] for j in js])
    tries = t[:, None] + np.arange(0, RETRY_FOR + 1, RETRY_EVERY)[None, :]
    good = ~is_down(tries, down) & (rng.random(tries.shape) >= eps)
    return good[:, 0], good.any(axis=1)


def gates(js, first, event):
    """Every exit cell's statistics under each gate, from q1's own functions; the window passes a gate when all 8
    cells meet both targets."""
    res = {f"block_{h}h": True for h in BLOCKS}
    res["pool_day_bootstrap"] = True
    for cell in range(CELLS):
        idx = [i for i, j in enumerate(js) if j[0] == cell]
        rows = [{"due": js[i][1]} for i in idx]
        for when, ok, target in (("eventual", event, q1.TARGETS["eventual"]), ("first_try", first, q1.TARGETS["first_try"])):
            hit = [bool(ok[i]) for i in idx]
            for h in BLOCKS:
                lo = q1.block_bound(rows, hit, h)["lower"] or 0
                res[f"block_{h}h"] &= lo >= target
            g: dict = {}
            for i, hh in zip(idx, hit):
                key = (js[i][2], int(js[i][1] // 86400))
                a = g.setdefault(key, [0, 0])
                a[0] += hh
                a[1] += 1
            res["pool_day_bootstrap"] &= (q1.cluster_lower([tuple(v) for v in g.values()], boots=400) or 0) >= target
    return res


def scenario(args):
    i, sc, windows = args
    rng = np.random.default_rng(np.random.SeedSequence(SEED, spawn_key=(i,)))
    passes = {f"block_{h}h": 0 for h in BLOCKS}
    passes["pool_day_bootstrap"] = 0
    k_ev = k_ft = n = 0
    for _ in range(windows):
        down = timeline(rng, DAYS, sc["D"], sc["u"], sc["day_model"])
        js = jobs(rng, DAYS)
        first, event = outcomes(rng, js, down, sc["eps"])
        k_ev += int(event.sum())
        k_ft += int(first.sum())
        n += len(js)
        for g, ok in gates(js, first, event).items():
            passes[g] += ok
    return {**sc, "windows": windows, "true_eventual": round(k_ev / n, 4), "true_first_try": round(k_ft / n, 4),
            "pass_rate": {g: round(v / windows, 3) for g, v in passes.items()}}


def grid() -> list[dict]:
    out = []
    for D in (120, 900, 3600, 3 * 3600, 6 * 3600, 24 * 3600):
        for u in (0.01, 0.03, 0.06, 0.10):
            for eps in (0.0, 0.03):
                out.append({"D": D, "u": u, "eps": eps, "day_model": False})
    for u in (0.03, 0.06, 0.10):                                  # Sol's model: whole UTC days up or down
        for eps in (0.0, 0.03):
            out.append({"D": 86400, "u": u, "eps": eps, "day_model": True})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=400)
    ap.add_argument("--procs", type=int, default=1)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    jobs_ = [(i, sc, a.windows) for i, sc in enumerate(grid())]
    if a.procs > 1:
        from multiprocessing import Pool
        with Pool(a.procs) as pool:
            rows = pool.map(scenario, jobs_, chunksize=1)
    else:
        rows = [scenario(j) for j in jobs_]
    for r in rows:
        m = "day" if r["day_model"] else f"D {r['D'] / 60:.0f} min"
        print(f"{m:12} u {r['u']:.2f} eps {r['eps']:.2f}: true eventual {r['true_eventual']:.3f} first {r['true_first_try']:.3f}"
              f" | " + " ".join(f"{g} {v:.2f}" for g, v in r["pass_rate"].items()), flush=True)
    if a.json:
        root = Path(__file__).resolve().parents[1]
        doc = {"what": __doc__.split("\n\n")[0], "seed": SEED, "windows_per_scenario": a.windows,
               "files_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (Path(__file__).resolve(), root / "meme_trader/sniper/q1.py")},
               "targets": q1.TARGETS, "rows": rows}
        Path(a.json).write_text(json.dumps(doc, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
