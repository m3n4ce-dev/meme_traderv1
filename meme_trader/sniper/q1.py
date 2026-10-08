"""Q1: the price watcher's qualification report (research/registrations/Q1-quote-qualification.md - frozen by the
owner on 2026-10-07, before its window opened; this code is part of the freeze).

    python -m meme_trader.sniper q1-report [--json data/research/q1_report.json]

Two estimands, kept apart (the freeze's one change to the eleventh review's proposal):
- AVAILABILITY - did the provider give a coherent, fresh, valid read, in hand by the job's deadline? A quote counts as
  available when it's `ok` and its only qualification gap, if any, is undocumented pool bytes. This is what T9-E1's
  A1-A5 envelope models (provider outages and failures), so it is what may replace it in power.
- EXECUTION QUALIFICATION - the same, AND the pool's whole account layout is documented (`qualified`). Its targets
  must also pass before any execution approval; a failure caused by undocumented bytes is reported as such.
A record without qualification (from before v2) is neither. Missed jobs (zero attempts) and late responses count as
failures - every job in the window is in its cell.

Cells: exits by arm x delay x hold (8 - the guards and the verdict); entries by arm x delay (4, reported apart).
Bounds: a one-sided 95% Wilson lower bound (the simple binomial), and the 5th percentile of a bootstrap over pool-days
(the clustered bound, which the verdict uses). Targets: eventual >= 95%, first try >= 90%, per exit cell.
"""
from __future__ import annotations

import calendar
import json
import math
import random
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

START_DAY = "2026-10-09"            # amended 2026-10-07 23:2x UTC, before the window opened: the fixed observer
                                    # (v3: the official 287-byte layout, spendable sell liquidity) must run all of it
DAYS, MAX_DAYS = 14, 28             # the window; extended a whole day at a time, only while a guard is short
TARGETS = {"eventual": 0.95, "first_try": 0.90}
MIN_EXITS_PER_CELL, MIN_POOLS, MIN_POOL_DAYS = 200, 30, 100
BOOT, SEED, Z = 2000, 20261008, 1.6449
BLOCKS_REPORTED = (1, 3, 6, 24)    # hours: each exit cell's block bound at every one of these is reported
BLOCK_H = 6                         # the verdict's block (amendment 2) - provisional until the calibration fixes it
HOLDS = ("hold 1 h", "hold 2 h")
DELAYS = (5, 60)


def _day0(day: str) -> float:
    return float(calendar.timegm(time.strptime(day, "%Y-%m-%d")))


REQUIRED_RECORD = 3                 # amendment 1: the fixed observer (official 287-byte layout, real-reserve sells)


def classify(state: str, tries: int, r: dict) -> dict:
    ok = state == "ok" and r.get("reason") == "ok"
    current = r.get("v", 1) >= REQUIRED_RECORD and "qualified" in r
    gaps = list(r.get("unqualified") or []) if current else ["unknown_record"]
    available = ok and current and set(gaps) <= {"undocumented_pool_bytes"}
    qualified = ok and current and r.get("qualified") is True and not gaps
    if not ok:
        why = f"{state}: {r.get('reason', '?')}"
    elif not current:
        why = f"ok, record from before v{REQUIRED_RECORD} (unknown)"
    elif qualified:
        why = "ok, qualified"
    else:
        why = "ok, unqualified: " + ",".join(gaps)
    return {"raw": ok, "available": available, "qualified": qualified, "first": tries == 1, "why": why,
            "zero_attempts": tries == 0}


def wilson_lower(k: int, n: int, z: float = Z) -> float | None:
    if n == 0:
        return None
    p = k / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(max(0.0, (c - h) / (1 + z * z / n)), 4)


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float | None:
    """The exact one-sided (1 - alpha) Clopper-Pearson lower bound for k successes in n: the p at which
    P(X >= k | n, p) = alpha. All n successes give alpha ** (1/n) - never 1.0."""
    if n == 0:
        return None
    if k == 0:
        return 0.0

    def tail(p):                                         # P(X >= k), in logs (n is a few hundred blocks at most)
        if p <= 0:
            return 0.0
        if p >= 1:
            return 1.0
        lp, lq = math.log(p), math.log1p(-p)
        return sum(math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
                   for i in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return round(lo, 4)


def block_bound(rows: list[dict], hit: list[bool], hours: float) -> dict:
    """Time blocks as the unit (amendment 2, a thirteenth review: provider outages hit every pool at once, so pools and
    pool-days aren't independent evidence). A UTC block of `hours` with at least one matured job is CLEAN when every
    job due in it succeeded; the share of clean blocks lower-bounds the block-average availability, and its exact
    Clopper-Pearson bound holds IF provider states are independent across such blocks (the stated assumption)."""
    blocks: dict = {}
    for r, h in zip(rows, hit):
        b = int(r["due"] // (hours * 3600))
        blocks[b] = blocks.get(b, True) and bool(h)
    n, k = len(blocks), sum(blocks.values())
    return {"hours": hours, "blocks": n, "clean": k, "lower": cp_lower(k, n)}


def cluster_lower(groups: list[tuple[int, int]], boots: int = BOOT, seed: int = SEED) -> float | None:
    """The 5th percentile of the success share over bootstrap resamples of whole clusters (pool-days)."""
    if not groups:
        return None
    rng = random.Random(seed)
    m = len(groups)
    shares = []
    for _ in range(boots):
        k = n = 0
        for _ in range(m):
            gk, gn = groups[rng.randrange(m)]
            k += gk
            n += gn
        shares.append(k / n if n else 0.0)
    shares.sort()
    return round(shares[int(0.05 * boots)], 4)


def _cell_stats(rows: list[dict]) -> dict:
    n = len(rows)
    out: dict = {"n": n, "zero_attempts": sum(r["c"]["zero_attempts"] for r in rows),
                 "reasons": dict(Counter(r["c"]["why"] for r in rows).most_common())}
    for est in ("raw", "available", "qualified"):
        for when in ("first_try", "eventual"):
            hit = [r["c"][est] and (r["c"]["first"] or when == "eventual") for r in rows]
            k = sum(hit)
            out[f"{est}_{when}"] = {"k": k, "rate": round(k / n, 4) if n else None, "wilson_lower": wilson_lower(k, n)}
            if est != "raw":
                g: dict = defaultdict(lambda: [0, 0])
                for r, h in zip(rows, hit):
                    g[r["cluster"]][0] += h
                    g[r["cluster"]][1] += 1
                out[f"{est}_{when}"]["clustered_lower"] = cluster_lower([tuple(v) for v in g.values()])
                out[f"{est}_{when}"]["block_lower"] = {f"{h}h": block_bound(rows, hit, h) for h in BLOCKS_REPORTED}
    return out


def _passes(cells: dict, est: str) -> bool:
    """Every exit cell's block bound (BLOCK_H hours) meets both targets (amendment 2; the pool-day bootstrap is kept
    as a diagnostic only - it reads 1.0 whenever everything succeeded, whatever the dependence)."""
    key = f"{BLOCK_H}h"
    return all((c[f"{est}_eventual"]["block_lower"][key]["lower"] or 0) >= TARGETS["eventual"] and
               (c[f"{est}_first_try"]["block_lower"][key]["lower"] or 0) >= TARGETS["first_try"] for c in cells.values())


def load(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    known = "due_known" if "due_known" in have else "1"
    rows = con.execute(f"SELECT id, kind, pool, due, deadline, state, tries, result, meta, {known} FROM jobs").fetchall()
    con.close()
    out = []
    for key, kind, pool, due, deadline, state, tries, result, meta, due_known in rows:
        m, r = json.loads(meta or "{}"), json.loads(result or "{}")
        out.append({"id": key, "kind": kind, "pool": pool, "due": due, "due_known": bool(due_known),
                    "deadline": deadline, "state": state,
                    "tries": tries, "r": r, "m": m, "arm": "control" if m.get("control") else "signal",
                    "delay": m.get("delay"), "hold": m.get("exit") if kind == "exit" else None, "rule": m.get("rule")})
    return out


def _guards(jobs: list[dict]) -> dict:
    """Matured exits per cell, and the spread of pools and pool-days (coverage guards, not independence)."""
    exits = Counter((j["arm"], j["delay"], j["hold"]) for j in jobs if j["kind"] == "exit" and j["state"] != "pending")
    pools = {j["pool"] for j in jobs}
    pool_days = {(j["pool"], int(j["due"] // 86400)) for j in jobs}
    short = [f"{a}/{d}s/{h}: {exits.get((a, d, h), 0)}" for a in ("signal", "control") for d in DELAYS
             for h in HOLDS if exits.get((a, d, h), 0) < MIN_EXITS_PER_CELL]
    return {"exit_cells_short": short, "pools": len(pools), "pool_days": len(pool_days),
            "met": not short and len(pools) >= MIN_POOLS and len(pool_days) >= MIN_POOL_DAYS}


def report(db: Path, now: float | None = None, start_day: str = START_DAY) -> dict:
    now = time.time() if now is None else now
    start = _day0(start_day)
    loaded = load(db)
    # `due` is each job's SCHEDULED time (immutable since book version 2): window, pool-day and recovery use it. A
    # job whose scheduled time was lost to an old retry (`due_known` false) can't be placed: kept out, and counted
    every = [j for j in loaded if j["due_known"]]
    unplaced = sum(1 for j in loaded if not j["due_known"] and start <= j["due"] < start + MAX_DAYS * 86400)
    days, status = DAYS, "running"
    while True:
        end = start + days * 86400
        win = [j for j in every if start <= j["due"] < end]
        complete = now >= end
        g = _guards(win)
        if g["met"] or days >= MAX_DAYS or not complete:
            break
        days += 1                                        # a guard is short at a completed window: one more day
    pending = sum(1 for j in win if j["state"] == "pending")
    for j in win:
        j["c"] = classify(j["state"], j["tries"], j["r"])
        j["cluster"] = (j["pool"], int(j["due"] // 86400))
    cells = {}
    for kind, keys in (("exit", [(a, d, h) for a in ("signal", "control") for d in DELAYS for h in HOLDS]),
                       ("entry", [(a, d, None) for a in ("signal", "control") for d in DELAYS])):
        for a, d, h in keys:
            rows = [j for j in win if j["kind"] == kind and j["arm"] == a and j["delay"] == d and j["hold"] == h]
            done = [j for j in rows if j["state"] != "pending"]          # matured jobs only; pending shown apart
            cells[f"{kind} / {a} / {d} s" + (f" / {h}" if h else "")] = {**_cell_stats(done),
                                                                          "pending": len(rows) - len(done)}
    exit_cells = {k: v for k, v in cells.items() if k.startswith("exit")}
    if complete and pending == 0:
        if not g["met"]:
            status = "insufficient: a guard is short at the longest window"
        else:
            status = "complete"
    verdict = None
    if status == "complete":
        av, qu = _passes(exit_cells, "available"), _passes(exit_cells, "qualified")
        layout = sum(1 for j in win if j["c"]["why"] == "ok, unqualified: undocumented_pool_bytes")
        verdict = {"availability": "PASS" if av else "FAIL",
                   "execution_qualification": "PASS" if qu else
                   ("FAIL (undocumented pool layout)" if av and layout else "FAIL")}
    ordered = sorted(win, key=lambda j: j["due"])          # outages: consecutive unavailable jobs, any cell
    runs, cur = [], []
    for j in ordered:
        if j["state"] == "pending":
            continue
        if j["c"]["available"]:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(j)
    if cur:
        runs.append(cur)
    longest = max(runs, key=len, default=[])
    rec = [((j["r"].get("job") or {}).get("usable_at") or 0) - j["due"] for j in win
           if j["c"]["available"] and j["tries"] > 1]
    clock = defaultdict(list)
    for j in win:
        m, job = j["m"], (j["r"].get("job") or {})
        if j["kind"] == "entry" and m.get("signal_t"):
            if m.get("signal_rx"):
                clock["chain_to_recorder_s"].append(m["signal_rx"] - m["signal_t"])
                if m.get("decided_at"):
                    clock["recorder_to_decision_s"].append(m["decided_at"] - m["signal_rx"])
            if m.get("decided_at"):
                clock["chain_to_decision_s"].append(m["decided_at"] - m["signal_t"])
        if job.get("usable_at") and j["state"] == "ok":
            clock["due_to_in_hand_s"].append(job["usable_at"] - j["due"])
        if j["state"] == "ok" and j["r"].get("context_slot") and j["r"].get("ref_slot"):
            clock["slots_behind_feed"].append(j["r"]["ref_slot"] - j["r"]["context_slot"])
        if j["state"] == "ok" and j["r"].get("response_s") is not None:
            clock["response_s"].append(j["r"]["response_s"])

    def q(xs):
        xs = sorted(xs)
        return {"n": len(xs), "median": round(statistics.median(xs), 3), "p90": round(xs[int(0.9 * (len(xs) - 1))], 3),
                "max": round(xs[-1], 3)} if xs else {"n": 0}
    by_rule = {}
    for rule in sorted({j["rule"] for j in win if j["rule"]}):
        rs = [j for j in win if j["rule"] == rule and j["state"] != "pending"]
        by_rule[rule] = {"n": len(rs), "available": sum(j["c"]["available"] for j in rs),
                         "qualified": sum(j["c"]["qualified"] for j in rs)}
    tails = {j["pool"] for j in win if j["c"]["why"] == "ok, unqualified: undocumented_pool_bytes"}
    return {"window": {"start_utc": start_day, "days": days, "end_ts": end, "complete": complete,
                       "pending_jobs": pending, "jobs": len(win)},
            "status": status, "verdict": verdict, "guards": g, "targets": TARGETS, "cells": cells,
            "jobs_with_unknown_scheduled_time": unplaced,
            "outages": {"unavailable_runs": len(runs), "longest_run_jobs": len(longest),
                        "longest_run_minutes": round((longest[-1]["due"] - longest[0]["due"]) / 60, 1) if longest else 0},
            "recovery_s": q(rec), "clock": {k: q(v) for k, v in clock.items()}, "by_rule": by_rule,
            "pools_with_undocumented_bytes": len(tails),
            "note": "engineering qualification only: not a landing-rate, failed-fee or edge estimate"}
