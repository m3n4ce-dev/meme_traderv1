"""The ledger DESIGN (research/ledger: schema.sql, reference.py, posting_vectors.json), proved on real SQLite: exact
signed integer postings that must balance per asset, sealed in one transaction; append-only history; one unresolved
attempt per order; contradictory chain observations kept, identical ones deduped. Not the bot's book (yet)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "research" / "ledger"
spec = importlib.util.spec_from_file_location("ledger_reference", ROOT / "reference.py")
ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ref)
VECTORS = json.loads((ROOT / "posting_vectors.json").read_text())


@pytest.fixture
def ledger():
    lg = ref.Ledger()
    for a in VECTORS["accounts"]:
        lg.add_account(a["account_id"], a["mode"], a["genesis"], a["owner"])
    for a in VECTORS["assets"]:
        lg.add_asset(a["asset"], a["program"], a["decimals"])
    for b in VECTORS["books"]:
        lg.add_book(*b)
    return lg


def apply(lg, ev):
    return lg.append(ev["account"], ev["kind"], [(b, a, x, b.startswith("external")) for b, a, x in ev["legs"]],
                     {"name": ev["name"]})


def test_every_worked_posting_balances_and_the_books_end_where_they_should(ledger):
    for ev in VECTORS["events"]:
        apply(ledger, ev)
    bal = {f"{b}|{a}": v for (b, a), v in ledger.balances("A1").items()}
    assert bal == VECTORS["expected_balances_A1"]                              # inventory, reserved, rent: all back to 0
    assert ledger.db.execute("SELECT COUNT(*) FROM events WHERE status != 'sealed'").fetchone()[0] == 0
    ledger.startup_check()


def test_balances_are_exact_where_sqlite_sum_is_not(ledger):
    big = (1 << 64) - 1
    ledger.append("A1", "deposit", [("A1:cash", "SOL", big, False), ("A1:cash", "SOL", -(big - 1), False),
                                    ("external:owner", "SOL", -1, False)], {})
    assert ledger.balances("A1")[("A1:cash", "SOL")] == 1
    s, t = ledger.db.execute("SELECT SUM(amount), typeof(SUM(amount)) FROM postings").fetchone()
    assert t == "real" and s != 1                                               # why the writer never uses SQL SUM


def test_an_unbalanced_event_leaves_nothing_behind(ledger):
    with pytest.raises(ref.LedgerError, match="balance"):
        ledger.append("A1", "deposit", [("A1:cash", "SOL", 5, False), ("external:owner", "SOL", -4, False)], {})
    assert ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["+5", "05", "-0", "1.5", "1e3", "", "--5", "5-"])
def test_only_canonical_signed_integers_are_stored(ledger, bad):
    seq = ledger.db.execute("INSERT INTO events (event_id, account_id, kind, status, schema_version, code_revision, "
                            "observed_at, recorded_at, payload, payload_sha256) VALUES ('x', 'A1', 'k', 'open', 1, 'r', "
                            "0, 0, '{}', '')").lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        ledger.db.execute("INSERT INTO postings VALUES (?, 0, 'A1:cash', 'SOL', ?)", (seq, bad))


def test_history_is_append_only_and_postings_only_go_into_open_events(ledger):
    seq = apply(ledger, VECTORS["events"][0])
    for sql in ("UPDATE postings SET amount = '1' WHERE seq = ?", "DELETE FROM postings WHERE seq = ?",
                "DELETE FROM events WHERE seq = ?", "UPDATE events SET payload = 'x' WHERE seq = ?",
                "INSERT INTO postings VALUES (?, 9, 'A1:cash', 'SOL', '1')"):
        with pytest.raises(sqlite3.IntegrityError):
            ledger.db.execute(sql, (seq,))


def test_a_correction_is_a_linked_reversal_and_postings_cant_cross_accounts(ledger):
    seq = apply(ledger, VECTORS["events"][2])                                    # a deposit
    rev = ledger.reverse(seq, {"why": "fixture"})
    assert ledger.db.execute("SELECT corrects FROM events WHERE seq = ?", (rev,)).fetchone()[0] == seq
    assert ("A1:cash", "SOL") not in ledger.balances("A1")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.append("A1", "deposit", [("A2:cash", "SOL", 1, False), ("A1:cash", "SOL", -1, False)], {})


def test_one_unresolved_attempt_per_order_and_a_new_one_only_after_resolution(ledger):
    db = ledger.db
    db.execute("INSERT INTO orders VALUES ('O1', 'A1', 'sell', 'MINT', '1000', '0', 'active', 0)")

    def attempt(i, state):
        db.execute("INSERT INTO attempts VALUES (?, 'O1', ?, 'bh', 100, 'tx', ?, 0)", (f"T{i}", f"S{i}", state))
    attempt(1, "unknown")
    with pytest.raises(sqlite3.IntegrityError):
        attempt(2, "signed")                                                     # never a fresh signature while unknown
    db.execute("UPDATE attempts SET state = 'expired_absent' WHERE attempt_id = 'T1'")   # proved expired and absent
    attempt(2, "signed")


def test_contradictory_observations_are_kept_and_identical_ones_dedupe(ledger):
    assert ledger.observe("S", 0, "finalized", "getTransaction@rpc", 100, None, "aa")
    assert not ledger.observe("S", 0, "finalized", "getTransaction@rpc", 100, None, "aa")       # identical
    assert ledger.observe("S", 0, "finalized", "getTransaction@rpc", 100, None, "bb")           # contradiction: kept
    assert ledger.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_an_open_event_at_startup_fails_closed(ledger):
    ledger.db.execute("INSERT INTO events (event_id, account_id, kind, status, schema_version, code_revision, "
                      "observed_at, recorded_at, payload, payload_sha256) VALUES ('x', 'A1', 'k', 'open', 1, 'r', 0, 0, "
                      "'{}', '')")
    with pytest.raises(ref.LedgerError, match="fail closed"):
        ledger.startup_check()
