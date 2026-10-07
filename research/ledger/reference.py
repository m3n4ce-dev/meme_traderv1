"""A reference writer for the ledger design (schema.sql, docs/LEDGER_DESIGN.md): it exists to prove the design's
rules on real SQLite, not to run the bot. The protocol, in ONE transaction per economic event:
1. insert the event as 'open' (seq = the database's commit order);
2. insert its postings (canonical signed integer text; the schema rejects anything else);
3. sum each asset's postings with exact Python integers - never SQL SUM(), which turns past int64 into floating point
   - and refuse unless every asset nets to exactly 0;
4. seal the event with those sums and the postings' sha256; commit.
Any failure rolls the whole event back: no open event survives a commit, and one found at startup means corruption.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1
ON_CHAIN_MAX = (1 << 64) - 1          # one instruction's amount (u64); books themselves may hold wider aggregates
BOOK_MAX = (1 << 127) - 1


class LedgerError(Exception):
    pass


def canonical(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int):
        raise LedgerError(f"{n!r} is not an integer amount")
    return str(n)


class Ledger:
    def __init__(self, path: str | Path = ":memory:", code_revision: str = "reference"):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA.read_text())
        self.code = code_revision

    def startup_check(self) -> None:
        n = self.db.execute("SELECT COUNT(*) FROM events WHERE status = 'open'").fetchone()[0]
        if n:
            raise LedgerError(f"{n} open event(s) at startup: the ledger is corrupt; refusing to run (fail closed)")

    # ---- setup
    def add_account(self, account_id: str, mode: str, genesis: str, owner: str, strategy: str = "") -> None:
        self.db.execute("INSERT INTO accounts (account_id, mode, genesis, owner, strategy) VALUES (?, ?, ?, ?, ?)",
                        (account_id, mode, genesis, owner, strategy))

    def add_asset(self, asset: str, program: str, decimals: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO assets VALUES (?, ?, ?)", (asset, program, decimals))

    def add_book(self, book_id: str, account_id: str | None, kind: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO books VALUES (?, ?, ?)", (book_id, account_id, kind))

    # ---- the one writer path for economics
    def append(self, account_id: str, kind: str, legs: list[tuple], payload: dict, corrects: int | None = None,
               observed_at: float | None = None, causation: str = "", correlation: str = "") -> int:
        """legs: [(book_id, asset, int amount, onchain: bool)]. Returns the sealed event's seq."""
        now = time.time()
        body = json.dumps(payload, sort_keys=True)
        eid = hashlib.sha256(f"{account_id}|{kind}|{body}|{now}".encode()).hexdigest()[:26]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "INSERT INTO events (event_id, account_id, kind, status, schema_version, code_revision, causation_id, "
                "correlation_id, corrects, observed_at, recorded_at, payload, payload_sha256) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, account_id, kind, SCHEMA_VERSION, self.code, causation, correlation, corrects,
                 observed_at or now, now, body, hashlib.sha256(body.encode()).hexdigest()))
            seq = cur.lastrowid
            sums: dict[str, int] = {}
            canon = []
            for i, (book, asset, amount, onchain) in enumerate(legs):
                text = canonical(amount)
                if abs(amount) > (ON_CHAIN_MAX if onchain else BOOK_MAX):
                    raise LedgerError(f"leg {i}: {amount} out of range")
                self.db.execute("INSERT INTO postings (seq, leg, book_id, asset, amount) VALUES (?, ?, ?, ?, ?)",
                                (seq, i, book, asset, text))
                sums[asset] = sums.get(asset, 0) + amount        # exact: Python integers
                canon.append([book, asset, text])
            if not legs or any(v != 0 for v in sums.values()):
                raise LedgerError(f"event doesn't balance: {sums}")
            self.db.execute("UPDATE events SET status = 'sealed', seal_sums = ?, seal_sha256 = ? WHERE seq = ?",
                            (json.dumps({a: "0" for a in sorted(sums)}),
                             hashlib.sha256(json.dumps(canon).encode()).hexdigest(), seq))
            self.db.execute("COMMIT")
            return seq
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reverse(self, seq: int, payload: dict) -> int:
        """A correction: the original stays; a new event posts its exact negation, linked by `corrects`."""
        acct, = self.db.execute("SELECT account_id FROM events WHERE seq = ?", (seq,)).fetchone()
        legs = [(b, a, -int(x), False) for b, a, x in self.db.execute(
            "SELECT book_id, asset, amount FROM postings WHERE seq = ? ORDER BY leg", (seq,))]
        return self.append(acct, "fork_correction", legs, payload, corrects=seq)

    # ---- reads (exact, sealed only)
    def balances(self, account_id: str | None = None) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        q = ("SELECT p.book_id, p.asset, p.amount FROM postings p JOIN events e ON e.seq = p.seq "
             "JOIN books b ON b.book_id = p.book_id WHERE e.status = 'sealed'")
        args: tuple = ()
        if account_id is not None:
            q += " AND b.account_id = ?"
            args = (account_id,)
        for book, asset, amount in self.db.execute(q, args):
            out[(book, asset)] = out.get((book, asset), 0) + int(amount)
        return {k: v for k, v in out.items() if v}

    # ---- chain observations
    def observe(self, signature: str, event_index, commitment: str, source: str, slot, err, content_sha256,
                observed_at: float | None = None) -> bool:
        """Append an observation; True if new. Identical observations dedupe by digest; a contradictory one (same
        source and commitment, other slot/error/content) is kept beside the first, for ranking - never refused."""
        digest = hashlib.sha256(json.dumps([signature, event_index, commitment, source, slot, err, content_sha256])
                                .encode()).hexdigest()
        cur = self.db.execute("INSERT OR IGNORE INTO observations (signature, event_index, commitment, source, slot, err, "
                              "content_sha256, observed_at, digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (signature, event_index, commitment, source, slot, err, content_sha256,
                               observed_at or time.time(), digest))
        return bool(cur.rowcount)
