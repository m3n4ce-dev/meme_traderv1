"""Q1's frozen report (sniper/q1.py): what counts as available and as qualified, its bounds, its guards and window
extension, and its verdicts - on a synthetic quote book."""
import json
import random

import pytest

from meme_trader.sniper import q1
from meme_trader.sniper import quotes as q
from meme_trader.sniper.t9_portfolio import REGISTERED_RISK, RISK_POLICIES

OK = {"reason": "ok", "v": 2, "qualified": True, "unqualified": []}
PREFIX = {**OK, "qualified": False, "unqualified": ["undocumented_pool_bytes"]}


def test_available_and_qualified_are_kept_apart():
    c = q1.classify("ok", 1, OK)
    assert c["available"] and c["qualified"] and c["first"]
    c = q1.classify("ok", 2, PREFIX)
    assert c["available"] and not c["qualified"] and not c["first"]                  # a layout gap, not an outage
    c = q1.classify("ok", 1, {**OK, "qualified": False, "unqualified": ["no_freshness_reference"]})
    assert not c["available"] and not c["qualified"]                                  # freshness unverified
    c = q1.classify("ok", 1, {"reason": "ok", "qualified": True})                     # a record from before v2
    assert c["raw"] and not c["available"] and "before v2" in c["why"]
    c = q1.classify("skipped", 0, {"reason": "missed"})
    assert not c["available"] and c["zero_attempts"]
    assert not q1.classify("unmeasured", 1, {"reason": "late_response"})["available"]


def test_the_bounds():
    assert q1.wilson_lower(200, 200) == pytest.approx(1 / (1 + q1.Z ** 2 / 200), abs=1e-4)
    assert q1.wilson_lower(0, 0) is None
    even = [(1, 1)] * 200                                                             # 200 independent successes
    assert q1.cluster_lower(even) == 1.0
    lumpy = [(10, 10)] * 18 + [(0, 10)] * 2                                          # correlated failures: lower
    assert q1.cluster_lower(lumpy) < q1.wilson_lower(180, 200)


def book(tmp_path, jobs):
    b = q.QuoteBook(tmp_path / "quotes.db", None, lambda: 0.0)
    for i, (kind, arm, delay, hold, pool, due, state, tries, result) in enumerate(jobs):
        meta = {"follow": f"F{i}", "rule": "+40% in 5 min, volume x4", "delay": delay, "control": arm == "control",
                **({"exit": hold} if hold else {})}
        b.db.execute("INSERT INTO jobs (id, kind, pool, side, amount, due, deadline, state, tries, result, meta, "
                     "created) VALUES (?, ?, ?, 'sell', '1', ?, ?, ?, ?, ?, ?, 0)",
                     (f"J{i}", kind, pool, due, due + 900, state, tries, json.dumps(result), json.dumps(meta)))
    return tmp_path / "quotes.db"


def window_jobs(days=14, per_cell=210, fail=0.0, prefix=0.0, seed=1, start=None):
    rng = random.Random(seed)
    start = q1._day0(q1.START_DAY) if start is None else start
    out = []
    for arm in ("signal", "control"):
        for d in q1.DELAYS:
            for h in q1.HOLDS:
                for k in range(per_cell):
                    due = start + (k % days) * 86400 + 3600 + k
                    pool = f"P{k % 40}"
                    if rng.random() < fail:
                        out.append(("exit", arm, d, h, pool, due, "unmeasured", 30, {"reason": "timeout"}))
                    else:
                        out.append(("exit", arm, d, h, pool, due, "ok", 1, PREFIX if rng.random() < prefix else OK))
    return out


def test_a_complete_window_that_meets_the_guards_passes_on_both_estimands(tmp_path):
    db = book(tmp_path, window_jobs())
    r = q1.report(db, now=q1._day0(q1.START_DAY) + 15 * 86400)
    assert r["status"] == "complete" and r["guards"]["met"] and r["window"]["days"] == 14
    assert r["verdict"] == {"availability": "PASS", "execution_qualification": "PASS"}
    cell = r["cells"]["exit / signal / 5 s / hold 1 h"]
    assert cell["n"] == 210 and cell["available_eventual"]["clustered_lower"] == 1.0


def test_undocumented_pool_bytes_fail_execution_qualification_but_not_availability(tmp_path):
    db = book(tmp_path, window_jobs(prefix=0.6))
    r = q1.report(db, now=q1._day0(q1.START_DAY) + 15 * 86400)
    assert r["verdict"] == {"availability": "PASS", "execution_qualification": "FAIL (undocumented pool layout)"}
    assert r["pools_with_undocumented_bytes"] > 0


def test_provider_failures_fail_availability(tmp_path):
    db = book(tmp_path, window_jobs(fail=0.08))
    r = q1.report(db, now=q1._day0(q1.START_DAY) + 15 * 86400)
    assert r["verdict"]["availability"] == "FAIL"
    assert r["outages"]["unavailable_runs"] > 0


def test_a_short_guard_extends_the_window_a_day_at_a_time_and_pending_jobs_hold_the_verdict(tmp_path):
    start = q1._day0(q1.START_DAY)
    jobs = window_jobs(per_cell=150)                                                  # 150 per cell in 14 days
    jobs += [(j[0], j[1], j[2], j[3], j[4], start + 14 * 86400 + 3600 + i, *j[6:])
             for i, j in enumerate(window_jobs(per_cell=60, seed=2))]                 # +60 per cell on day 15
    db = book(tmp_path, jobs)
    r = q1.report(db, now=start + 16 * 86400)
    assert r["window"]["days"] == 15 and r["status"] == "complete"
    running = q1.report(db, now=start + 10 * 86400)
    assert running["status"] == "running" and running["verdict"] is None
    (tmp_path / "s").mkdir()
    short = book(tmp_path / "s", window_jobs(per_cell=50))
    r = q1.report(short, now=start + 40 * 86400)
    assert r["status"].startswith("insufficient") and r["window"]["days"] == q1.MAX_DAYS and r["verdict"] is None


def test_the_window_and_targets_are_the_frozen_ones():
    assert (q1.START_DAY, q1.DAYS, q1.MAX_DAYS) == ("2026-10-09", 14, 28)
    assert q1.TARGETS == {"eventual": 0.95, "first_try": 0.90}
    assert (q1.MIN_EXITS_PER_CELL, q1.MIN_POOLS, q1.MIN_POOL_DAYS) == (200, 30, 100)


def test_t9_e1s_risk_policy_is_the_owners_choice():
    assert REGISTERED_RISK == "net+cap" and REGISTERED_RISK in RISK_POLICIES
