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
            if calls["n"] == 3:
                raise RuntimeError("killed after the shadow committed, before anything was acknowledged")
    s._open().fault = crash
    st = s.sync()
    assert st["errors"] == 1 and st["mirrored"] == 3              # line 3 committed, then the crash
    s.lg.fault = None
    st = s.sync()
    st = s.sync()                                                 # and again: nothing new
    assert st["errors"] == 1 and st["divergences"] == 0 and st["mirrored"] == 7
    n = s.lg.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
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
    assert mods <= {"__future__", "importlib.util", "json", "sqlite3", "time", "pathlib", "..config", "..redact"}
    assert not any(w in src for w in ("execution", "keypair", "send_transaction", "Engine(", ".sign("))


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
