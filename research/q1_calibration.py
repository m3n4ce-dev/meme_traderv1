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

Version 2 (a fourteenth review): the timeline starts in its STATIONARY state (down with probability u, each period's
remainder exponential) - version 1 always started up, a chosen stress initialization - plus explicitly initially-down
cases; a zero-outage control; 28-day windows beside 14-day ones; the as-run revision and environment recorded.
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


def timeline(rng, days: int, D: float, u: float, day_model: bool, start: str = "stationary"):
    """Down intervals [(start, end)] in seconds over the window (+ a day's margin for retries). `start`: "stationary"
    (down at t=0 with probability u; exponential periods are memoryless, so the remainder is a full exponential) or
    "down" (the window opens inside an outage)."""
    end = (days + 1) * 86400
    if u <= 0:
        return np.empty((0, 2))
    if day_model:
        down = rng.random(days + 1) < u
        if start == "down":
            down[0] = True
        return np.array([(d * 86400, (d + 1) * 86400) for d in range(days + 1) if down[d]]).reshape(-1, 2)
    up_mean = D * (1 - u) / u
    t, out = 0.0, []
    if start == "down" or rng.random() < u:                       # opens inside an outage
        d = rng.exponential(D)
        out.append((0.0, d))
        t = d
    t += rng.exponential(up_mean)
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
    rng = np.random.default_rng(np.random.SeedSequence(SEED + 2, spawn_key=(i,)))
    days = sc["days"]
    passes = {f"block_{h}h": 0 for h in BLOCKS}
    passes["pool_day_bootstrap"] = 0
    k_ev = k_ft = n = 0
    for _ in range(windows):
        down = timeline(rng, days, sc["D"], sc["u"], sc["day_model"], sc["start"])
        js = jobs(rng, days)
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
    for days in (14, 28):
        for eps in (0.0, 0.03):                                   # the control: no outages at all
            out.append({"days": days, "D": 0, "u": 0.0, "eps": eps, "day_model": False, "start": "stationary"})
        durations = (120, 900, 3600, 3 * 3600, 6 * 3600, 24 * 3600) if days == 14 else (3600, 6 * 3600, 24 * 3600)
        for D in durations:
            for u in (0.01, 0.03, 0.06, 0.10):
                for eps in (0.0, 0.03):
                    out.append({"days": days, "D": D, "u": u, "eps": eps, "day_model": False, "start": "stationary"})
        for u in (0.03, 0.06, 0.10):                              # the reviewer's model: whole UTC days up or down
            for eps in (0.0, 0.03):
                out.append({"days": days, "D": 86400, "u": u, "eps": eps, "day_model": True, "start": "stationary"})
        for D in (6 * 3600, 24 * 3600):                           # windows that open inside an outage
            out.append({"days": days, "D": D, "u": 0.06, "eps": 0.0, "day_model": False, "start": "down"})
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
        print(f"{r['days']}d {r['start'][:4]} {m:12} u {r['u']:.2f} eps {r['eps']:.2f}: true eventual {r['true_eventual']:.3f} first {r['true_first_try']:.3f}"
              f" | " + " ".join(f"{g} {v:.2f}" for g, v in r["pass_rate"].items()), flush=True)
    if a.json:
        root = Path(__file__).resolve().parents[1]
        import importlib.metadata as md
        import platform
        import subprocess

        def git(*args):
            try:
                return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, cwd=root).stdout
            except (OSError, subprocess.SubprocessError):
                return ""
        diff = git("diff", "HEAD") + git("status", "--porcelain")
        doc = {"what": __doc__.split("\n\n")[0], "version": 2, "seed": SEED + 2, "windows_per_scenario": a.windows,
               "as_run": {"code_revision": git("rev-parse", "HEAD").strip(),
                          "uncommitted_changes_sha256": hashlib.sha256(diff.encode()).hexdigest() if diff.strip() else "",
                          "python": sys.version.split()[0], "numpy": md.version("numpy"),
                          "platform": platform.platform()},
               "files_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (Path(__file__).resolve(), root / "meme_trader/sniper/q1.py")},
               "targets": q1.TARGETS, "rows": rows}
        Path(a.json).write_text(json.dumps(doc, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
