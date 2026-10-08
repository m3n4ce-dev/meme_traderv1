"""The fourteenth external review's acceptance cases, as our own tests: legacy scheduled-time provenance (a missed job
after an overwritten retry; unmigrated books read as unknown; an idempotent corrective migration), and an export
whose integrity check sees the raw tables (orphans, dropped attempts, tries against attempts, one snapshot)."""
import json
import sqlite3


from meme_trader.sniper import q1
from meme_trader.sniper import quotes as q


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Script:
    """A quoter returning the given reasons in turn (v4-shaped records)."""

    def __init__(self, clock, *reasons):
        self.c, self.reasons = clock, list(reasons)

    def quote(self, pool, side, amount):
        reason = self.reasons.pop(0) if self.reasons else "ok"
        return {"v": 4, "reason": reason, "finished_at": self.c(), "qualified": True, "unqualified": [],
                "output": "100"}


def v2_book_with_a_legacy_missed_job(path):
    """A book at version 2 (columns present, user_version 2) holding a missed job written by the OLD writer: one
    attempt whose clock has no next_attempt_at, its due overwritten by the retry, tries left at 1."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, pool TEXT NOT NULL, side TEXT NOT NULL, "
                "amount TEXT NOT NULL, due REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL, tries INTEGER NOT "
                "NULL DEFAULT 0, result TEXT, meta TEXT, created REAL NOT NULL, next_at REAL, due_known INTEGER NOT NULL "
                "DEFAULT 1)")
    con.execute("CREATE TABLE attempts (job TEXT NOT NULL, n INTEGER NOT NULL, rec TEXT NOT NULL, PRIMARY KEY (job, n))")
    con.execute("INSERT INTO jobs VALUES ('legacy', 'exit', 'P', 'sell', '1', 1030, 1900, 'unmeasured', 1, ?, '{}', 0, "
                "NULL, 1)", (json.dumps({"reason": "missed"}),))
    con.execute("INSERT INTO attempts VALUES ('legacy', 1, ?)",
                (json.dumps({"reason": "timeout", "job": {"due": 1000, "usable_at": 1000}}),))
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()


def test_the_corrective_migration_marks_a_legacy_missed_job_unknown_once(tmp_path):
    p = tmp_path / "q.db"
    v2_book_with_a_legacy_missed_job(p)
    b = q.QuoteBook(p, None, Clock(0))
    assert b.db.execute("SELECT due_known FROM jobs WHERE id = 'legacy'").fetchone()[0] == 0
    assert b.db.execute("SELECT due FROM jobs WHERE id = 'legacy'").fetchone()[0] == 1030          # never rewritten
    assert b.db.execute("PRAGMA user_version").fetchone()[0] == q.QuoteBook.BOOK_VERSION == 3
    b.db.close()
    q.QuoteBook(p, None, Clock(0)).db.close()                                                    # idempotent


def test_a_missed_job_written_by_the_current_code_keeps_its_known_time(tmp_path):
    c = Clock(1000)
    b = q.QuoteBook(tmp_path / "q.db", Script(c, "timeout"), c)
    b.request("now", "exit", "P", "sell", 1, 1000, 1100, {"follow": "F"})
    b.run()                                                           # times out: retry at 1030
    c.t = 1200
    b.run()                                                           # past the deadline: missed, tries 1
    state, due, known, tries = b.db.execute("SELECT state, due, due_known, tries FROM jobs").fetchone()
    assert (state, due, known, tries) == ("unmeasured", 1000, 1, 1)
    b.db.close()
    con = sqlite3.connect(tmp_path / "q.db")                          # replay the corrective step on it: unchanged
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    b = q.QuoteBook(tmp_path / "q.db", None, Clock(0))
    assert b.db.execute("SELECT due_known FROM jobs").fetchone()[0] == 1


def test_an_unmigrated_book_read_only_calls_every_time_unknown_and_q1_places_none(tmp_path):
    p = tmp_path / "q.db"
    v2_book_with_a_legacy_missed_job(p)
    con = sqlite3.connect(p)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    jobs, _, check = q.QuoteBook.read_only(p).export()
    assert [j["due_known"] for j in jobs] == [False] and not check["book_migrated"]
    r = q1.report(p, now=q1._day0(q1.START_DAY) + 29 * 86400)
    assert r["window"]["jobs"] == 0 and r["jobs_with_unknown_scheduled_time"] == 1


def test_the_export_check_sees_orphans_dropped_attempts_and_tries_that_dont_match(tmp_path):
    c = Clock(1000)
    b = q.QuoteBook(tmp_path / "q.db", Script(c), c)
    b.request("ok", "exit", "P", "sell", 1, 1000, 1900, {"follow": "F"})
    b.request("waiting", "exit", "P", "sell", 1, 5000, 5900, {"follow": "G"})
    b.request("gone", "exit", "P", "sell", 1, 100, 200, {"follow": "H"})
    b.run()                                                           # ok: 1 attempt; gone: missed, 0 attempts
    jobs, attempts, check = b.export()
    assert check["clean"] and check["zero_attempt_jobs_pending"] == 1 and check["zero_attempt_jobs_finished"] == 1
    b.db.execute("INSERT INTO attempts (job, n, rec) VALUES ('no-such-job', 1, '{}')")       # an orphan
    b.db.execute("INSERT INTO attempts (job, n, rec) VALUES ('ok', 3, '{}')")                # a gap: 1, 3 vs tries 1
    jobs, attempts, check = b.export()
    assert not check["clean"] and check["attempts_without_a_job"] == 1 and check["raw_attempts"] == len(attempts) == 3
    assert check["jobs_whose_attempts_dont_match_tries"] == ["ok"]
    assert [a for a in attempts if a["orphan"]][0]["job_id"] == "no-such-job"                # kept, not filtered


def test_the_package_reports_an_unclean_quote_export_as_a_finding(tmp_path):
    from meme_trader.sniper import review_export as rx
    data = tmp_path / "data"
    data.mkdir()
    b = q.QuoteBook(data / "quotes.db", None, Clock(0))
    b.db.execute("INSERT INTO attempts (job, n, rec) VALUES ('missing-job', 1, '{}')")
    b.db.close()
    ex = rx.Export.__new__(rx.Export)                                 # just the quote section, on that database
    ex.data, ex.out, ex.inputs, ex.unavailable, ex.findings, ex.counts = data, tmp_path / "out", [], [], [], {}
    ex.out.mkdir()
    ex.quotes()
    check = json.loads((ex.out / "quote_export_check.json").read_text())
    assert not check["clean"] and check["attempts_without_a_job"] == 1
    assert any("isn't clean" in f for f in ex.findings)
