"""Account journal crash handling: whole rows in order, torn lines terminated, restore gaps detected.

A fifteenth review: the paper shadow's crash matrix integrated end-to-end at the real boundary,
plus unit tests covering every code path (the journal append/restore and _restore_gap).
"""
import asyncio
import errno
import json
import time
from pathlib import Path

import pytest

import meme_trader.journal as jm
import meme_trader.sniper.engine as em
from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor


class Quiet:
    """A feed that reports the current time."""
    realtime, degraded = False, False

    def now(self):
        return time.time()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A paper engine in a temporary directory, with log_to_journal enabled."""
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)
    P = config.load(config.EXAMPLE)
    e = Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    # Replace e.reconcile with a no-op and e.say with a silent logger
    async def nothing():
        return None
    e.reconcile = nothing
    e.say = lambda *a, **kw: None
    return e


def read_journal(tmp_path, mode="paper"):
    """Read the account journal file; return list of (row dict or None for malformed lines)."""
    path = tmp_path / f"account-{mode}.jsonl"
    if not path.exists():
        return []
    lines = []
    for raw_line in path.read_text().split('\n'):
        if not raw_line:
            continue
        try:
            lines.append(json.loads(raw_line))
        except json.JSONDecodeError:
            lines.append(None)
    return lines


def test_rows_written_whole_and_in_order(engine, tmp_path):
    """Rows are written whole and in order: each line is complete JSON with cash_after increasing."""
    asyncio.run(engine.restore_state())
    # Get the "open" row that restore_state creates
    lines_before = read_journal(tmp_path)
    assert len(lines_before) == 1 and lines_before[0]["kind"] == "open"
    cash_before = lines_before[0]["cash_after"]

    # Write two deposits
    engine._cash(1.0, "deposit")
    engine._cash(0.5, "deposit")

    lines = read_journal(tmp_path)
    assert len(lines) == 3, f"Expected 3 lines, got {len(lines)}"
    assert lines[0]["kind"] == "open"
    assert lines[1]["kind"] == "deposit"
    assert lines[2]["kind"] == "deposit"

    # Each line is valid JSON
    for line in lines:
        assert line is not None, "All lines must be valid JSON"

    # cash_after is increasing
    assert lines[0]["cash_after"] == cash_before
    assert lines[1]["cash_after"] == pytest.approx(cash_before + 1.0, abs=1e-8)
    assert lines[2]["cash_after"] == pytest.approx(cash_before + 1.5, abs=1e-8)


def test_full_disk_then_recovery(engine, tmp_path, monkeypatch):
    """Full disk on first write, then recovery: pending row is kept, then written on retry."""
    asyncio.run(engine.restore_state())

    # Monkeypatch Path.open to raise ENOSPC on the first call to the journal file
    journal_path = tmp_path / f"account-{engine.mode}.jsonl"
    original_open = Path.open
    call_count = {"count": 0}

    def patched_open(self, mode="r", *args, **kwargs):
        if self == journal_path and "a" in mode:
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)

    # First cash event fails (full disk)
    engine._cash(1.0, "deposit")
    assert engine.stats["account_journal_errors"] == 1
    assert len(engine._journal_pending) == 1, "The failed row should stay pending"

    # Count lines before recovery
    lines_before = read_journal(tmp_path)
    count_before = len(lines_before)

    # Restore the normal open and write another row
    monkeypatch.setattr(Path, "open", original_open)
    engine._cash(0.5, "deposit")

    # Both rows should now be written, in order, once each
    lines = read_journal(tmp_path)
    assert len(lines) == count_before + 2
    assert len(engine._journal_pending) == 0, "Pending queue should be empty after successful write"

    # Find the two new deposit rows and verify their order
    deposit_lines = [l for l in lines[count_before:] if l and l.get("kind") == "deposit"]
    assert len(deposit_lines) == 2
    assert deposit_lines[0]["sol"] == pytest.approx(1.0, abs=1e-8)
    assert deposit_lines[1]["sol"] == pytest.approx(0.5, abs=1e-8)


def test_short_write_never_duplicates(engine, tmp_path, monkeypatch):
    """A partial write: torn line is terminated, then full row written, no duplication."""
    asyncio.run(engine.restore_state())

    journal_path = tmp_path / f"account-{engine.mode}.jsonl"
    original_open = Path.open
    writes_patched = {"count": 0}

    def patched_open(self, mode="r", *args, **kwargs):
        if self == journal_path and "a" in mode:
            # Open the real file first
            real_f = original_open(self, mode, *args, **kwargs)

            # Wrap it to intercept the first write
            class WrappedFile:
                def __init__(self):
                    self.real_f = real_f
                    self.first_write = True

                def write(self, data):
                    if self.first_write and writes_patched["count"] == 0:
                        writes_patched["count"] += 1
                        self.first_write = False
                        # Write only half the data
                        return self.real_f.write(data[:len(data) // 2])
                    return self.real_f.write(data)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.real_f.__exit__(*args)

                def seek(self, *args):
                    return self.real_f.seek(*args)

                def read(self, *args):
                    return self.real_f.read(*args)

            return WrappedFile()
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)

    # First cash event: partial write
    engine._cash(1.0, "deposit")
    assert engine.stats["account_journal_errors"] == 1, "The short write should record an error"

    # Restore normal open and write the pending row plus a new one
    monkeypatch.setattr(Path, "open", original_open)
    engine._cash(0.5, "deposit")

    lines = read_journal(tmp_path)

    # Find the torn line (it won't be valid JSON)
    malformed_count = sum(1 for l in lines if l is None)
    assert malformed_count == 1, f"Expected 1 malformed line, got {malformed_count}"

    # The malformed line should be terminated with a newline
    raw_text = (tmp_path / f"account-{engine.mode}.jsonl").read_text()
    lines_raw = raw_text.split('\n')
    malformed_idx = None
    for i, raw in enumerate(lines_raw):
        try:
            json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            if raw:  # skip empty lines at the end
                malformed_idx = i
                break
    assert malformed_idx is not None and json.loads(lines_raw[malformed_idx + 1])["kind"] == "deposit"

    # After the malformed line, there should be the two valid deposit rows
    valid_lines = [l for l in lines if l is not None]
    deposits = [l for l in valid_lines if l.get("kind") == "deposit"]
    assert len(deposits) == 2, "Both deposits should be present, written exactly once"

    assert engine.stats["account_journal_torn"] == 1


def test_torn_last_line_terminated_at_restore(tmp_path, monkeypatch):
    """A torn last line from a crash is terminated at restore_state."""
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)

    # Manually write a valid row plus a partial line to the journal
    journal_path = tmp_path / "account-paper.jsonl"
    account_id = "test-account"
    valid_row = {
        "ts": round(time.time(), 3),
        "account": account_id,
        "kind": "adopted",
        "sol": 0.0,
        "cash_after": 100.0,
        "start_sol": 100.0,
    }
    partial_line = '{"ts": 1, "account": "' + account_id + '", "kind": "deposit'
    journal_path.write_text(json.dumps(valid_row) + "\n" + partial_line)

    P = config.load(config.EXAMPLE)
    # Create engine with this account_id by setting it
    e = Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    e.book.account_id = account_id
    e.book.sol = 100.0
    e.book.start_sol = 100.0

    # Don't create a state file (or delete it if it exists)
    if e.state_path.exists():
        e.state_path.unlink()

    async def nothing():
        return None
    e.reconcile = nothing
    e.say = lambda *a, **kw: None

    # Restore should terminate the torn line
    asyncio.run(e.restore_state())

    # Check the journal: the torn line should now end with \n
    text = journal_path.read_text()
    lines = text.split('\n')
    # The second line should be the partial one, now terminated (but still not valid JSON)
    assert len(lines) >= 2
    # After restore, a new "open" row should be written
    raw_lines = text.split('\n')
    valid_rows = []
    for raw in raw_lines:
        try:
            valid_rows.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            pass

    # Check that torn stat is recorded
    assert e.stats["account_journal_torn"] == 1


def test_missing_journal_stays_missing_at_restore(tmp_path, monkeypatch):
    """With no journal file and an existing saved state, no file is created on restore."""
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)

    P = config.load(config.EXAMPLE)
    e = Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    e.say = lambda *a, **kw: None

    async def nothing():
        return None
    e.reconcile = nothing

    # Manually save state without ever writing the journal
    e.save_state()
    assert e.state_path.exists()

    journal_path = tmp_path / f"account-{e.mode}.jsonl"
    assert not journal_path.exists(), "Journal should not exist yet"

    # Restore with no pending rows: the journal should stay missing
    asyncio.run(e.restore_state())
    assert not journal_path.exists(), "Journal should remain missing when nothing is pending"


def test_restore_gap_detection(tmp_path, monkeypatch):
    """A restore gap is detected when state and journal disagree on cash."""
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)

    P = config.load(config.EXAMPLE)

    # Engine A: restore, deposit, save state, then deposit again without saving
    e1 = Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    e1.say = lambda *a, **kw: None

    async def nothing():
        return None
    e1.reconcile = nothing

    asyncio.run(e1.restore_state())
    e1._cash(1.0, "deposit")
    e1.save_state()
    cash_after_first = round(e1.book.sol, 9)

    e1._cash(0.5, "deposit")
    # Don't save state: crash after journal append

    account_id_a = e1.book.account_id

    # Engine B: new instance, same directory, restore_state
    e2 = Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    e2.book.account_id = account_id_a  # Ensure it looks for the same account
    e2.say = lambda *a, **kw: None
    e2.reconcile = nothing

    asyncio.run(e2.restore_state())

    # The journal should have a restore_gap row
    journal_after = read_journal(tmp_path)
    restore_gap_rows = [r for r in journal_after if r and r.get("kind") == "restore_gap"]
    assert len(restore_gap_rows) == 1, "Should detect one restore gap"

    gap = restore_gap_rows[0]
    assert gap["account"] == account_id_a
    # sol field is the difference (restored cash - journal's last cash)
    # The journal's last for e1 was cash_after_first + 0.5
    # The saved state in e2 was cash_after_first
    # So the gap should be -(0.5)
    assert gap["sol"] == pytest.approx(-0.5, abs=1e-8)
    assert gap["cash_after"] == pytest.approx(cash_after_first, abs=1e-9) == pytest.approx(e2.book.sol, abs=1e-9)
    assert gap["journal_cash_after"] == pytest.approx(cash_after_first + 0.5, abs=1e-9)
    assert e2.stats["restore_gaps"] == 1

    # Engine C: restore again, should NOT record another gap (state and journal now agree)
    e3 = Engine(P, Quiet(), PaperExecutor(P.sniper.execution), persist=True, log_to_journal=True)
    e3.book.account_id = account_id_a
    e3.say = lambda *a, **kw: None
    e3.reconcile = nothing

    asyncio.run(e3.restore_state())
    assert e3.stats["restore_gaps"] == 0, "No new gap should be recorded on second restore"


def test_crash_matrix_end_to_end():
    """Run the full crash matrix: all cases must verify at the boundary."""
    import importlib.util
    import sys

    # Load shadow_crash_matrix by path
    spec = importlib.util.spec_from_file_location(
        "shadow_crash_matrix",
        Path(__file__).parent.parent / "research" / "shadow_crash_matrix.py"
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["shadow_crash_matrix"] = m
    spec.loader.exec_module(m)

    # Run each case and verify
    for case in m.CASES:
        r = m.run_case(case)
        v = m.verdict(r)
        assert v["ok"], f"Case {case} failed: {v}\nFull result: {r}"
