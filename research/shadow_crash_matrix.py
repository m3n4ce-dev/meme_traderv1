"""The paper shadow's crash matrix at the REAL adapter boundary (a fifteenth review: "exceptions injected into a
reference ledger alone are insufficient").

Each case runs the real paper Engine in a child process, in its own temporary data directory: restore, a scripted run
of cash events through the engine's own `_cash` -> account-journal append -> `save_state` path (a deposit, a buy with
rent, a failed fee, a sell reclaiming the rent), the shadow syncing with the engine's authority cut - and one fault
injected at a boundary:
  - SIGKILL after the memory change, before the journal append; in the middle of the append (a torn line); after the
    append, before the state save; in the middle of the state save (temporary file written, never renamed); in the
    middle of the shadow's own commit;
  - a full disk: an append that fails and recovers, one that fails and is followed by a kill, and the kernel's real
    file-size limit (RLIMIT_FSIZE, EFBIG) on every write - the journal, the state and the shadow's store;
  - the source itself: atomically replaced by an identical copy, edited in the middle at the same length, deleted;
  - concurrency: a writer appending rows in two halves while another process syncs the shadow.
Then a second child RESTARTS - a new engine restores its state (and records a restore gap if the state and the
journal disagree), the shadow syncs with a fresh cut - and reports the restored authority's cash, the journal's last
row, the shadow's books, source epochs and unresolved coverage. Paper only; no network; nothing outside the case's
directory is touched.

    python research/shadow_crash_matrix.py [--out data/research/shadow_crash_matrix.json] [--case NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STEPS = [("deposit", 1.0, {}),
         ("buy", -(0.25 + 0.00203928), {"mint": "M1", "rent": 0.00203928, "ref": "SIG1"}),
         ("failed_fee", -0.001005, {"mint": "M1", "attached": "position"}),
         ("sell", 0.31 + 0.00203928, {"mint": "M1", "rent_reclaimed": 0.00203928, "ref": "SIG2"}),
         ("deposit", 0.5, {})]
FAULT_AT = 2                                             # the fault hits the third event (the failed fee) by default


def _engine(data: Path):
    """A real paper Engine whose every file is under `data` (its own directory per case)."""
    import meme_trader.journal as jm
    import meme_trader.sniper.engine as em
    from meme_trader import config
    from meme_trader.sniper.execution import PaperExecutor
    em.DATA = jm.DATA = data
    P = config.load(config.EXAMPLE)
    P.sniper["ledger"] = {"shadow": True}

    class Quiet:
        realtime, degraded = False, False

        def now(self):
            return time.time()

    e = em.Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    e.say = lambda level, text, *a, **k: e.stats.update({f"said_{level}": e.stats[f"said_{level}"] + 1})

    async def nothing():
        return None
    e.reconcile = nothing
    return e


def _kill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def child_run(data: Path, case: str) -> None:
    """Phase 1: restore, the scripted events with the case's fault, the shadow syncing as the engine does."""
    import asyncio
    import errno
    import resource

    e = _engine(data)
    asyncio.run(e.restore_state())
    e.save_state()
    journal = data / f"account-{e.mode}.jsonl"
    orig_flush, orig_save = e._flush_account_journal, e.save_state
    state = {"i": -1}

    def at_fault():
        return state["i"] == FAULT_AT

    if case == "kill_before_append":
        def flush():
            if at_fault():
                _kill()
            orig_flush()
        e._flush_account_journal = flush
    elif case == "kill_mid_append":
        def flush():
            if at_fault():
                data_ = e._journal_pending[0]
                with journal.open("ab") as f:
                    f.write(data_[:len(data_) // 2])
                _kill()
            orig_flush()
        e._flush_account_journal = flush
    elif case == "kill_after_append_before_save":
        def save():
            if at_fault():
                _kill()
            orig_save()
        e.save_state = save
    elif case == "kill_mid_state_save":
        real_replace = Path.replace

        def replace(self, target):
            if at_fault() and Path(target) == e.state_path:
                _kill()
            return real_replace(self, target)
        Path.replace = replace
    elif case in ("disk_full_append_recovers", "disk_full_append_then_kill"):
        real_open = Path.open

        def open_(self, mode="r", *a, **k):
            if at_fault() and self == journal and "a" in mode:
                raise OSError(errno.ENOSPC, "No space left on device (injected)")
            return real_open(self, mode, *a, **k)
        Path.open = open_
        if case == "disk_full_append_then_kill":
            def save():
                orig_save()
                if at_fault():
                    _kill()
            e.save_state = save
    elif case == "disk_full_real_limit":
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)    # the kernel's limit then fails the write with EFBIG

    for i, (kind, sol, kw) in enumerate(STEPS):
        state["i"] = i
        if case == "disk_full_real_limit" and i == FAULT_AT:
            sizes = [p.stat().st_size for p in data.iterdir() if p.is_file()]
            lim = min(sizes) if sizes else 1
            resource.setrlimit(resource.RLIMIT_FSIZE, (lim, resource.RLIM_INFINITY))
        if kind == "deposit":
            e.deposit_paper(sol)
        else:
            e._cash(sol, kind, **kw)
        try:
            e.save_state()
        except OSError:
            e.stats["state_save_errors"] += 1           # (the engine's own callers see this as an error)
        if case == "kill_mid_shadow_commit" and i == FAULT_AT:
            e.shadow._open().fault = lambda point: _kill() if point == "postings" else None
        e.shadow.sync(cut=e._shadow_cut())
        if case == "disk_full_real_limit" and i == FAULT_AT:
            _kill()                                      # the process dies with the disk still full
    print(json.dumps({"finished": True, "stats": dict(e.stats)}))


def child_restart(data: Path) -> None:
    """Phase 2: a new engine restores; the shadow syncs with a fresh cut; everything is reported."""
    import asyncio

    e = _engine(data)
    asyncio.run(e.restore_state())
    st = e.shadow.sync(cut=e._shadow_cut())
    journal = data / f"account-{e.mode}.jsonl"
    rows, bad = [], 0
    if journal.exists():
        for raw in journal.read_bytes().split(b"\n")[:-1]:
            try:
                rows.append(json.loads(raw))
            except ValueError:
                bad += 1
    mine = [r for r in rows if r.get("account") == e.book.account_id]
    print(json.dumps({
        "authority_cash": round(e.book.sol, 9),
        "journal": {"exists": journal.exists(), "rows": len(rows), "unreadable_lines": bad,
                    "last_cash_after": mine[-1]["cash_after"] if mine else None,
                    "kinds": [r.get("kind") for r in rows]},
        "shadow": {k: st.get(k) for k in ("source", "committed", "validated_through", "unresolved",
                                          "unresolved_by_state", "faults", "epochs", "lag_lines", "incomplete",
                                          "healthy", "errors", "last_error", "authority", "accounts")},
        "stats": {k: v for k, v in e.stats.items() if "journal" in k or "restore" in k or "said_error" in k}},
        default=str))


def _py(args: list[str], env: dict | None = None, timeout: float = 120) -> tuple[int, dict | None, str]:
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=timeout, env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})})
    out = None
    for line in reversed(p.stdout.splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    return p.returncode, out, p.stderr[-2000:]


def _source_case(data: Path, case: str) -> None:
    """Between the run and the restart, the source itself changes (in the parent: the bot is down)."""
    j = data / "account-paper.jsonl"
    if case == "source_replaced_identical":
        tmp = data / "copy.jsonl"
        tmp.write_bytes(j.read_bytes())
        tmp.replace(j)                                   # a new inode, the same bytes
    elif case == "source_edited_same_length":
        raw = bytearray(j.read_bytes())
        i = raw.index(b'"failed_fee"')
        raw[i + 1] = ord("F")                            # in place, one byte, same length
        j.write_bytes(bytes(raw))
    elif case == "source_deleted":
        j.unlink()


def _concurrent(data: Path) -> dict:
    """A writer appends rows in two halves (a pause between) while another process syncs the shadow throughout."""
    code = ("import json,sys,time;from pathlib import Path;from meme_trader.sniper import shadow_ledger as sl;"
            "d=Path(sys.argv[1]);s=sl.ShadowLedger(d/'account-paper.jsonl',d/'shadow.db');seen=0;"
            "end=time.time()+float(sys.argv[2])\n"
            "while time.time()<end:\n st=s.sync();seen+=bool(st['partial_bytes']);time.sleep(0.002)\n"
            "st=s.sync();print(json.dumps({'passes_seeing_a_partial_line':seen,'committed':st['committed'],"
            "'unresolved':st['unresolved'],'healthy':st['healthy'],'cash':st['accounts']}))")
    j = data / "account-paper.jsonl"
    cash = 10.0
    rows = [{"ts": 0, "account": "C", "kind": "adopted", "sol": 0.0, "cash_after": cash}]
    reader = subprocess.Popen([sys.executable, "-c", code, str(data), "4"], cwd=str(ROOT), text=True,
                              stdout=subprocess.PIPE, env={**os.environ, "PYTHONPATH": str(ROOT)})
    with j.open("ab", buffering=0) as f:
        f.write((json.dumps(rows[0]) + "\n").encode())
        for i in range(60):
            cash = round(cash + 0.01, 9)
            b = (json.dumps({"ts": i + 1, "account": "C", "kind": "deposit", "sol": 0.01, "cash_after": cash})
                 + "\n").encode()
            f.write(b[:len(b) // 2])
            time.sleep(0.02)
            f.write(b[len(b) // 2:])
    out, _ = reader.communicate(timeout=60)
    r = json.loads(out.strip().splitlines()[-1])
    r["rows_written"], r["expected_cash"] = 61, int(round(cash * 1e9))
    return r


CASES = ("control", "kill_before_append", "kill_mid_append", "kill_after_append_before_save", "kill_mid_state_save",
         "kill_mid_shadow_commit", "disk_full_append_recovers", "disk_full_append_then_kill", "disk_full_real_limit",
         "source_replaced_identical", "source_edited_same_length", "source_deleted", "concurrent_partial_writes")


def run_case(case: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"crash-{case}-") as d:
        data = Path(d)
        if case == "concurrent_partial_writes":
            return {"case": case, **_concurrent(data)}
        code, run, err = _py(["--child", "run", d, "control" if case.startswith("source_") else case])
        killed = code == -signal.SIGKILL
        if case.startswith("source_"):
            _source_case(data, case)
        code2, after, err2 = _py(["--child", "restart", d])
        return {"case": case, "phase1": "killed" if killed else ("finished" if code == 0 else f"exit {code}"),
                "phase1_stderr": "" if killed or code == 0 else err, "restart_exit": code2,
                "restart_stderr": err2 if code2 else "", **(after or {})}


# What each case must show after the restart: whether the restored authority, its journal and the shadow agree,
# whether the shadow must be healthy, and - when it mustn't - the recorded cause it must carry.
EXPECT = {
    "control": (True, True, None),
    "kill_before_append": (True, True, None),            # the event is lost from every store alike: consistent
    "kill_mid_append": (True, False, "malformed"),       # the torn half-row: terminated at the restore, a hole
    "kill_after_append_before_save": (True, False, "flagged"),   # the state behind its journal: a restore gap
    "kill_mid_state_save": (True, False, "flagged"),     # (the same: the new state never replaced the old)
    "kill_mid_shadow_commit": (True, True, None),        # the shadow's torn transaction rolled back, then caught up
    "disk_full_append_recovers": (True, True, None),     # the row went out late, in order, once
    "disk_full_append_then_kill": (True, False, "flagged"),      # the state ahead of its journal: a restore gap
    "disk_full_real_limit": (True, True, None),          # nothing could be written: lost from every store alike
    "source_replaced_identical": (True, True, None),     # a new epoch, the same bytes
    "source_edited_same_length": (True, False, "fault"),
    "source_deleted": (False, False, "missing"),
    "concurrent_partial_writes": (True, True, None),
}


def verdict(r: dict) -> dict:
    """Agreement of the restored authority, its journal's last row and the shadow's cash; the shadow's health; and,
    when unhealthy, its recorded cause - each against EXPECT. Nothing may be silently clean."""
    want_agree, want_healthy, want_cause = EXPECT[r["case"]]
    if r["case"] == "concurrent_partial_writes":
        agree = r["committed"] == r["rows_written"] and r["cash"].get("S-C") == r["expected_cash"]
        healthy, cause = bool(r["healthy"]) and not r["unresolved"], None
    else:
        sh = r.get("shadow") or {}
        cash = [v for k, v in (sh.get("accounts") or {}).items() if k != "S-source"]
        auth = round(r["authority_cash"] * 1e9) if r.get("authority_cash") is not None else None
        j = r.get("journal") or {}
        last = round(j["last_cash_after"] * 1e9) if j.get("last_cash_after") is not None else None
        agree = auth is not None and auth == last and len(cash) == 1 and abs(cash[0] - auth) <= 10
        healthy = bool(sh.get("healthy"))
        states = sh.get("unresolved_by_state") or {}
        cause = ("fault" if sh.get("faults") else sh.get("source") if sh.get("source") != "ok" else
                 next(iter(states), None))
    ok = agree == want_agree and healthy == want_healthy and (want_cause is None or cause == want_cause)
    return {"agree": agree, "healthy": healthy, "cause": cause, "expected": list(EXPECT[r["case"]]), "ok": ok}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=2, metavar=("PHASE", "DIR"))
    ap.add_argument("case", nargs="?")
    ap.add_argument("--case", dest="only")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.child:
        phase, d = a.child
        if phase == "run":
            child_run(Path(d), a.case)
        else:
            child_restart(Path(d))
        return
    from meme_trader.sniper.research import code_revision
    t0 = time.time()
    results = []
    for case in CASES:
        if a.only and case != a.only:
            continue
        r = run_case(case)
        r["verdict"] = verdict(r)
        results.append(r)
        print(f"{case:32} {r['verdict']}", flush=True)
    out = {"what": "the paper shadow's crash matrix at the real adapter boundary (research/shadow_crash_matrix.py)",
           "code": code_revision(), "started_at": round(t0, 3), "seconds": round(time.time() - t0, 1),
           "fault_at_event": FAULT_AT, "events": [k for k, _, _ in STEPS], "results": results,
           "all_ok": all(r["verdict"]["ok"] for r in results)}
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"all_ok": out["all_ok"], "seconds": out["seconds"]}))


if __name__ == "__main__":
    main()
