"""A reference writer for the ledger design (schema.sql, docs/LEDGER_DESIGN.md revision 3): it exists to prove the
design's rules on real SQLite, not to run the bot. The protocol, in ONE transaction per economic event:
1. look the event up by its caller-supplied stable key (account, kind, key): the same content again is a replay - the
   original is returned and nothing is written (a crash after commit and before the acknowledgement, a replayed
   receipt); different content under the same key is a conflict, never a second event;
2. insert the event as 'open' (seq = the database's commit order; its id derives from the key, not the clock);
3. insert its postings (canonical signed integer text; the schema rejects anything else) and its effects (each fill
   and each signature's network fee once per account, whatever event carries it);
4. sum each asset's postings with exact Python integers - never SQL SUM(), which turns past int64 into floating point
   - and refuse unless every asset nets to exactly 0, and unless every holding it touches (cash, reserved, inventory,
   rent) stays >= 0 after it;
5. seal the event with those sums and the postings' sha256; commit.
Any failure rolls the whole event back. Startup recomputes every sealed event's sums and hashes (and the stored signed
transactions' hashes) and refuses to run on any inconsistency - fail closed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 3
ON_CHAIN_MAX = (1 << 64) - 1          # one instruction's amount (u64); books themselves may hold wider aggregates
BOOK_MAX = (1 << 127) - 1
NONNEGATIVE = ("cash", "reserved", "inventory", "rent")    # an account's holdings; external and unresolved books may
                                                           # go negative (they are counterparties and suspense items)
ONCE_PER_ACCOUNT = ("adoption", "residual")                # administrative events with no receipt: keyed by the kind


class LedgerError(Exception):
    pass


def canonical(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int):
        raise LedgerError(f"{n!r} is not an integer amount")
    return str(n)


def _sha(x) -> str:
    return hashlib.sha256((x if isinstance(x, str) else json.dumps(x, sort_keys=True)).encode()).hexdigest()


def content_hash(account_id: str, kind: str, legs: list, payload) -> str:
    """What makes two appends the same economic event: account, kind, ordered legs and payload."""
    return _sha([account_id, kind, [[b, a, str(x)] for b, a, x in legs], payload])


class Ledger:
    def __init__(self, path: str | Path = ":memory:", code_revision: str = "reference", fault=None):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA.read_text())
        self.code = code_revision
        self.fault = fault                # tests: fault(point) may raise at 'open', 'postings', 'sealed', 'committed'

    def _at(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)

    # ---- startup: recompute, don't trust
    def startup_check(self) -> dict:
        """Every sealed event re-derived from its rows: payload hash, content hash, per-asset sums (exactly 0 and as
        sealed), postings hash; every holding >= 0; every stored signed transaction matching its hash. No open event.
        Raises LedgerError naming the first problems (fail closed); else returns what was checked."""
        problems: list[str] = []
        n = self.db.execute("SELECT COUNT(*) FROM events WHERE status = 'open'").fetchone()[0]
        if n:
            problems.append(f"{n} open event(s)")
        posts: dict[int, list] = {}
        for seq, book, asset, amount in self.db.execute("SELECT seq, book_id, asset, amount FROM postings ORDER BY seq, leg"):
            posts.setdefault(seq, []).append([book, asset, amount])
        checked = 0
        for seq, acct, kind, payload, psha, key, csha, sums, ssha in self.db.execute(
                "SELECT seq, account_id, kind, payload, payload_sha256, effect_key, content_sha256, seal_sums, "
                "seal_sha256 FROM events WHERE status = 'sealed' ORDER BY seq"):
            legs = posts.get(seq, [])
            total: dict[str, int] = {}
            for _, a, x in legs:
                total[a] = total.get(a, 0) + int(x)
            if not key or not csha:
                problems.append(f"event {seq}: no effect key or content hash (not written by the writer)")
            if _sha(payload) != psha:
                problems.append(f"event {seq}: payload hash mismatch")
            elif csha and content_hash(acct, kind, legs, json.loads(payload)) != csha:
                problems.append(f"event {seq}: content hash mismatch")
            if not legs or any(v != 0 for v in total.values()):
                problems.append(f"event {seq}: postings don't balance {total}")
            if sums != json.dumps({a: "0" for a in sorted(total)}):
                problems.append(f"event {seq}: seal sums aren't the postings'")
            if _sha(json.dumps(legs)) != ssha:
                problems.append(f"event {seq}: postings hash mismatch")
            checked += 1
            if len(problems) > 20:
                break
        for (book, asset), v in self.balances().items():
            kind = self.db.execute("SELECT kind FROM books WHERE book_id = ?", (book,)).fetchone()
            if kind and kind[0] in NONNEGATIVE and v < 0:
                problems.append(f"{book} holds {v} {asset}")
        for aid, tx, sha in self.db.execute("SELECT attempt_id, signed_tx, tx_sha256 FROM attempts"):
            if hashlib.sha256(tx).hexdigest() != sha:
                problems.append(f"attempt {aid}: signed transaction doesn't match its hash")
        if problems:
            raise LedgerError("refusing to run (fail closed): " + "; ".join(problems[:20]))
        return {"events": checked, "postings": sum(len(v) for v in posts.values())}

    # ---- setup
    def add_account(self, account_id: str, mode: str, genesis: str, owner: str, strategy: str = "") -> None:
        self.db.execute("INSERT INTO accounts (account_id, mode, genesis, owner, strategy) VALUES (?, ?, ?, ?, ?)",
                        (account_id, mode, genesis, owner, strategy))

    def add_asset(self, asset: str, program: str, decimals: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO assets VALUES (?, ?, ?)", (asset, program, decimals))

    def add_book(self, book_id: str, account_id: str | None, kind: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO books VALUES (?, ?, ?)", (book_id, account_id, kind))

    # ---- the one writer path for economics
    def append(self, account_id: str, kind: str, legs: list[tuple], payload: dict, key: str | None = None,
               effects: tuple = (), corrects: int | None = None, observed_at: float | None = None,
               causation: str = "", correlation: str = "") -> int:
        """legs: [(book_id, asset, int amount[, onchain: bool])]; effects: [(effect, locator)]. `key`: the caller's
        stable key for this effect (default: its causation id; an adoption or residual is once per account). Returns
        the sealed event's seq - the original's, when this is a replay of it."""
        key = key or causation or (kind if kind in ONCE_PER_ACCOUNT else "")
        if not key:
            raise LedgerError("an economic event needs a stable key: its retries must find it")
        legs = [(b, a, x, (rest[0] if rest else False)) for b, a, x, *rest in legs]
        body = json.dumps(payload, sort_keys=True)
        texts = [(b, a, canonical(x)) for b, a, x, _ in legs]
        chash = content_hash(account_id, kind, texts, json.loads(body))
        began = committed = False
        try:
            self.db.execute("BEGIN IMMEDIATE")
            began = True
            row = self.db.execute("SELECT seq, content_sha256 FROM events WHERE account_id = ? AND kind = ? AND "
                                  "effect_key = ?", (account_id, kind, key)).fetchone()
            if row is not None:
                if row[1] != chash:
                    raise LedgerError(f"key {key!r} is event {row[0]}, with other content: a conflict, not a retry")
                self.db.execute("COMMIT")                # a replay: the original, nothing written
                committed = True
                return row[0]
            now = time.time()
            eid = _sha(f"{account_id}|{kind}|{key}")[:26]
            cur = self.db.execute(
                "INSERT INTO events (event_id, account_id, kind, status, schema_version, code_revision, causation_id, "
                "correlation_id, corrects, observed_at, recorded_at, payload, payload_sha256, effect_key, "
                "content_sha256) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, account_id, kind, SCHEMA_VERSION, self.code, causation, correlation, corrects,
                 observed_at or now, now, body, _sha(body), key, chash))
            seq = cur.lastrowid
            self._at("open")
            sums: dict[str, int] = {}
            delta: dict[tuple, int] = {}
            for i, ((book, asset, amount, onchain), (_, _, text)) in enumerate(zip(legs, texts)):
                if abs(amount) > (ON_CHAIN_MAX if onchain else BOOK_MAX):
                    raise LedgerError(f"leg {i}: {amount} out of range")
                self.db.execute("INSERT INTO postings (seq, leg, book_id, asset, amount) VALUES (?, ?, ?, ?, ?)",
                                (seq, i, book, asset, text))
                sums[asset] = sums.get(asset, 0) + amount        # exact: Python integers
                delta[(book, asset)] = delta.get((book, asset), 0) + amount
            self._at("postings")
            for effect, locator in effects:
                try:
                    self.db.execute("INSERT INTO effects (account_id, effect, locator, seq) VALUES (?, ?, ?, ?)",
                                    (account_id, effect, locator, seq))
                except sqlite3.IntegrityError:
                    raise LedgerError(f"{effect} {locator} is already booked") from None
            if not legs or any(v != 0 for v in sums.values()):
                raise LedgerError(f"event doesn't balance: {sums}")
            for (book, asset), d in delta.items():       # holdings never go below zero (an eleventh review)
                bk = self.db.execute("SELECT kind FROM books WHERE book_id = ?", (book,)).fetchone()
                if d < 0 and bk and bk[0] in NONNEGATIVE:
                    after = self._balance(book, asset) + d
                    if after < 0:
                        raise LedgerError(f"{book} would hold {after} {asset}: holdings can't go negative")
            self.db.execute("UPDATE events SET status = 'sealed', seal_sums = ?, seal_sha256 = ? WHERE seq = ?",
                            (json.dumps({a: "0" for a in sorted(sums)}), _sha(json.dumps([list(t) for t in texts])),
                             seq))
            self._at("sealed")
            self.db.execute("COMMIT")
            committed = True
            self._at("committed")                        # (a crash here: committed, never acknowledged)
            return seq
        except BaseException:
            if began and not committed:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:                    # (the original failure is the one that matters)
                    pass
            raise

    def _balance(self, book: str, asset: str) -> int:
        return sum(int(x) for (x,) in self.db.execute(
            "SELECT p.amount FROM postings p JOIN events e ON e.seq = p.seq WHERE e.status = 'sealed' AND "
            "p.book_id = ? AND p.asset = ?", (book, asset)))

    def reverse(self, seq: int, payload: dict) -> int:
        """A correction: the original stays; a new event posts its exact negation, linked by `corrects` (once)."""
        acct, = self.db.execute("SELECT account_id FROM events WHERE seq = ?", (seq,)).fetchone()
        legs = [(b, a, -int(x), False) for b, a, x in self.db.execute(
            "SELECT book_id, asset, amount FROM postings WHERE seq = ? ORDER BY leg", (seq,))]
        return self.append(acct, "fork_correction", legs, payload, key=f"corrects:{seq}", corrects=seq)

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
                observed_at: float | None = None, block_height: int | None = None) -> int:
        """Append an observation; its obs_id (an identical one already kept: that one's). Identical observations dedupe
        by digest; a contradictory one (same source and commitment, other slot/error/content) is kept beside the
        first, for ranking - never refused, never edited (the table is append-only)."""
        digest = _sha(json.dumps([signature, event_index, commitment, source, slot, err, content_sha256, block_height]))
        cur = self.db.execute("INSERT OR IGNORE INTO observations (signature, event_index, commitment, source, slot, err, "
                              "content_sha256, block_height, observed_at, digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (signature, event_index, commitment, source, slot, err, content_sha256, block_height,
                               observed_at or time.time(), digest))
        if cur.rowcount:
            return cur.lastrowid
        return self.db.execute("SELECT obs_id FROM observations WHERE digest = ?", (digest,)).fetchone()[0]

    # ---- orders and attempts
    def add_order(self, order_id: str, account_id: str, side: str, asset: str, target: int) -> None:
        self.db.execute("INSERT INTO orders (order_id, account_id, side, asset, target, state, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'intent', ?)", (order_id, account_id, side, asset, canonical(target),
                                                                time.time()))

    def sign(self, attempt_id: str, order_id: str, signature: str, blockhash: str, last_valid_height: int,
             signed_tx: bytes) -> None:
        """Record a signed transaction BEFORE it's sent: its exact bytes, signature and expiry, in one row."""
        self.db.execute("INSERT INTO attempts (attempt_id, order_id, signature, blockhash, last_valid_height, signed_tx, "
                        "tx_sha256, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'signed', ?)",
                        (attempt_id, order_id, signature, blockhash, last_valid_height, bytes(signed_tx),
                         hashlib.sha256(bytes(signed_tx)).hexdigest(), time.time()))

    def settle(self, attempt_id: str, state: str, obs_id: int | None = None) -> None:
        self.db.execute("UPDATE attempts SET state = ?, resolved_by = ? WHERE attempt_id = ?", (state, obs_id, attempt_id))
