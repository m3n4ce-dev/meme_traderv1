"""The paper shadow ledger (sniper/shadow_ledger.py): the authority's account journal mirrored line by line into the
ledger prototype - same-watermark cash checks, crash catch-up, refusals and divergences reported (never acted on),
the file gate, rebuild invariance, and no capability to act."""
import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from meme_trader.sniper import shadow_ledger as sl

A = "acct1"


def rows():
    """A realistic paper journal: adoption, a deposit, a buy with rent, a failed fee, a sell reclaiming the rent, the
    trade's close link (no cash), and a wallet sync."""
    cash = 3.472391524
    out = [{"ts": 1, "account": A, "kind": "adopted", "sol": 0.0, "cash_after": cash, "start_sol": 9.0}]

    def add(kind, sol, **kw):
        nonlocal cash
        cash = round(cash + sol, 9)
        out.append({"ts": len(out) + 1, "account": A, "kind": kind, "sol": round(sol, 9), "cash_after": cash, **kw})
    add("deposit", 1.0)
    add("buy", -(0.25 + 0.00203928), mint="M", rent=0.00203928, ref="SIG1")
    add("failed_fee", -0.001005, mint="M", attached="position")
    add("sell", 0.31 + 0.00203928, mint="M", rent_reclaimed=0.00203928, ref="SIG2")
    add("close", 0.0, mint="M", net_pnl=0.058)
    add("wallet_sync", -0.000001, note="fixture")
    return out


def write(p: Path, rs, partial: str = ""):
    p.write_text("".join(json.dumps(r) + "\n" for r in rs) + partial)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def shadow(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    return j, sl.ShadowLedger(j, tmp_path / "shadow.db")


def test_every_line_is_mirrored_and_cash_matches_the_authority_at_each_watermark(shadow):
    j, s = shadow
    before = sha(j)
    st = s.sync()
    assert st["errors"] == 0 and st["divergences"] == 0 and st["mirrored"] == st["lines"] == 7 and st["lag_lines"] == 0
    assert st["accounts"]["S-" + A] == sl.lamports(rows()[-1]["cash_after"]) and st["skipped_non_cash"] == 1
    bal = s.lg.balances()
    assert (f"S-{A}:rent", "SOL") not in bal                     # rent locked, then reclaimed
    s.lg.startup_check()
    assert sha(j) == before                                       # the authority is only ever read


def test_a_replay_is_a_no_op_and_a_crash_after_commit_is_caught_up_once(shadow):
    j, s = shadow
    calls = {"n": 0}

    def crash(point):
        if point == "committed":
            calls["n"] += 1
            if calls["n"] == 4:                                   # (the first commit records the source's epoch)
                raise RuntimeError("killed after the shadow committed, before anything was acknowledged")
    s._open().fault = crash
    st = s.sync()
    assert st["errors"] == 1 and st["mirrored"] == 3              # line 3 committed, then the crash
    s.lg.fault = None
    st = s.sync()
    st = s.sync()                                                 # and again: nothing new
    assert st["errors"] == 1 and st["divergences"] == 0 and st["mirrored"] == 7
    n = s.lg.db.execute("SELECT COUNT(*) FROM events WHERE effect_key LIKE 'line:%'").fetchone()[0]
    assert n == 7 and st["accounts"]["S-" + A] == sl.lamports(rows()[-1]["cash_after"])


def test_lines_written_while_the_shadow_was_off_are_caught_up_and_a_partial_line_waits(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows()[:3])
    sl.ShadowLedger(j, tmp_path / "shadow.db").sync()
    write(j, rows(), partial='{"ts": 99, "account": "acct1", "kind": "dep')   # the authority mid-write
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")                # a restart: a new instance, no memory
    st = s.sync()
    assert st["mirrored"] == 7 and st["divergences"] == 0 and st["lines"] == 7


def test_a_divergence_or_a_refusal_is_reported_and_nothing_else_happens(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    rs = rows()
    rs[1]["cash_after"] = round(rs[1]["cash_after"] + 0.5, 9)     # the authority's own number disagrees
    rs.append({"ts": 99, "account": A, "kind": "sell", "sol": 0.01, "cash_after": rs[-1]["cash_after"] + 0.01,
               "rent_reclaimed": 0.5, "ref": "SIG3"})             # reclaims rent never locked: refused (negative rent)
    write(j, rs)
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    st = s.sync()
    assert st["errors"] == 0 and st["divergences"] >= 2 and st["mirrored"] == len(rs)
    refused = [d for d in st["recent_divergences"] if "refused" in d["what"]]
    assert refused and refused[0]["line"] == len(rs) and "negative" in refused[0]["what"]


def test_a_locked_shadow_reports_an_error_and_catches_up_after(shadow, tmp_path):
    j, s = shadow
    s._open()
    s.lg.db.execute("PRAGMA busy_timeout = 1")
    other = sqlite3.connect(tmp_path / "shadow.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    st = s.sync()
    assert st["errors"] == 1 and "locked" in st["last_error"]
    other.execute("ROLLBACK")
    st = s.sync()
    assert st["mirrored"] == 7 and st["divergences"] == 0


def test_a_quarantined_shadow_file_is_reported_and_left_untouched(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    db = tmp_path / "shadow.db"
    db.write_bytes(b"not a ledger")
    st = sl.ShadowLedger(j, db).sync()
    assert st["errors"] == 1 and "quarantined" in st["last_error"] and db.read_bytes() == b"not a ledger"


def test_deleting_and_rebuilding_the_shadow_gives_the_same_books_and_never_touches_the_authority(shadow, tmp_path):
    j, s = shadow
    before = sha(j)
    s.sync()
    first = s.lg.balances()
    s.lg.db.close()
    for p in tmp_path.glob("shadow.db*"):
        p.unlink()
    again = sl.ShadowLedger(j, tmp_path / "shadow.db")
    again.sync()
    assert again.lg.balances() == first and sha(j) == before


def test_a_reset_opens_a_new_shadow_account(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    rs = rows() + [{"ts": 50, "account": "acct2", "kind": "reset", "sol": 0.0, "cash_after": 9.0, "start_sol": 9.0,
                    "previous": A},
                   {"ts": 51, "account": "acct2", "kind": "deposit", "sol": 1.0, "cash_after": 10.0}]
    write(j, rs)
    st = sl.ShadowLedger(j, tmp_path / "shadow.db").sync()
    assert st["accounts"]["S-acct2"] == 10_000_000_000 and st["divergences"] == 0
    assert st["accounts"]["S-" + A] == sl.lamports(rows()[-1]["cash_after"])


def test_passes_are_bounded_and_a_backlog_catches_up_over_several(shadow, monkeypatch):
    j, s = shadow
    monkeypatch.setattr(sl, "MAX_LINES_PER_PASS", 3)
    assert s.sync()["lag_lines"] == 4
    assert s.sync()["lag_lines"] == 1
    assert s.sync()["lag_lines"] == 0


def test_the_shadow_has_no_capability_to_act():
    """It imports nothing that trades, signs, sends or holds the bot's state: only the standard library, the config
    root, the redaction helper - and the ledger prototype, loaded from research/ by path."""
    src = Path(sl.__file__).read_text()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(("." * node.level) + (node.module or ""))
    assert mods <= {"__future__", "base64", "hashlib", "importlib.util", "json", "os", "sqlite3", "time", "pathlib",
                    "..config", "..redact"}
    assert not any(w in src for w in ("execution", "keypair", "send_transaction", "Engine(", ".sign(", "subprocess",
                                      "os.system", "os.remove", "unlink"))


def test_the_engine_runs_the_shadow_only_when_enabled_and_only_in_paper(tmp_path, monkeypatch):
    import asyncio
    import copy

    import meme_trader.journal as jm
    import meme_trader.sniper.engine as em
    from meme_trader import config
    from meme_trader.sniper.engine import Engine
    from meme_trader.sniper.execution import PaperExecutor
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)
    P = config.load(config.EXAMPLE)

    class Quiet:
        realtime, degraded = False, False

    def make(shadow):
        p = copy.deepcopy(P)
        p.sniper["ledger"] = {"shadow": shadow}
        return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), persist=True, log_to_journal=False)
    assert make(False).shadow is None                             # the default: off
    e = make(True)
    assert isinstance(e.shadow, sl.ShadowLedger) and e.shadow.journal == tmp_path / f"account-{e.mode}.jsonl"
    write(tmp_path / f"account-{e.mode}.jsonl", rows())
    asyncio.run(e._run_shadow())
    assert e.shadow.status["mirrored"] == 7 and e.shadow.status["divergences"] == 0


# ---- a fifteenth review: durable coverage, source integrity, recorded repairs, the authority cut

def unhealthy(st):
    return bool(st["errors"] or st["divergences"] or st["incomplete"] or st["lag_lines"])


def states(s):
    return [(x["n"], x["state"]) for x in s.cov.lines]


def test_holes_refusals_and_divergences_are_derived_from_storage_after_a_restart(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    rs = rows()
    rs[1]["cash_after"] = round(rs[1]["cash_after"] + 0.5, 9)              # line 2: divergent
    j.write_text(json.dumps(rs[0]) + "\n" + json.dumps(rs[1]) + "\n{not json}\n" +
                 "".join(json.dumps(r) + "\n" for r in rs[2:]))           # line 3: malformed
    first = sl.ShadowLedger(j, tmp_path / "shadow.db").sync()
    again = sl.ShadowLedger(j, tmp_path / "shadow.db").sync()              # a restart: no memory of the first
    for st in (first, again):
        assert st["unresolved_by_state"] == {"divergent": 1, "malformed": 1} and st["validated_through"] == 1
        assert st["committed"] == 8 and unhealthy(st)
    hole = [u for u in again["recent_unresolved"] if u["state"] == "malformed"][0]
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    s._open()
    payload = json.loads(s.lg.db.execute("SELECT payload FROM events WHERE effect_key = 'line:3'").fetchone()[0])
    import base64
    assert hole["line"] == 3 and base64.b64decode(payload["raw_b64"]) == b"{not json}" and payload["raw_complete"]


def test_a_recorded_repair_resolves_one_item_and_survives_a_restart(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    j.write_text(json.dumps(rows()[0]) + "\n{torn\n" + "".join(json.dumps(r) + "\n" for r in rows()[1:]))
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    assert unhealthy(s.sync())
    assert s.resolve(2, "") != ""                                           # a repair needs a note
    assert s.resolve(3, "fine") != ""                                       # and an unresolved line
    assert s.resolve(2, "a torn write from the crash at 05:10; the next row is intact") == ""
    assert s.resolve(2, "again") != ""
    st = sl.ShadowLedger(j, tmp_path / "shadow.db").sync()
    assert not unhealthy(st) and st["repaired"] == 1 and st["validated_through"] == st["committed"] == 8


def test_an_oversize_line_is_a_hole_hashed_in_chunks_with_its_head_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "MAX_LINE_BYTES", 200)
    monkeypatch.setattr(sl, "CHUNK", 16)
    monkeypatch.setattr(sl, "RAW_KEEP", 10)
    j = tmp_path / "account-paper.jsonl"
    big = b"x" * 500
    j.write_bytes(json.dumps(rows()[0]).encode() + b"\n" + big + b"\n")
    st = sl.ShadowLedger(j, tmp_path / "shadow.db").sync()
    assert st["unresolved_by_state"] == {"oversize": 1} and st["committed"] == 2 and st["lag_lines"] == 0
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    s._open()
    x = s.cov.lines[1]
    assert x["sha"] == hashlib.sha256(big).hexdigest() and x["len"] == 501


def test_the_pass_is_bounded_in_bytes_and_a_partial_line_waits_then_reports_torn(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "MAX_BYTES_PER_PASS", 150)
    monkeypatch.setattr(sl, "CHUNK", 64)
    j = tmp_path / "account-paper.jsonl"
    write(j, rows(), partial='{"ts": 99, "account": "acct1"')
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    seen = []
    for t in range(10):
        st = s.sync(now=1000.0 + t)
        seen.append(st["committed"])
    assert seen[0] < 7 and seen[-1] == 7 and st["partial_bytes"] > 0 and not st["torn_partial"]
    assert s.sync(now=1000.0 + sl.PARTIAL_STALE_S + 20)["torn_partial"]     # never completed: torn
    with j.open("a") as f:
        f.write(', "kind": "deposit", "sol": 0.0, "cash_after": %s}\n' % rows()[-1]["cash_after"])
    st = s.sync(now=2000.0)
    assert st["committed"] == 8 and not st["torn_partial"] and not unhealthy(st)


def test_truncation_is_a_persisted_fault_until_a_recorded_rebuild_which_archives_the_old_store(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    db = tmp_path / "shadow.db"
    sl.ShadowLedger(j, db).sync()
    write(j, rows()[:4])                                                    # shorter than what was consumed
    st = sl.ShadowLedger(j, db).sync()
    assert st["source"] == "fault" and "truncated" in st["faults"][0] and unhealthy(st)
    write(j, rows())                                                        # even restored, the fault stays
    s = sl.ShadowLedger(j, db)
    assert s.sync()["source"] == "fault"
    assert s.rebuild("") != ""
    assert s.rebuild("the journal was restored from the backup of 05:00") == ""
    st = s.sync()
    assert not unhealthy(st) and st["committed"] == 7 and st["faults"] == []
    kept = [p for p in tmp_path.glob("shadow.db.archived-*") if not p.name.endswith(("-wal", "-shm"))]
    assert len(kept) == 1                                                   # the old store kept, not deleted


def test_a_replaced_file_is_a_new_epoch_when_its_prefix_is_identical_and_a_fault_when_not(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows()[:4])
    db = tmp_path / "shadow.db"
    s = sl.ShadowLedger(j, db)
    s.sync()
    new = tmp_path / "new.jsonl"
    write(new, rows())
    new.replace(j)                                                          # an atomic replacement: a new inode
    st = s.sync()
    assert st["epochs"] == 2 and st["committed"] == 7 and not unhealthy(st)
    other = rows()
    other[2]["rent"] = 0.00203929                                           # one byte different, same length
    other[2]["sol"] = round(other[2]["sol"] - 0.00000001, 9)
    new.write_text("".join(json.dumps(r) + "\n" for r in other))
    new.replace(j)
    st = s.sync()
    assert st["source"] == "fault" and "replaced" in st["faults"][0]


def test_a_same_length_change_in_the_middle_is_found_by_the_scheduled_full_verification(tmp_path, monkeypatch):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    assert not unhealthy(s.sync(now=1000.0))
    raw = bytearray(j.read_bytes())
    i = raw.index(b'"deposit"')
    raw[i + 1:i + 8] = b"Deposit"                                           # line 2, in place: same length, same inode
    j.write_bytes(bytes(raw))
    assert s.sync(now=1001.0)["source"] == "ok"                             # the tail check can't see line 2...
    st = s.sync(now=1000.0 + sl.FULL_VERIFY_S + 1)                          # ...the scheduled full pass does
    assert st["source"] == "fault" and "line 2" in st["faults"][0]
    assert sl.ShadowLedger(j, tmp_path / "shadow.db").sync()["source"] == "fault"


def test_an_existing_store_is_content_checked_before_it_is_trusted(tmp_path, monkeypatch):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    db = tmp_path / "shadow.db"
    sl.ShadowLedger(j, db).sync()
    s = sl.ShadowLedger(j, db)
    ref = s.ref

    def bad(self):
        raise ref.LedgerError("refusing to run (fail closed): event 3: content hash mismatch")
    monkeypatch.setattr(ref.Ledger, "startup_check", bad)
    before = sha(db)
    st = s.sync()
    assert st["source"] == "store_invalid" and unhealthy(st) and sha(db) == before


def test_a_store_without_coverage_records_is_not_trusted(tmp_path):
    """A version-0 store (events with no source records): its highest line isn't coverage."""
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    db = tmp_path / "shadow.db"
    s = sl.ShadowLedger(j, db)
    s._open()
    s._books("S-acct1")
    s.lg.append("S-acct1", "marker", [("S-acct1:unresolved", "SOL", 0)], {"line": 1}, key="line:1")
    s.lg.db.close()
    st = sl.ShadowLedger(j, db).sync()
    assert st["source"] == "store_invalid" and unhealthy(st)


def test_the_authority_cut_compares_memory_with_the_shadow_at_that_exact_line(tmp_path):
    j = tmp_path / "account-paper.jsonl"
    write(j, rows())
    s = sl.ShadowLedger(j, tmp_path / "shadow.db")
    size = j.stat().st_size
    st = s.sync(cut={"account": A, "cash": rows()[-1]["cash_after"], "size": size, "journal_errors": 0})
    assert st["authority"]["result"] == "equal" and not unhealthy(st)
    st = s.sync(cut={"account": A, "cash": rows()[-1]["cash_after"] - 0.25, "size": size, "journal_errors": 1})
    assert st["authority"]["result"] == "divergent" and st["unresolved_by_state"] == {"authority": 1}
    st = sl.ShadowLedger(j, tmp_path / "shadow.db").sync()                 # persisted
    assert unhealthy(st) and st["unresolved_by_state"] == {"authority": 1}
    s2 = sl.ShadowLedger(j, tmp_path / "shadow.db")
    assert s2.resolve("authority:7", "the append failed on a full disk; the next row carries both") == ""
    assert not unhealthy(s2.sync())
    with j.open("a") as f:
        f.write(json.dumps({"ts": 9, "account": A, "kind": "deposit", "sol": 1.0,
                            "cash_after": round(rows()[-1]["cash_after"] + 1, 9)}) + "\n")
    st = s2.sync(cut={"account": A, "cash": rows()[-1]["cash_after"] + 1, "size": size, "journal_errors": 0})
    assert st["authority"]["line"] == 7                                     # the cut's line, not the newest


def test_incremental_and_full_rebuild_agree_on_books_and_coverage(tmp_path, monkeypatch):
    j = tmp_path / "account-paper.jsonl"
    rs = rows()
    rs[3]["cash_after"] = 0.1                                               # a divergence
    j.write_text("".join(json.dumps(r) + "\n" for r in rs[:4]) + "{x\n" + "".join(json.dumps(r) + "\n" for r in rs[4:]))
    monkeypatch.setattr(sl, "MAX_LINES_PER_PASS", 2)
    inc = sl.ShadowLedger(j, tmp_path / "inc.db")
    for _ in range(6):
        inc.sync()
    monkeypatch.setattr(sl, "MAX_LINES_PER_PASS", 5000)
    full = sl.ShadowLedger(j, tmp_path / "full.db")
    full.sync()
    assert inc.lg.balances() == full.lg.balances() and states(inc) == states(full)
    assert inc.status["unresolved_by_state"] == full.status["unresolved_by_state"] == {"divergent": 1, "malformed": 1}
