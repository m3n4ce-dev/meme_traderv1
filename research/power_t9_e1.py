"""Power, null and stress simulation of the WHOLE proposed T9-E1 procedure, run through the same account model as
the paper runner (meme_trader/sniper/t9_portfolio.py).

    python research/power_t9_e1.py [--sims 150] [--null-sims 500] [--boots 400] [--hurdle 1.0] [--procs 8]
                                   [--json out.json]                                                 (needs numpy)

Version 3 (an eighth review, 2026-10-07). Version 2 bootstrapped the measured-only series while cash also lost the
written-off positions; only the signal arm ever missed an exit; missingness was drawn per episode day, not when an
exit and its retries actually happen; observed-hours invalidation wasn't simulated; and its provenance was a HEAD
string for a run on uncommitted code. Now:
- the PRIMARY statistic is the account's economic daily P&L (`Portfolio.daily`: measured exits plus impairments at
  zero recovery and fees, on the day they're recognized). The bootstrap, the hurdle H and the total all use it;
- AVAILABILITY is a calendar of provider outages (an alternating renewal process: up ~ Exp(mean up), down ~ Exp(mean
  outage), plus rare long maintenance outages), SHARED by the signal and control arms, plus each quote attempt's own
  transient failures and pools that can't be quoted at all (more often the ones collapsing: informative missingness,
  never revealed to the strategy). A quote is needed at entry (else the signal is skipped, in that arm) and at the
  exit or one of its retries (every 60 s for RETRY_S) - else the position is impaired;
- the registered INVALID rule is applied: observed hours < 80% of the window, or > 20% of attempted trades
  execution-unmeasured;
- the one-day bootstrap is the registered verdict; a moving-block (3-day) bootstrap is reported beside it;
- the scenario envelope varies winner rate (0 to replay-like), winner magnitude (full, halved), ordinary net outcome
  (pessimistic, flat after costs, mildly positive), availability (good / medium / poor) and extra cost;
- the output records the script's and the account model's sha256, the git revision with a fingerprint of any
  uncommitted changes, packages, seeds, constants, and the output's own sha256 (beside it, in <json>.sha256).
The availability profiles are DECLARED assumptions until a quote observer's reason-coded logs exist to estimate them.

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

__all__ = ["one_test", "one_test_v3", "wilson", "BANK", "MAX_OPEN", "SIZE"]      # (the account constants, for callers)

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
AVAILABILITY = {"good": (48.0, 10.0, 0.0, 0.0, 0.05, 0.01),
                "medium": (12.0, 30.0, 1 / 30, 4.0, 0.10, 0.03),
                "poor": (6.0, 60.0, 1 / 15, 8.0, 0.20, 0.08)}
ATTEMPT_S = 60                       # exit retries, from the exit time until RETRY_S after it


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
    up_h, down_min, long_per_day, long_h, _, _ = AVAILABILITY[profile]
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
    """(entry quote available, exit measured within its retries) for each signal in one arm."""
    *_, q_t, q_p = AVAILABILITY[profile]
    n = len(t_entry)
    entry_ok = _up(starts, ends, t_entry) & (rng.random(n) >= q_t)
    attempts = t_entry[:, None] + HOLD_S + ATTEMPT_S * np.arange(RETRY_S // ATTEMPT_S + 1)[None, :]
    ok = _up(starts, ends, attempts.ravel()).reshape(attempts.shape) & (rng.random(attempts.shape) >= q_t)
    dead = rng.random(n) < np.where(ret < -0.5, min(1.0, 3 * q_p), q_p)     # collapsing pools fail more often
    return entry_ok, ok.any(1) & ~dead


def _bootstrap(rng, x: np.ndarray, boots: int, block: int) -> np.ndarray:
    days = len(x)
    if block <= 1:
        return x[rng.integers(0, days, size=(boots, days))].mean(1)
    nb = -(-days // block)
    starts = rng.integers(0, days - block + 1, size=(boots, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(boots, -1)[:, :days]
    return x[idx].mean(1)


def one_test_v3(rng, days: int, win_frac: float, magnitude: float, ordinary: str, profile: str, extra: float,
                hurdle: float, boots: int) -> dict:
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
    starts, ends, observed = outages(rng, end, profile)
    t_entry = t_sig + DELAY_S
    s_entry, s_exit = _arm_quotes(rng, starts, ends, t_entry, r_sig, profile)
    c_entry, c_exit = _arm_quotes(rng, starts, ends, t_entry, r_ctl, profile)
    port, ctl = Portfolio(end), Portfolio(end)
    for i in range(n):
        if s_entry[i]:
            port.try_enter(t_sig[i], coin[i], float(r_sig[i]) if s_exit[i] else None)
        if c_entry[i]:
            ctl.try_enter(t_sig[i], coin[i], float(r_ctl[i]) if c_exit[i] else None)
    port.close()
    ctl.close()
    s_day, c_day = np.array(port.daily(days)), np.array(ctl.daily(days))
    cov = port.measured / port.attempted if port.attempted else 0.0
    total, ctl_total = float(s_day.sum()), float(c_day.sum())
    assert abs(total - (port.cash - BANK)) < 1e-6          # the primary series reconciles to cash
    out = {"total": total, "coverage": cov, "observed": observed, "control_total": ctl_total,
           "measured_only_total": float(sum(port.measured_daily(days))), "attempted": port.attempted,
           "entry_skipped": int((~s_entry).sum())}
    invalid = observed < 0.8 or cov < 0.8
    for name, block in (("verdict", 1), ("verdict_block3", 3)):
        m = _bootstrap(rng, s_day, boots, block)
        diff = _bootstrap(rng, s_day - c_day, boots, block)
        lo, hi, dlo = np.percentile(m, 5), np.percentile(m, 95), np.percentile(diff, 5)
        out[name] = ("invalid" if invalid else
                     "pass" if lo > 0 and dlo > 0 and total >= hurdle else
                     "fail" if hi <= 0 or total < ctl_total else "inconclusive")
    return out


def wilson(k: int, n: int, z: float = 1.96) -> list:
    """95% interval for a simulated proportion (Monte Carlo uncertainty of a cell)."""
    if n == 0:
        return [None, None]
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def cells(sims: int, null_sims: int) -> list[dict]:
    out = []
    for profile in AVAILABILITY:
        for extra in (0.0, 0.015):
            for days in (60, 90, 120):
                out.append(dict(scenario="null", win_frac=0.0, magnitude=1.0, ordinary="flat", profile=profile,
                                extra=extra, days=days, sims=null_sims))
    for win_frac, name in ((0.125, "eighth"), (0.25, "quarter"), (0.5, "half"), (1.0, "replay")):
        for magnitude in (1.0, 0.5):
            for ordinary in ("pessimistic", "flat", "mild"):
                for profile in AVAILABILITY:
                    for extra in (0.0, 0.015):
                        for days in (60, 90, 120):
                            out.append(dict(scenario=name, win_frac=win_frac, magnitude=magnitude, ordinary=ordinary,
                                            profile=profile, extra=extra, days=days, sims=sims))
    for ordinary in ("pessimistic", "mild"):                  # no winners at all: ordinary trades only
        for profile in AVAILABILITY:
            for days in (60, 90, 120):
                out.append(dict(scenario="no winners", win_frac=0.0, magnitude=1.0, ordinary=ordinary,
                                profile=profile, extra=0.0, days=days, sims=sims))
    return out


def run_cell(args) -> dict:
    i, cell, hurdle, boots = args
    rng = np.random.default_rng(np.random.SeedSequence(SEED, spawn_key=(i,)))
    res = [one_test_v3(rng, cell["days"], cell["win_frac"], cell["magnitude"], cell["ordinary"], cell["profile"],
                       cell["extra"], hurdle, boots) for _ in range(cell["sims"])]
    row = {**cell, "cell": i}
    n = len(res)
    for key in ("verdict", "verdict_block3"):
        v = [r[key] for r in res]
        pre = "" if key == "verdict" else "block3_"
        for k in ("pass", "fail", "inconclusive", "invalid"):
            row[pre + k] = round(v.count(k) / n, 4)
            row[pre + k + "_ci95"] = wilson(v.count(k), n)
    for k in ("total", "measured_only_total", "coverage", "observed", "control_total"):
        row["median_" + k] = round(float(np.median([r[k] for r in res])), 4)
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
            "files_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            "packages": {"numpy": md.version("numpy")}, "python": sys.version.split()[0]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=150)
    ap.add_argument("--null-sims", type=int, default=500)
    ap.add_argument("--boots", type=int, default=400)
    ap.add_argument("--hurdle", type=float, default=1.0, help="SOL the total must reach (the registration's H)")
    ap.add_argument("--procs", type=int, default=1)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    grid = cells(a.sims, a.null_sims)
    jobs = [(i, c, a.hurdle, a.boots) for i, c in enumerate(grid)]
    if a.procs > 1:
        from multiprocessing import Pool
        with Pool(a.procs) as pool:
            rows = pool.map(run_cell, jobs, chunksize=1)
    else:
        rows = [run_cell(j) for j in jobs]
    for r in rows:
        print(f"{r['scenario']:10} x{r['magnitude']:.1f} {r['ordinary']:11} {r['profile']:6} cost+{r['extra']:.3f} "
              f"{r['days']:3}d: pass {r['pass']:.3f} {r['pass_ci95']} fail {r['fail']:.2f} inconcl "
              f"{r['inconclusive']:.2f} invalid {r['invalid']:.2f} | block3 pass {r['block3_pass']:.3f} | total "
              f"{r['median_total']:+.2f} SOL, coverage {r['median_coverage']:.3f}, observed {r['median_observed']:.3f}",
              flush=True)
    if a.json:
        root = Path(__file__).resolve().parents[1]
        doc = {"version": 3, "seed": SEED, "seeding": "numpy SeedSequence(seed, spawn_key=(cell index,)) per cell",
               "sims": a.sims, "null_sims": a.null_sims, "boots": a.boots, "hurdle_sol": a.hurdle,
               **provenance([Path(__file__).resolve(), root / "meme_trader/sniper/t9_portfolio.py"]),
               "constants": {"bank": tp.BANK, "size": tp.SIZE, "max_open": tp.MAX_OPEN, "day_stop": tp.DAY_STOP,
                             "hold_s": tp.HOLD_S, "delay_s": tp.DELAY_S, "retry_s": tp.RETRY_S, "fee": FEE,
                             "attempt_s": ATTEMPT_S, "episodes_per_day": EPISODES_PER_DAY,
                             "win_episodes_per_day": WIN_EPISODES_PER_DAY, "win_net": WIN_NET, "ordinary": ORDINARY,
                             "control": CONTROL, "availability": AVAILABILITY},
               "primary_statistic": "economic daily P&L (Portfolio.daily): measured exits + impairments at zero "
                                    "recovery and fees, on the day recognized; reconciles to cash",
               "verdict_rule": "INVALID if observed hours < 80% or coverage < 80%; PASS if the day bootstrap's 5th "
                               "percentile of mean daily P&L > 0, of mean daily (signal - control) > 0, and total >= "
                               "H; FAIL if its 95th percentile <= 0 or total < control total; else INCONCLUSIVE",
               "control_policy": "an independent synthetic draw per signal (no revival), through the same provider "
                                 "outage calendar with its own transient/pool failures; real matched-pool controls "
                                 "need the runner's pre-decision data",
               "availability_note": "declared profiles, not estimates: replace them with a quote observer's "
                                    "reason-coded logs",
               "rows": rows}
        data = json.dumps(doc, indent=1)
        Path(a.json).write_text(data)
        Path(a.json + ".sha256").write_text(hashlib.sha256(data.encode()).hexdigest() + "  " + Path(a.json).name + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
