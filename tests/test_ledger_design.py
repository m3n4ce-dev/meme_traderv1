"""The ledger DESIGN (research/ledger: schema.sql, reference.py, posting_vectors.json), proved on real SQLite: exact
signed integer postings that must balance per asset, sealed in one transaction; append-only history; one unresolved
attempt per order; contradictory chain observations kept, identical ones deduped. Revision 3 (an eleventh review):
caller-stable keys and per-effect uniqueness, nonnegative holdings, frozen seals recomputed at startup, append-only
observations, signed bytes and proof-backed attempt transitions, the opening at actual cash. Not the bot's book (yet)."""
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
                     {"name": ev["name"]}, key=ev.get("key"), effects=[tuple(x) for x in ev.get("effects", [])])


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
                                    ("external:owner", "SOL", -1, False)], {}, key="big")
    assert ledger.balances("A1")[("A1:cash", "SOL")] == 1
    s, t = ledger.db.execute("SELECT SUM(amount), typeof(SUM(amount)) FROM postings").fetchone()
    assert t == "real" and s != 1                                               # why the writer never uses SQL SUM


def test_an_unbalanced_event_leaves_nothing_behind(ledger):
    with pytest.raises(ref.LedgerError, match="balance"):
        ledger.append("A1", "deposit", [("A1:cash", "SOL", 5, False), ("external:owner", "SOL", -4, False)], {},
                      key="unbalanced")
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
        ledger.append("A1", "deposit", [("A2:cash", "SOL", 1, False), ("A1:cash", "SOL", -1, False)], {}, key="x")


def test_one_unresolved_attempt_per_order_and_a_new_one_only_after_resolution(ledger):
    ledger.add_order("O1", "A1", "sell", "MINT", 1000)
    ledger.sign("T1", "O1", "S1", "bh", 100, b"signed-1")
    ledger.settle("T1", "submitted")
    ledger.settle("T1", "unknown")                                               # e.g. restored after a restart
    with pytest.raises(sqlite3.IntegrityError):
        ledger.sign("T2", "O1", "S2", "bh", 100, b"signed-2")                    # never a fresh signature while unknown
    proof = ledger.observe("S1", None, "finalized", "getSignatureStatuses@rpc", None, "absent", None, block_height=101)
    ledger.settle("T1", "expired_absent", proof)                                 # proved expired and absent
    ledger.sign("T2", "O1", "S2", "bh", 100, b"signed-2")


def test_contradictory_observations_are_kept_and_identical_ones_dedupe(ledger):
    a = ledger.observe("S", 0, "finalized", "getTransaction@rpc", 100, None, "aa")
    assert ledger.observe("S", 0, "finalized", "getTransaction@rpc", 100, None, "aa") == a      # identical: the same row
    assert ledger.observe("S", 0, "finalized", "getTransaction@rpc", 100, None, "bb") != a      # contradiction: kept
    assert ledger.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    for sql in ("UPDATE observations SET slot = 1", "DELETE FROM observations"):               # (rev 3) immutable
        with pytest.raises(sqlite3.IntegrityError):
            ledger.db.execute(sql)


def test_an_open_event_at_startup_fails_closed(ledger):
    ledger.db.execute("INSERT INTO events (event_id, account_id, kind, status, schema_version, code_revision, "
                      "observed_at, recorded_at, payload, payload_sha256) VALUES ('x', 'A1', 'k', 'open', 1, 'r', 0, 0, "
                      "'{}', '')")
    with pytest.raises(ref.LedgerError, match="fail closed"):
        ledger.startup_check()


# --------------------------------------------------------------------------- revision 3 (an eleventh review)
def test_the_opening_is_the_observed_cash_and_the_residual_moves_none(ledger):
    """Independent of the vectors' own legs: after adoption and the residual, cash equals the source's observed cash,
    the unresolved item equals legacy expected minus observed, and the opening book carries the legacy total."""
    snap = VECTORS["source_snapshot"]
    for ev in VECTORS["events"][:2]:
        apply(ledger, ev)
    bal = ledger.balances()
    assert bal[("A1:cash", "SOL")] == snap["observed_cash"]
    assert bal[("A1:unresolved", "SOL")] == snap["legacy_expected_cash"] - snap["observed_cash"] == 8_040_000
    assert bal[("external:opening", "SOL")] == -snap["legacy_expected_cash"]
    assert "ACTUAL" in VECTORS["opening_basis"]


def deposit(ledger, n=10, key="S:transfer", **kw):
    return ledger.append("A1", "deposit", [("A1:cash", "SOL", n), ("external:owner", "SOL", -n)], {"signature": "S"},
                         key=key, **kw)


def test_a_replay_returns_the_original_and_other_content_under_its_key_is_a_conflict(ledger):
    first = deposit(ledger)
    assert deposit(ledger) == first and ledger.balances("A1")[("A1:cash", "SOL")] == 10
    with pytest.raises(ref.LedgerError, match="conflict"):
        deposit(ledger, n=11)
    assert ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    with pytest.raises(ref.LedgerError, match="stable key"):
        ledger.append("A1", "deposit", [("A1:cash", "SOL", 1), ("external:owner", "SOL", -1)], {})


def test_a_crash_after_commit_and_before_the_acknowledgement_is_retried_once(ledger):
    def crash(point):
        if point == "committed":
            raise RuntimeError("process died before the caller heard back")
    ledger.fault = crash
    with pytest.raises(RuntimeError):
        deposit(ledger)
    ledger.fault = None
    seq = deposit(ledger)                                                        # the retry finds it
    assert ledger.balances("A1")[("A1:cash", "SOL")] == 10 and seq == 1
    ledger.startup_check()


@pytest.mark.parametrize("point", ["open", "postings", "sealed"])
def test_a_crash_inside_the_transaction_leaves_nothing_and_the_retry_succeeds(ledger, point):
    def crash(p):
        if p == point:
            raise RuntimeError(p)
    ledger.fault = crash
    with pytest.raises(RuntimeError):
        deposit(ledger)
    assert ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
    ledger.fault = None
    deposit(ledger)
    assert ledger.balances("A1")[("A1:cash", "SOL")] == 10
    ledger.startup_check()


def test_a_locked_database_fails_the_append_cleanly_and_a_later_retry_succeeds(tmp_path):
    lg = ref.Ledger(tmp_path / "l.db")
    for a in VECTORS["accounts"]:
        lg.add_account(a["account_id"], a["mode"], a["genesis"], a["owner"])
    for a in VECTORS["assets"]:
        lg.add_asset(a["asset"], a["program"], a["decimals"])
    for b in VECTORS["books"]:
        lg.add_book(*b)
    lg.db.execute("PRAGMA busy_timeout = 1")
    other = sqlite3.connect(tmp_path / "l.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        deposit(lg)
    other.execute("ROLLBACK")
    assert deposit(lg) == 1 and lg.balances("A1")[("A1:cash", "SOL")] == 10


def test_each_fill_and_each_signatures_fee_is_booked_once_whatever_event_carries_it(ledger):
    deposit(ledger, n=10 ** 9)

    def buy(key, fill, fee=True):
        return ledger.append("A1", "buy", [("A1:cash", "SOL", -1000), ("external:pool", "SOL", 1000),
                                           ("external:pool", "MINT", -5), ("A1:inventory", "MINT", 5)],
                             {"k": key}, key=key, effects=[("fill", fill)] + ([("network_fee", "B")] if fee else []))
    buy("B:fill:0", "B:0")
    buy("B:fill:1", "B:1", fee=False)                                             # two fills in one signature: fine
    with pytest.raises(ref.LedgerError, match="already booked"):
        buy("B:fill:0 again", "B:0", fee=False)                                   # the same fill under another key
    with pytest.raises(ref.LedgerError, match="already booked"):
        buy("B:fill:2", "B:2")                                                    # the signature's fee twice
    assert ledger.balances("A1")[("A1:inventory", "MINT")] == 10


def test_balanced_postings_cant_create_negative_holdings_but_counterparties_can_go_negative(ledger):
    with pytest.raises(ref.LedgerError, match="negative"):
        ledger.append("A1", "sell", [("A1:inventory", "MINT", -1), ("external:pool", "MINT", 1)], {}, key="S:fill:0")
    with pytest.raises(ref.LedgerError, match="negative"):
        ledger.append("A1", "reserve", [("A1:cash", "SOL", -1), ("A1:reserved", "SOL", 1)], {}, key="O:reserve")
    deposit(ledger)                                                               # external:owner goes to -10: fine
    assert ledger.balances()[("external:owner", "SOL")] == -10
    assert ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def raw_event(db, eid="raw", status="open"):
    return db.execute("INSERT INTO events (event_id, account_id, kind, status, schema_version, code_revision, "
                      "observed_at, recorded_at, payload, payload_sha256) VALUES (?, 'A1', 'test', ?, 1, 'r', 0, 0, "
                      "'{}', 'h')", (eid, status)).lastrowid


@pytest.mark.parametrize("change", ["event_id = 'changed'", "schema_version = 99", "code_revision = 'x'",
                                    "account_id = 'A2'", "kind = 'other'", "payload = '[]'", "recorded_at = 5",
                                    "effect_key = 'k'", "content_sha256 = 'c'", "causation_id = 'c'"])
def test_sealing_freezes_every_other_column_and_events_start_open(ledger, change):
    raw_event(ledger.db)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.db.execute(f"UPDATE events SET status = 'sealed', seal_sums = '{{}}', seal_sha256 = 'h', {change} "
                          "WHERE event_id = 'raw'")
    with pytest.raises(sqlite3.IntegrityError):
        raw_event(ledger.db, "born-sealed", status="sealed")


def test_startup_recomputes_every_seal_and_fails_closed_on_forgery_or_corruption(ledger):
    for ev in VECTORS["events"]:
        apply(ledger, ev)
    assert ledger.startup_check()["events"] == len(VECTORS["events"])
    seq = raw_event(ledger.db, "forged")                                          # a bad writer: unbalanced, fake seal
    ledger.db.execute("INSERT INTO postings VALUES (?, 0, 'A1:cash', 'SOL', '5')", (seq,))
    ledger.db.execute("UPDATE events SET status = 'sealed', seal_sums = '{\"SOL\": \"0\"}', seal_sha256 = 'x' "
                      "WHERE seq = ?", (seq,))
    with pytest.raises(ref.LedgerError, match="fail closed") as e:
        ledger.startup_check()
    assert "don't balance" in str(e.value) and "not written by the writer" in str(e.value)


def test_startup_catches_history_edited_around_the_triggers(ledger):
    """A bad migration that drops the guards and edits a sealed posting: the triggers can't see it; the recomputed
    hashes do. (A writer able to rewrite the whole file, hashes included, defeats any in-file check.)"""
    for ev in VECTORS["events"][:3]:
        apply(ledger, ev)
    ledger.db.execute("DROP TRIGGER postings_no_update")
    ledger.db.execute("UPDATE postings SET amount = '3472391525' WHERE seq = 1 AND leg = 1")
    with pytest.raises(ref.LedgerError, match="hash mismatch|don't balance"):
        ledger.startup_check()


def test_attempts_keep_their_signed_bytes_and_move_only_with_proof(ledger):
    db = ledger.db
    ledger.add_order("O1", "A1", "buy", "SOL", 250_000_000)
    with pytest.raises(sqlite3.IntegrityError):                                   # a hash alone can't be rebroadcast
        db.execute("INSERT INTO attempts (attempt_id, order_id, signature, blockhash, last_valid_height, signed_tx, "
                   "tx_sha256, state, created_at) VALUES ('T0', 'O1', 'S0', 'bh', 100, NULL, 'h', 'signed', 0)")
    ledger.sign("T1", "O1", "S1", "bh", 100, b"\x01" * 200)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.settle("T1", "landed_ok")                                          # signed -> landed: not a transition
    ledger.settle("T1", "submitted")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.settle("T1", "landed_ok")                                          # no observation
    early = ledger.observe("S1", None, "finalized", "getSignatureStatuses@rpc", None, "absent", None, block_height=100)
    unsure = ledger.observe("S1", None, "confirmed", "getSignatureStatuses@rpc", None, "absent", None, block_height=150)
    other = ledger.observe("S9", None, "finalized", "getSignatureStatuses@rpc", None, "absent", None, block_height=150)
    for obs in (early, unsure, other):                                            # not proof of expiry
        with pytest.raises(sqlite3.IntegrityError):
            ledger.settle("T1", "expired_absent", obs)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE attempts SET signed_tx = x'00' WHERE attempt_id = 'T1'")  # the transaction is immutable
    proof = ledger.observe("S1", None, "finalized", "getSignatureStatuses@rpc", None, "absent", None, block_height=101)
    ledger.settle("T1", "expired_absent", proof)
    ledger.startup_check()
    db.execute("DROP TRIGGER attempts_frozen")                                     # corruption around the guard
    db.execute("UPDATE attempts SET signed_tx = x'02' WHERE attempt_id = 'T1'")
    with pytest.raises(ref.LedgerError, match="signed transaction"):
        ledger.startup_check()


def test_one_live_sell_per_position(ledger):
    ledger.add_order("X1", "A1", "sell", "MINT", 10)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add_order("X2", "A1", "sell", "MINT", 10)
    ledger.db.execute("UPDATE orders SET state = 'done' WHERE order_id = 'X1'")
    ledger.add_order("X2", "A1", "sell", "MINT", 10)
