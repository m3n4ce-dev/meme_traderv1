"""Q1's frozen report (sniper/q1.py): what counts as available and as qualified, its bounds, its guards and window
extension, and its verdicts - on a synthetic quote book."""
import json
import random

import pytest

from meme_trader.sniper import q1
from meme_trader.sniper import quotes as q
from meme_trader.sniper.t9_portfolio import REGISTERED_RISK, RISK_POLICIES

OK = {"reason": "ok", "v": 3, "qualified": True, "unqualified": []}
PREFIX = {**OK, "qualified": False, "unqualified": ["undocumented_pool_bytes"]}


def test_available_and_qualified_are_kept_apart():
    c = q1.classify("ok", 1, OK)
    assert c["available"] and c["qualified"] and c["first"]
    c = q1.classify("ok", 2, PREFIX)
    assert c["available"] and not c["qualified"] and not c["first"]                  # a layout gap, not an outage
    c = q1.classify("ok", 1, {**OK, "qualified": False, "unqualified": ["no_freshness_reference"]})
    assert not c["available"] and not c["qualified"]                                  # freshness unverified
    c = q1.classify("ok", 1, {"reason": "ok", "qualified": True})                     # a record from before v2
    assert c["raw"] and not c["available"] and "before v3" in c["why"]
    c = q1.classify("ok", 1, {**OK, "v": 2})                     # v2: the 271-byte layout, whole-vault sells
    assert c["raw"] and not c["available"] and not c["qualified"]
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


def test_a_complete_all_success_window_is_described_not_certified(tmp_path):
    """Amendment 2: everything succeeded, every guard met - and still no PASS: the pool-day bootstrap reads 1.0, the
    time-block bounds say what 14 days can and can't support, each beside its assumption."""
    db = book(tmp_path, window_jobs())
    r = q1.report(db, now=q1._day0(q1.START_DAY) + 15 * 86400)
    assert r["status"] == "complete" and r["guards"]["met"] and r["window"]["days"] == 14
    assert r["verdict"] is None and r["assessment"].startswith("descriptive")
    cell = r["cells"]["exit / signal / 5 s / hold 1 h"]["available_eventual"]
    assert cell["clustered_lower"] == 1.0                                         # the diagnostic that misled
    assert cell["block_lower"]["24h"]["lower"] == q1.cp_lower(14, 14) < 0.81       # 14 clean days: ~80.7%, not 1.0


def test_undocumented_pool_bytes_count_against_qualification_but_not_availability(tmp_path):
    db = book(tmp_path, window_jobs(prefix=0.6))
    r = q1.report(db, now=q1._day0(q1.START_DAY) + 15 * 86400)
    cell = r["cells"]["exit / signal / 5 s / hold 1 h"]
    assert cell["available_eventual"]["rate"] == 1.0 and cell["qualified_eventual"]["rate"] < 0.6
    assert r["pools_with_undocumented_bytes"] > 0


def test_provider_failures_show_as_outage_episodes(tmp_path):
    db = book(tmp_path, window_jobs(fail=0.08))
    r = q1.report(db, now=q1._day0(q1.START_DAY) + 15 * 86400)
    assert r["outages"]["unavailable_runs"] > 0 and r["outages"]["episodes"] >= r["outages"]["unavailable_runs"]
    assert r["cells"]["exit / signal / 5 s / hold 1 h"]["available_eventual"]["rate"] < 1


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


# --------------------------------------------------------------------------- the scheduled clock (13th review, R13-A)
class Flaky:
    """A quoter whose first `fails` attempts on each side time out, then succeed (v3-shaped records)."""

    def __init__(self, clock, fails=1, sides=("sell",)):
        self.c, self.fails, self.sides, self.n = clock, fails, sides, {}

    def quote(self, pool, side, amount):
        self.n[side] = self.n.get(side, 0) + 1
        bad = side in self.sides and self.n[side] <= self.fails
        return {"v": 4, "reason": "timeout" if bad else "ok", "qualified": True, "unqualified": [],
                "finished_at": self.c(), "output": "1000" if side == "buy" else "300000000"}


class T:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_retries_never_move_the_scheduled_time_entry_or_exit_across_midnight_and_a_restart(tmp_path):
    midnight = q1._day0("2026-10-10")
    c = T(midnight - 3600 - 20)                                  # the entry is due 20 s before 23:00 UTC
    b = q.QuoteBook(tmp_path / "q.db", Flaky(c, fails=1, sides=("buy", "sell")), c)
    b.request("E", "entry", "P", "buy", 250_000_000, c.t, c.t + 60, {"follow": "F", "holds": {"hold 1 h": 3600}})
    b.run()                                                       # the entry times out: retried in 10 s
    assert b.db.execute("SELECT due, next_at FROM jobs WHERE id = 'E'").fetchone() == (c.t, c.t + 10)
    c.t += 10
    b.run()                                                       # in hand at 22:59:50: the exit is due 23:59:50
    exit_due = c.t + 3600
    assert b.db.execute("SELECT due FROM jobs WHERE kind = 'exit'").fetchone()[0] == exit_due
    b.db.close()
    c.t = exit_due + 1
    b = q.QuoteBook(tmp_path / "q.db", Flaky(c, fails=1, sides=("sell",)), c)      # a restart before the exit
    b.run()                                                       # times out; the retry falls after midnight
    due, nxt, tries = b.db.execute("SELECT due, next_at, tries FROM jobs WHERE kind = 'exit'").fetchone()
    assert (due, tries) == (exit_due, 1) and nxt > midnight
    b.db.close()
    c.t = nxt
    b = q.QuoteBook(tmp_path / "q.db", Flaky(c, fails=0), c)      # another restart: the retry still runs
    b.run()
    state, due, result = b.db.execute("SELECT state, due, result FROM jobs WHERE kind = 'exit'").fetchone()
    assert state == "ok" and due == exit_due and int(due // 86400) < int(midnight // 86400)   # stays on its day
    assert json.loads(result)["job"]["usable_at"] - due == pytest.approx(31, abs=0.01)


def test_a_late_response_and_a_missed_job_keep_their_scheduled_time(tmp_path):
    c = T(5000)
    b = q.QuoteBook(tmp_path / "q.db", Flaky(c, fails=0), c)
    b.request("M", "exit", "P", "sell", 1, 1000, 1900, {"follow": "F"})        # its deadline passed: missed
    b.run()
    assert b.db.execute("SELECT state, due, tries, next_at FROM jobs WHERE id = 'M'").fetchone() == \
        ("unmeasured", 1000, 0, None)


def test_old_books_are_migrated_once_and_overwritten_times_marked_unknown(tmp_path):
    import sqlite3
    p = tmp_path / "q.db"
    con = sqlite3.connect(p)                                      # a book version 1: no next_at, no due_known
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, pool TEXT NOT NULL, side TEXT NOT NULL, "
                "amount TEXT NOT NULL, due REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL, tries INTEGER "
                "NOT NULL DEFAULT 0, result TEXT, meta TEXT, created REAL NOT NULL)")
    rows = [("ok1", "ok", 1, 100.0), ("retried", "ok", 3, 160.0), ("waiting", "pending", 1, 130.0),
            ("fresh", "pending", 0, 200.0)]
    for key, state, tries, due in rows:
        con.execute("INSERT INTO jobs VALUES (?, 'exit', 'P', 'sell', '1', ?, 2000, ?, ?, '{}', '{}', 0)",
                    (key, due, state, tries))
    con.commit()
    con.close()
    b = q.QuoteBook(p, None, T(0))
    got = dict((k, (dk, nx)) for k, dk, nx in b.db.execute("SELECT id, due_known, next_at FROM jobs"))
    assert got == {"ok1": (1, None), "retried": (0, 160.0), "waiting": (0, 130.0), "fresh": (1, None)}
    assert b.db.execute("PRAGMA user_version").fetchone()[0] == q.QuoteBook.BOOK_VERSION
    b.db.close()
    b = q.QuoteBook(p, None, T(0))                                # idempotent
    assert dict((k, dk) for k, dk in b.db.execute("SELECT id, due_known FROM jobs"))["ok1"] == 1


def test_the_time_block_bound_is_exact_and_never_reads_one():
    assert q1.cp_lower(14, 14) == pytest.approx(0.05 ** (1 / 14), abs=1e-4)          # the reviewer's 80.7%
    assert q1.cp_lower(28, 28) == pytest.approx(0.05 ** (1 / 28), abs=1e-4)          # ... and 89.9%
    assert q1.cp_lower(5, 10) == pytest.approx(0.2224, abs=1e-4) and q1.cp_lower(0, 9) == 0.0
    rows = [{"due": h * 3600 + 5} for h in range(48)]
    b = q1.block_bound(rows, [True] * 48, 6)
    assert (b["blocks"], b["clean"], b["lower"]) == (8, 8, q1.cp_lower(8, 8))
    hit = [True] * 48
    hit[7] = False                                                # one failure dirties its whole 6-hour block
    assert q1.block_bound(rows, hit, 6)["clean"] == 7 and q1.block_bound(rows, hit, 1)["clean"] == 47
