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

Cells: exits by arm x delay x hold (8, the guards); entries by arm x delay (4, reported apart).

Amendment 2 (a thirteenth review, before the window opened): NO FORMAL PASS/FAIL. Provider outages hit every pool at
once, so pools and pool-days aren't independent evidence, and the pool-day bootstrap reads 1.0 whenever everything
succeeded. research/q1_calibration.py ran whole windows under common outage processes through this code: no bound in
the family both kept false passes <= 5% under outages of an hour or more and had power in 14-28 days (a whole-day
shock can't be certified at 95% in under ~2 months). So the report is DESCRIPTIVE: per cell, the estimates (simple
Wilson bound beside them), the time-block bounds (exact one-sided Clopper-Pearson on clean UTC blocks of 1/3/6/24 h,
each valid only if provider states are independent across blocks of that length), the pool-day bootstrap (a
diagnostic only), and the outage episodes seen - the measured outage process T9-E1's power may add as a profile.
Targets kept for reference: eventual >= 95%, first try >= 90%.

Amendment 4 (a fifteenth review, before the window opened; amendment 2 confirmed by the owner): the attempt-level
observation product is three-state (up / down / unknown, from each attempt's own read-stage evidence), covers all
elapsed time (edges and empty windows unobserved; clipped to now), and reports DETECTED failure runs that never
cross an unsampled gap or a host change. The outage-profile recipe is replaced by a nonparametric stress profile at
frozen horizons (the registration), labelled stress - not confidence - until the whole pipeline is calibrated.
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


EPISODE_GAP_S = 900                # unavailable jobs this close (no available job between) are one failure span
# The attempt-level observation product (a fourteenth review: final job outcomes aren't a provider up/down trace;
# a fifteenth: an attempt is evidence about the read path only if it READ). Each attempt is classified from its own
# read-stage evidence (quote record v5 `reads`), three ways:
#   up      - a network read responded (an answer, even a refusal about one pool, is the provider responding);
#   down    - a read was attempted and failed: timeout, HTTP or RPC error, a malformed answer;
#   unknown - no read: a cached or local refusal, a local error, or a legacy record whose evidence can't place it.
# Unknown attempts observe nothing: they are neither up nor down, and they don't cover time. Responses outside the
# freshness or latency limits (stale_state, slow_response) are UP for the transport and DEGRADED for the read path -
# a stale answer can be the provider's lag or the reference feed's, not proof of a provider outage.
READ_FAILED = {"timeout", "rpc_error"}
DEGRADED = {"slow_response", "stale_state"}
TRANSPORT = READ_FAILED | DEGRADED   # the read-path product's failures (legacy name: a fourteenth review's set)
NO_OBSERVATION_S = 600              # a gap longer than this with no observing read is UNOBSERVED - never up, never down
MAX_INTERVALS = 500                 # unobserved intervals listed (all are counted)


def observation(r: dict) -> tuple[str, str]:
    """(up | down | unknown, its evidence) for one attempt record."""
    reads = r.get("reads")
    if isinstance(reads, list):                          # v5+: explicit read-stage evidence
        if not reads:
            return "unknown", "no read (a cached or local refusal, or a local error)"
        last = reads[-1]
        return ("up" if last.get("outcome") == "responded" else "down"), f"{last.get('stage')} read {last.get('outcome')}"
    if r.get("responded_at") is not None:                # legacy: the main read responded
        return "up", "legacy: the accounts read responded"
    if r.get("reason") in READ_FAILED:                   # legacy: inferred (an old rpc_error may have been local)
        return "down", "legacy: inferred from the reason"
    return "unknown", "legacy: no read-stage evidence"


def attempt_trace(db: Path) -> list[dict]:
    """Every attempt as an observation: when it was requested and finished, its reason, what it observed (up / down /
    unknown, with the evidence), whether a response was degraded, its job, host and code revision. By request time."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT job, n, rec FROM attempts").fetchall()
    con.close()
    out = []
    for job, n, rec in rows:
        r = json.loads(rec)
        t = r.get("requested_at") or (r.get("job") or {}).get("started_at")
        if t is None:
            continue
        o, why = observation(r)
        out.append({"job": job, "n": n, "t": float(t), "done": r.get("finished_at"), "reason": r.get("reason"),
                    "observation": o, "evidence": why, "down": o == "down",
                    "degraded": o == "up" and r.get("reason") in DEGRADED, "host": r.get("host", ""),
                    "code": r.get("code", "")})
    return sorted(out, key=lambda a: a["t"])


def _obs(a: dict) -> str:
    return a.get("observation") or ("down" if a.get("down") else "up")


def _runs(seen: list[dict], start: float, clip: float, end: float, failed) -> list[dict]:
    """Detected failure runs among observing attempts. A run is consecutive failed observations within one SEGMENT -
    observations no more than NO_OBSERVATION_S apart, on one host - so an unsampled gap or a host change always ends
    a run: it can't be counted as continuous downtime. Each run's edges say what bounds it: an up observation, or
    (censored) the window's start, the clip (window end, or `now` while running), an unobserved gap, a host change."""
    runs, cur, seg_start = [], None, 0
    for i, a in enumerate(seen):
        prev = seen[i - 1] if i else None
        boundary = prev is None or a["t"] - prev["t"] > NO_OBSERVATION_S or a.get("host") != prev.get("host")
        if boundary:
            if cur is not None:
                cur["right"] = "gap" if prev and a["t"] - prev["t"] > NO_OBSERVATION_S else "host_change"
                runs.append(cur)
                cur = None
            seg_start = i
        if failed(a):
            if cur is None:
                left = ("up", seen[i - 1]["t"]) if i > seg_start else \
                    (("window_start" if a["t"] - start <= NO_OBSERVATION_S else "gap") if prev is None else
                     ("gap" if a["t"] - prev["t"] > NO_OBSERVATION_S else "host_change"))
                cur = {"fails": [], "left": left, "i0": i}
            cur["fails"].append(a["t"])
            cur["i1"] = i
        elif cur is not None:
            cur["right"] = ("up", a["t"])
            runs.append(cur)
            cur = None
    if cur is not None:
        cur["right"] = ("clip" if clip < end else "window_end") if clip - seen[-1]["t"] <= NO_OBSERVATION_S else "gap"
        runs.append(cur)
    out = []
    for k, r in enumerate(runs):
        f = r["fails"]
        lu = r["left"][1] if isinstance(r["left"], tuple) else None
        ru = r["right"][1] if isinstance(r["right"], tuple) else None
        # the nearest UP observation on each side, beyond any gap or host change: the episode containing these
        # failures (if they are one) lies between them - a bound that includes unobserved time when an edge is a gap
        before = lu if lu is not None else next((x["t"] for x in reversed(seen[:r["i0"]]) if not failed(x)), None)
        after = ru if ru is not None else next((x["t"] for x in seen[r["i1"] + 1:] if not failed(x)), None)
        out.append({"first_fail": f[0], "last_fail": f[-1], "failures": len(f),
                    "min_s": round(f[-1] - f[0], 1),
                    "max_s": round(ru - lu, 1) if lu is not None and ru is not None else None,
                    "max_s_including_unobserved": round(after - before, 1) if before is not None and after is not None
                    else None,
                    "left": r["left"][0] if isinstance(r["left"], tuple) else r["left"],
                    "right": r["right"][0] if isinstance(r["right"], tuple) else r["right"],
                    "left_censored": lu is None, "right_censored": ru is None,
                    "may_join_previous": bool(k and out[-1]["right"] in ("gap", "host_change") and
                                              r["left"] in ("gap", "host_change") and
                                              not any(not failed(x) for x in seen[runs[k - 1]["i1"] + 1:r["i0"]]))})
    return out


def transport_brackets(trace: list[dict], start: float, end: float, now: float | None = None,
                       liveness: list | None = None) -> dict:
    """The observation product over [start, min(now, end)) - never past `now`: future scheduled time isn't unobserved
    downtime. Exposure is all of that elapsed time; it's OBSERVED only between observing reads at most
    NO_OBSERVATION_S apart, so the window's leading and trailing edges, and an empty window, are unobserved too.
    Runs are DETECTED failure runs (see _runs): `min_s` (first to last failure) is a duration only under the declared
    one-episode assumption for failures within one segment, and `max_s` is bounded only by up observations. Two
    products: the TRANSPORT (down = a failed read) and the READ PATH (down or degraded). `liveness`: the process's
    heartbeat intervals, which split unobserved time into the process up with no reads, and the process down/unknown."""
    clip = min(end, now) if now is not None else end
    obs = [a for a in trace if start <= a["t"] < clip]
    seen = [a for a in obs if _obs(a) != "unknown"]
    gaps, t = [], start
    for a in seen:
        if a["t"] - t > NO_OBSERVATION_S:
            gaps.append((t, a["t"]))
        t = a["t"]
    if clip - t > NO_OBSERVATION_S:
        gaps.append((t, clip))
    if not seen and clip > start:
        gaps = [(start, clip)]
    unobs = sum(b - a for a, b in gaps)
    exposure = max(0.0, clip - start)
    transport = _runs(seen, start, clip, end, lambda a: _obs(a) == "down") if seen else []
    read_path = _runs(seen, start, clip, end, lambda a: _obs(a) == "down" or a.get("degraded")) if seen else []
    hosts = Counter(a.get("host", "") for a in seen)
    by_host = {h: {"observations": n, "down": sum(1 for a in seen if a.get("host", "") == h and _obs(a) == "down")}
               for h, n in hosts.items()}
    out = {"what": "DETECTED failure runs from observing reads (record v5 read-stage evidence; legacy records are "
                   "inferred and labelled). Not a physical outage trace: episodes between reads are missed, one run "
                   "can hide several episodes, min_s holds only under the one-episode assumption within a segment",
           "clipped_to": clip, "running": clip < end, "exposure_hours": round(exposure / 3600, 2),
           "attempts": len(obs), "observing_attempts": len(seen),
           "observations": dict(Counter(_obs(a) for a in obs)),
           "evidence": dict(Counter(a.get("evidence", "") for a in obs if a.get("evidence")).most_common()),
           "transport_failures": sum(1 for a in seen if _obs(a) == "down"),
           "degraded_responses": sum(1 for a in seen if a.get("degraded")),
           "reasons": dict(Counter(a["reason"] for a in obs).most_common()),
           "runs": len(transport), "brackets": transport,
           "read_path": {"runs": len(read_path), "brackets": read_path},
           "observed_hours": round((exposure - unobs) / 3600, 2), "unobserved_hours": round(unobs / 3600, 2),
           "unobserved_gaps": len(gaps), "unobserved_intervals": [[round(a, 3), round(b, 3)] for a, b in gaps[:MAX_INTERVALS]],
           "hosts": dict(hosts), "by_host": by_host,
           "host_changes": sum(1 for x, y in zip(seen, seen[1:]) if x.get("host") != y.get("host")),
           "code_revisions": dict(Counter(a.get("code", "") for a in seen))}
    if liveness is not None:
        up_s = 0.0
        for a, b in gaps:
            for s0, s1 in liveness:
                up_s += max(0.0, min(b, s1) - max(a, s0))
        out["unobserved_split"] = {"process_up_no_reads_hours": round(up_s / 3600, 2),
                                   "process_down_or_unknown_hours": round((unobs - up_s) / 3600, 2),
                                   "heartbeat_since": liveness[0][0] if liveness else None}
    return out


def _split(runs: list[list[dict]]) -> list[list[dict]]:
    """Runs of consecutive unavailable jobs, split where two of them are more than EPISODE_GAP_S apart."""
    out = []
    for r in runs:
        cur = [r[0]]
        for j in r[1:]:
            if j["due"] - cur[-1]["due"] > EPISODE_GAP_S:
                out.append(cur)
                cur = []
            cur.append(j)
        out.append(cur)
    return out


def _liveness(db: Path) -> list:
    """The price watcher's heartbeat intervals (quotes.QuoteBook), or [] for a book from before it."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in con.execute("SELECT start, last FROM liveness ORDER BY start")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def load(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    from .quotes import QuoteBook
    current = con.execute("PRAGMA user_version").fetchone()[0] >= QuoteBook.BOOK_VERSION
    # an unmigrated book's scheduled times are UNKNOWN (no provenance is not proof), never assumed known (14th review)
    known = "due_known" if ("due_known" in have and current) else "0"
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
    unplaced = sum(1 for j in loaded if not j["due_known"])   # (all of them: an unknown time places no job in a window)
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
    if complete and pending == 0:
        if not g["met"]:
            status = "insufficient: a guard is short at the longest window"
        else:
            status = "complete"
    # Amendment 2: no formal PASS/FAIL. The calibration (research/q1_calibration.py) showed no bound in this family
    # both controls false passes under common outages of an hour or more and has power in 14-28 days; a whole-day
    # common shock can't be certified at 95% in under ~2 months. The report is DESCRIPTIVE: estimates, the outage
    # episodes seen, and every block bound beside the independence assumption it needs.
    verdict = None
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
    # episodes on the provider's clock: unavailable jobs (any cell) less than EPISODE_GAP_S apart with no available
    # job between them - the measured outage process that may become an availability profile in T9-E1's power
    episodes = [{"start": r[0]["due"], "end": r[-1]["due"], "jobs": len(r),
                 "minutes": round((r[-1]["due"] - r[0]["due"]) / 60, 1)} for r in _split(runs)]
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
            "failure_spans": {
                "what": "EVENTUAL quote-failure spans: jobs whose final outcome was unavailable, merged when under "
                        f"{EPISODE_GAP_S} s apart, timed by their scheduled due - sampled where jobs happened to be, so "
                        "first/last failed dues are detection points, not onset/recovery, a one-job span's 0 minutes "
                        "isn't a 0-minute outage, and recovered interruptions don't appear. Not a provider outage trace: "
                        "see transport_observations",
                "unavailable_runs": len(runs), "longest_run_jobs": len(longest),
                "longest_run_minutes": round((longest[-1]["due"] - longest[0]["due"]) / 60, 1) if longest else 0,
                "spans": len(episodes), "spans_per_day": round(len(episodes) / max(days, 1), 3),
                "span_minutes": sorted(e["minutes"] for e in episodes), "span_list": episodes},
            "outages": {"episodes": len(episodes), "renamed": "failure_spans (not provider outages)"},
            "transport_observations": transport_brackets(attempt_trace(db), start, end, now=now,
                                                         liveness=_liveness(db)),
            "assessment": "descriptive (amendment 2): no formal verdict - a block bound holds only if provider states "
                          "are independent across blocks of its length; see block_lower per cell and "
                          "data/research/q1_calibration.json",
            "recovery_s": q(rec), "clock": {k: q(v) for k, v in clock.items()}, "by_rule": by_rule,
            "pools_with_undocumented_bytes": len(tails),
            "note": "engineering qualification only: not a landing-rate, failed-fee or edge estimate"}
