"""The paper SHADOW ledger: the account journal (data/account-<mode>.jsonl, the authority) mirrored into the
transactional ledger prototype (research/ledger), to prove it on real events before it could ever be trusted.
Default OFF (`ledger.shadow` in params); NON-AUTHORITATIVE (a thirteenth and fourteenth review).

What it is allowed to do: read the authority's journal, write its own database (data/shadow-ledger-<mode>.db),
report status. What it can't: sign, send, retry, release a reservation, decide an order settled, or change anything
the bot trades on - it has no handle to any of it; a divergence or an error is a status line, never an action.

Coverage is DURABLE and derived from storage, never from an instance's memory (a fifteenth review: a highest-line
watermark had skipped holes, and a restart had erased the only record of a refusal):
- every complete journal line becomes exactly one shadow event, keyed `line:<n>`, in ONE transaction with what the
  shadow learned about it: its byte offset and length, the sha256 of its bytes, a chain hash over every line before
  it, the shadow's cash after it, and its STATE - validated (mirrored, cash equal to the authority's `cash_after`),
  divergent (mirrored, cash not equal), refused (the ledger refused it: a marker keeps the evidence and the cause),
  malformed or oversize (not a journal row: its raw bytes kept, or the first RAW_KEEP of an oversize one), flagged
  (mirrored, but the authority itself recorded a problem - a restore gap). Only `validated` is coverage;
- the store says `committed` (lines with an event), `validated_through` (the longest fully validated prefix) and
  `unresolved` (every other state, until a recorded repair names it) - re-derived on every open, so a restart can't
  make a hole, a refusal or a divergence disappear;
- the SOURCE is checked, not trusted: its identity (device, inode) is a recorded epoch; a smaller file than was
  consumed is truncation; the last consumed line is re-hashed every pass, and the whole consumed prefix is re-hashed
  (streamed) at an instance's first pass and every FULL_VERIFY_S - so a same-length rewrite anywhere is found within
  that interval, and at the tail at once. A replaced file whose consumed prefix is byte-identical is a new epoch; any
  other change is a persisted SOURCE FAULT: mirroring stops until a recorded `rebuild` (the old store is archived,
  never deleted). A missing journal is `missing` (or `not_ready` before anything was mirrored) - never an empty pass;
- passes are bounded in memory AND I/O: a pass reads from the consumed offset, at most MAX_BYTES_PER_PASS and
  MAX_LINES_PER_PASS; a line longer than MAX_LINE_BYTES is an `oversize` hole, hashed in chunks; a partial last line
  (the authority mid-write) waits, and one that stays partial for PARTIAL_STALE_S is reported as torn;
- the store's own content is re-derived before it's trusted: the ledger's startup_check (sums, hashes, holdings) and
  the coverage records' contiguity and chain, at every open;
- with an AUTHORITY CUT (the engine's in-memory cash and the journal's size, taken together on its event loop), the
  shadow's cash at that exact line is compared with the authority's memory: the journal can be internally consistent
  while the authority moved without it (an append that failed). A difference is persisted, unresolved until repaired.
Repairs are records, not edits: `resolve(<line> | "authority:<line>", note)` and `rebuild(note)`.
SQLite: WAL, synchronous=FULL (durable against a process crash; a power loss is only as safe as the disk's flush).
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

from ..config import ROOT

TOLERANCE_LAMPORTS = 10              # the authority's float cash vs exact sums of its 9-decimal events
MAX_LINES_PER_PASS = 5000            # a pass is bounded: a long backlog catches up over several
MAX_BYTES_PER_PASS = 8 << 20         # ... in bytes read, too
MAX_LINE_BYTES = 64 << 10            # a longer line isn't a journal row: an `oversize` hole (hashed in chunks)
RAW_KEEP = 4096                      # an oversize line's first bytes kept as evidence (a malformed one: all of it)
FULL_VERIFY_S = 600                  # the whole consumed prefix re-hashed this often (and at an instance's first pass)
LAG_SCAN_BYTES = 32 << 20            # the backlog's lines are counted up to this far; past it `lag_exact` is false
PARTIAL_STALE_S = 120                # a partial last line older than this is reported as torn
CHUNK = 1 << 20
SOURCE = "S-source"                  # the shadow's own account for source records: epochs, holes, faults, repairs
GOOD = "validated"


def _reference():
    spec = importlib.util.spec_from_file_location("ledger_reference_shadow", ROOT / "research/ledger/reference.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def lamports(sol) -> int:
    return int(round(float(sol or 0.0) * 1_000_000_000))


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _chain(prev: str, sha: str) -> str:
    return hashlib.sha256((prev + sha).encode()).hexdigest()


class Coverage:
    """What the store says the shadow has covered, re-derived from its events (never kept only in memory)."""

    def __init__(self):
        self.lines: list[dict] = []      # line n at index n-1: off, len, sha, chain, state, cause, acct, cash
        self.repaired: dict[str, dict] = {}
        self.faults: list[dict] = []
        self.epochs: list[dict] = []
        self.authority: dict[int, dict] = {}
        self.rebuilt: dict | None = None

    @property
    def n(self) -> int:
        return len(self.lines)

    @property
    def offset(self) -> int:
        last = self.lines[-1] if self.lines else None
        return last["off"] + last["len"] if last else 0

    @property
    def chain(self) -> str:
        return self.lines[-1]["chain"] if self.lines else ""

    def unresolved(self) -> list[dict]:
        out = [{"line": x["n"], "state": x["state"], "what": x["cause"] or x["state"]}
               for x in self.lines if x["state"] != GOOD and str(x["n"]) not in self.repaired]
        out += [{"line": n, "state": "authority", "what": a["what"]} for n, a in sorted(self.authority.items())
                if f"authority:{n}" not in self.repaired]
        return out

    def validated_through(self) -> int:
        for x in self.lines:
            if x["state"] != GOOD and str(x["n"]) not in self.repaired:
                return x["n"] - 1
        return self.n


class ShadowLedger:
    def __init__(self, journal: Path, db: Path):
        self.journal, self.db_path = Path(journal), Path(db)
        self.ref = _reference()
        self.lg = None
        self.cov: Coverage | None = None
        self._db_ident = None
        self._data_version = None
        self._verified_at = None         # None: this instance hasn't verified the prefix yet (its first pass will)
        self._partial = None             # (offset, first seen) of a partial last line
        self.errors, self.last_error = 0, ""
        self.status: dict = self._blank()

    def _blank(self) -> dict:
        return {"enabled": True, "journal": self.journal.name, "db": self.db_path.name, "source": "not_checked",
                "lines": 0, "mirrored": 0, "committed": 0, "validated": 0, "validated_through": 0, "unresolved": 0,
                "unresolved_by_state": {}, "recent_unresolved": [], "recent_divergences": [], "last_divergence": None,
                "repaired": 0, "faults": [], "epochs": 0, "offset": 0, "lag_bytes": 0, "lag_lines": 0,
                "lag_exact": True, "partial_bytes": 0, "torn_partial": False, "authority": None, "divergences": 0,
                "incomplete": True, "healthy": False, "errors": 0, "last_error": "", "skipped_non_cash": 0,
                "verified_at": None, "prefix_chain": "", "checked_at": None, "accounts": {}}

    # ---- the shadow's own store
    def _open(self):
        ident = None
        try:
            st = self.db_path.stat()
            ident = (st.st_dev, st.st_ino)
        except FileNotFoundError:
            pass
        if self.lg is not None and ident != self._db_ident:      # archived or replaced under us: reopen
            self._close()
        if self.lg is None:
            lg = self.ref.Ledger(self.db_path, code_revision="shadow")    # the file gate: quarantine, never touch
            self.lg, self.cov = lg, None
            try:
                lg.db.execute("PRAGMA journal_mode=WAL")
                lg.db.execute("PRAGMA synchronous=FULL")
                lg.startup_check()                       # content, not just structure, before anything is trusted
                lg.add_asset("SOL", "native", 9)
                self._books(SOURCE)
            except BaseException:
                self.lg = None
                lg.db.close()
                raise
            st = self.db_path.stat()
            self._db_ident = (st.st_dev, st.st_ino)
        dv = self.lg.db.execute("PRAGMA data_version").fetchone()[0]
        if self.cov is None or dv != self._data_version:         # another connection wrote (a repair): re-derive
            self.cov = self._derive()
            self._data_version = dv
        return self.lg

    def _close(self) -> None:
        if self.lg is not None:
            try:
                self.lg.db.close()
            except sqlite3.Error:
                pass
        self.lg, self.cov, self._db_ident = None, None, None

    def _books(self, acct: str) -> None:
        lg = self.lg
        if not lg.db.execute("SELECT 1 FROM accounts WHERE account_id = ?", (acct,)).fetchone():
            lg.add_account(acct, "paper", "mainnet", "paper")
        for b, kind in (("cash", "cash"), ("rent", "rent"), ("unresolved", "unresolved")):
            lg.add_book(f"{acct}:{b}", acct, kind)
        for b in ("opening", "owner", "market", "network", "reconciliation"):
            lg.add_book(f"external:{b}", None, "external")

    def _derive(self) -> Coverage:
        """Coverage from the events alone - contiguous lines from 1, each starting where the last ended, each chain
        hash the previous chain with its own hash - or the store isn't trusted (LedgerError)."""
        cov = Coverage()
        lines: dict[int, dict] = {}
        for acct, kind, key, payload in self.lg.db.execute(
                "SELECT account_id, kind, effect_key, payload FROM events WHERE status = 'sealed' ORDER BY seq"):
            p = json.loads(payload)
            if key.startswith("line:"):
                n = int(key.split(":")[1])
                if n in lines:
                    raise self.ref.LedgerError(f"the shadow's store has two events for line {n}")
                s = p.get("src") or {}
                lines[n] = {"n": n, "off": s.get("off"), "len": s.get("len"), "sha": s.get("sha"),
                            "chain": s.get("chain"), "state": p.get("state"), "cause": p.get("cause"),
                            "acct": acct if acct != SOURCE else None, "cash": p.get("shadow_cash"),
                            "kind": (p.get("row") or {}).get("kind") if isinstance(p.get("row"), dict) else None}
            elif kind == "repair":
                cov.repaired[str(p.get("item"))] = p
            elif kind == "source_fault":
                cov.faults.append(p)
            elif kind == "source_epoch":
                cov.epochs.append(p)
            elif kind == "authority_check":
                cov.authority[int(p["line"])] = p
            elif kind == "rebuild":
                cov.rebuilt = p
        prev_end, prev_chain = 0, ""
        for i in range(1, len(lines) + 1):
            x = lines.get(i)
            if x is None:
                raise self.ref.LedgerError(f"the shadow's coverage skips line {i}")
            if x["off"] != prev_end or not isinstance(x["len"], int) or x["len"] < 1 or \
                    x["chain"] != _chain(prev_chain, x["sha"] or ""):
                raise self.ref.LedgerError(f"the shadow's coverage record for line {i} doesn't follow line {i - 1}'s")
            prev_end, prev_chain = x["off"] + x["len"], x["chain"]
            cov.lines.append(x)
        return cov

    def _record(self, kind: str, key: str, payload: dict) -> None:
        """A source-level record (zero-value: it moves nothing)."""
        self.lg.append(SOURCE, kind, [(f"{SOURCE}:unresolved", "SOL", 0)], payload, key=key)

    def _fault(self, what: str) -> None:
        self._record("source_fault", f"fault:{len(self.cov.faults) + 1}",
                     {"what": what[:300], "at": round(time.time(), 3), "consumed_lines": self.cov.n,
                      "consumed_bytes": self.cov.offset, "prefix_chain": self.cov.chain})
        self.cov = None

    # ---- reading the source, bounded
    def _span_ok(self, f, x: dict) -> str:
        """'' when line x's bytes in the file are the ones mirrored (hashed in chunks), else what differs."""
        f.seek(x["off"])
        h, left = hashlib.sha256(), x["len"] - 1
        while left > 0:
            b = f.read(min(CHUNK, left))
            if not b:
                return f"line {x['n']}: the file now ends inside it"
            h.update(b)
            left -= len(b)
        if f.read(1) != b"\n":
            return f"line {x['n']}: it no longer ends where it did"
        if h.hexdigest() != x["sha"]:
            return f"line {x['n']}: its bytes changed after they were mirrored"
        return ""

    def _verify(self) -> str:
        """The whole consumed prefix re-hashed against the store, streamed: '' or the first difference."""
        with self.journal.open("rb") as f:
            for x in self.cov.lines:
                bad = self._span_ok(f, x)
                if bad:
                    return bad
        self._verified_at = time.time()
        return ""

    def _lines_from(self, f, off: int, size: int):
        """Complete lines from `off`, at most the pass's budgets: yields (off, length, sha, raw or None (oversize),
        head). Stops at a partial last line, which waits."""
        f.seek(off)
        buf, start, read, n = b"", off, 0, 0
        while n < MAX_LINES_PER_PASS:
            i = buf.find(b"\n")
            if i < 0:
                if len(buf) > MAX_LINE_BYTES:            # oversize: hash it in chunks, keep its head
                    h, head, length = hashlib.sha256(buf), buf[:RAW_KEEP], len(buf)
                    while True:
                        b = f.read(CHUNK)
                        if not b:
                            return                       # still partial: waits (re-read next pass, memory bounded)
                        j = b.find(b"\n")
                        if j >= 0:
                            h.update(b[:j])
                            length += j
                            yield start, length + 1, h.hexdigest(), None, head
                            n += 1
                            start += length + 1
                            buf = b[j + 1:]
                            break
                        h.update(b)
                        length += len(b)
                    continue
                if read >= MAX_BYTES_PER_PASS:
                    return
                b = f.read(min(CHUNK, size - start - len(buf)) if size > start + len(buf) else CHUNK)
                if not b:
                    return
                read += len(b)
                buf += b
                continue
            raw = buf[:i]
            buf = buf[i + 1:]
            if len(raw) > MAX_LINE_BYTES:
                yield start, i + 1, _sha(raw), None, raw[:RAW_KEEP]
            else:
                yield start, i + 1, _sha(raw), raw, None
            start += i + 1
            n += 1

    # ---- one journal row -> postings
    def _legs(self, r: dict, acct: str, line: int):
        """(kind, legs, effects) for a journal row, or None when it moves no cash (a close links a trade row)."""
        k, delta = r.get("kind"), lamports(r.get("sol"))
        cash, rent = f"{acct}:cash", f"{acct}:rent"
        if k in ("open", "adopted", "reset"):
            amt = lamports(r.get("cash_after"))
            if amt == 0:
                return None
            return "adoption" if k == "adopted" else "opening", [("external:opening", "SOL", -amt), (cash, "SOL", amt)], ()
        if k == "deposit":
            return "deposit", [("external:owner", "SOL", -delta), (cash, "SOL", delta)], ()
        if k == "buy":                                   # delta = -(sol + rent): cash out to the market and to rent
            r_l = lamports(r.get("rent"))
            legs = [(cash, "SOL", delta), ("external:market", "SOL", -delta - r_l)] + ([(rent, "SOL", r_l)] if r_l else [])
            return "buy", legs, ((("fill", f"{r['ref']}:buy"),) if r.get("ref") else ())
        if k == "sell":                                  # delta = sol + rent reclaimed (sol may be negative)
            r_l = lamports(r.get("rent_reclaimed"))
            legs = [(cash, "SOL", delta), ("external:market", "SOL", -(delta - r_l))] + ([(rent, "SOL", -r_l)] if r_l else [])
            return "sell", legs, ((("fill", f"{r['ref']}:sell"),) if r.get("ref") else ())
        if k == "failed_fee":
            return "failed_fee", [(cash, "SOL", delta), ("external:network", "SOL", -delta)], ()
        if k in ("wallet_sync", "restore_gap"):          # (a restore gap is also flagged: see _line)
            return k, [(cash, "SOL", delta), ("external:reconciliation", "SOL", -delta)], ()
        if k == "close" or delta == 0:
            return None
        return "unknown_kind", [(f"{acct}:unresolved", "SOL", delta), ("external:reconciliation", "SOL", -delta)], ()

    def _line(self, n: int, off: int, length: int, sha: str, raw: bytes | None, head: bytes | None,
              cash: dict) -> None:
        """Line n, in one transaction with everything the shadow learned about it."""
        lg = self.lg
        src = {"line": n, "off": off, "len": length, "sha": sha, "chain": _chain(self.cov.chain, sha)}
        r = None
        if raw is not None:
            try:
                r = json.loads(raw)
            except ValueError:
                r = None
            if not (isinstance(r, dict) and r.get("account") and r.get("kind")):
                r = None
        if r is None:                                    # a hole: its evidence kept, never a mirrored line
            state = "oversize" if raw is None else "malformed"
            body = head if raw is None else raw
            lg.append(SOURCE, "hole", [(f"{SOURCE}:unresolved", "SOL", 0)],
                      {"src": src, "state": state, "cause": f"{state} line in the authority journal",
                       "raw_b64": base64.b64encode(body).decode(), "raw_complete": raw is not None}, key=f"line:{n}")
            self.cov.lines.append({"n": n, **{k: src[k] for k in ("off", "len", "sha", "chain")}, "state": state,
                                   "cause": f"{state} line in the authority journal", "acct": None, "cash": None,
                                   "kind": None})
            return
        acct = f"S-{r['account']}"
        self._books(acct)
        before = cash.get(acct, 0)
        spec = self._legs(r, acct, n)
        kind, legs, effects = spec if spec is not None else ("marker", [(f"{acct}:unresolved", "SOL", 0)], ())
        after = before + sum(x for b, _, x in legs if b == f"{acct}:cash")
        state, cause = GOOD, None
        if "cash_after" in r and abs(after - lamports(r["cash_after"])) > TOLERANCE_LAMPORTS:
            state, cause = "divergent", f"cash {after} lamports in the shadow, {lamports(r['cash_after'])} in the authority"
        if r.get("kind") == "restore_gap" and state == GOOD:
            state, cause = "flagged", "the authority's restored state differed from its own journal (a restore gap)"
        payload = {"src": src, "state": state, "cause": cause, "row": r, "shadow_cash": after}
        try:
            lg.append(acct, kind, legs, payload, key=f"line:{n}", effects=effects)
        except self.ref.LedgerError as ex:               # refused (a negative holding, a double fill): evidence kept
            after = before
            state = "refused"
            cause = f"refused: {str(ex)[:200]}"
            if "cash_after" in r and abs(after - lamports(r["cash_after"])) > TOLERANCE_LAMPORTS:
                cause += f"; cash {after} lamports in the shadow, {lamports(r['cash_after'])} in the authority"
            lg.append(acct, "marker", [(f"{acct}:unresolved", "SOL", 0)],
                      {"src": src, "state": state, "cause": cause, "row": r, "shadow_cash": after}, key=f"line:{n}")
        cash[acct] = after
        self.cov.lines.append({"n": n, **{k: src[k] for k in ("off", "len", "sha", "chain")}, "state": state,
                               "cause": cause, "acct": acct, "cash": after, "kind": r.get("kind")})

    # ---- a pass
    def sync(self, now: float | None = None, cut: dict | None = None) -> dict:
        """Mirror what the authority has committed since the consumed offset (bounded), checking the source first.
        Never raises: every failure is counted and kept in `status` - the bot's trading never waits on or hears from
        it. `cut`: the engine's {account, cash, size, journal_errors} taken together on its loop (optional)."""
        now = time.time() if now is None else now
        failed = False
        try:
            self._sync(now, cut)
        except Exception as ex:                          # noqa: BLE001 - a shadow problem is a status, never an action
            from ..redact import describe
            failed = True
            self.errors += 1
            self.last_error = describe(ex)
            if isinstance(ex, sqlite3.Error) or isinstance(ex, self.ref.LedgerError):
                self._close()                            # reopen next pass (a lock clears; a quarantine stays)
            else:
                self.cov = None                          # re-derive from storage: memory isn't trusted after a failure
            if "quarantined" in self.last_error or "refusing to run" in self.last_error or \
                    "shadow's coverage" in self.last_error or "shadow's store" in self.last_error:
                self.status["source"] = "store_invalid"
        self._summarize(now, failed)
        return self.status

    def _sync(self, now: float, cut: dict | None) -> None:
        self._open()
        cov = self.cov
        st = self.status
        if cov.faults:
            st["source"] = "fault"                       # mirroring stops until a recorded rebuild
            return
        try:
            fs = os.stat(self.journal)
        except FileNotFoundError:
            st["source"] = "missing" if cov.n else "not_ready"
            st["lag_bytes"] = st["lag_lines"] = st["partial_bytes"] = 0
            return
        ident = {"dev": fs.st_dev, "ino": fs.st_ino}
        last = cov.epochs[-1] if cov.epochs else None
        if last is None or (last.get("dev"), last.get("ino")) != (fs.st_dev, fs.st_ino):
            if last is not None or cov.n:
                bad = self._verify() if fs.st_size >= cov.offset else "the file is shorter than what was consumed"
                if bad:
                    self._fault(f"source replaced: {bad}")
                    st["source"] = "fault"
                    return
            self._record("source_epoch", f"epoch:{len(cov.epochs) + 1}",
                         {**ident, "at": round(now, 3), "from_line": cov.n + 1,
                          "why": "first seen" if last is None and not cov.n else "replaced; consumed prefix identical"})
            cov = self.cov = self._derive()
        if fs.st_size < cov.offset:
            self._fault(f"source truncated: {fs.st_size} bytes, {cov.offset} were consumed")
            st["source"] = "fault"
            return
        with self.journal.open("rb") as f:
            bad = self._span_ok(f, cov.lines[-1]) if cov.lines else ""
            if not bad and (self._verified_at is None or now - self._verified_at >= FULL_VERIFY_S):
                bad = self._verify()
                self._verified_at = now if not bad else self._verified_at
            if bad:
                self._fault(f"source changed: {bad}")
                st["source"] = "fault"
                return
            st["source"] = "ok"
            cash = self._cash_by_account()               # running totals: no full re-sum per line
            for off, length, sha, raw, head in self._lines_from(f, cov.offset, fs.st_size):
                self._line(cov.n + 1, off, length, sha, raw, head, cash)
            self._lag(f, fs.st_size, now)
        if cut:
            self._authority(cut)

    def _lag(self, f, size: int, now: float) -> None:
        """The backlog after this pass: bytes, complete lines (counted up to LAG_SCAN_BYTES), and a partial last line
        - which is normal mid-write, and reported as torn once it has stayed partial for PARTIAL_STALE_S."""
        st, off = self.status, self.cov.offset
        st["lag_bytes"] = size - off
        f.seek(off)
        lines, pos, after_nl = 0, off, off
        while pos < size and pos - off < LAG_SCAN_BYTES:
            b = f.read(min(CHUNK, size - pos, LAG_SCAN_BYTES - (pos - off)))
            if not b:
                break
            k = b.rfind(b"\n")
            if k >= 0:
                lines += b.count(b"\n")
                after_nl = pos + k + 1
            pos += len(b)
        st["lag_lines"], st["lag_exact"] = lines, pos >= size
        partial = size - after_nl if st["lag_exact"] else 0
        st["partial_bytes"] = partial
        if partial and lines == 0:                       # the next thing to read is a line still being written
            if not self._partial or self._partial[0] != off:
                self._partial = (off, now)
            st["torn_partial"] = now - self._partial[1] >= PARTIAL_STALE_S
        else:
            self._partial, st["torn_partial"] = None, False

    def _authority(self, cut: dict) -> None:
        """The shadow's cash at the cut's exact line against the authority's memory at that moment."""
        size, acct = cut.get("size"), f"S-{cut.get('account')}"
        out = {"size": size, "account": cut.get("account"), "journal_errors": cut.get("journal_errors", 0)}
        if cut.get("pending_rows"):                      # the authority's journal is behind its memory (a full disk)
            out["result"] = f"journal behind memory: {cut['pending_rows']} row(s) not yet written"
            self.status["authority"] = out
            return
        line = next((x for x in reversed(self.cov.lines) if x["off"] + x["len"] == size), None) if size else None
        if line is None:
            out["result"] = "cut not mirrored yet" if size and size > self.cov.offset else "no line ends at the cut"
            self.status["authority"] = out
            return
        mine = next((x for x in reversed(self.cov.lines[:line["n"]]) if x["acct"] == acct and x["cash"] is not None),
                    None)
        if mine is None:
            out["result"] = "no line for this account yet"
            self.status["authority"] = out
            return
        want = lamports(cut.get("cash"))
        out.update(line=line["n"], shadow=mine["cash"], authority=want)
        if abs(mine["cash"] - want) <= TOLERANCE_LAMPORTS:
            out["result"] = "equal"
        else:
            out["result"] = "divergent"
            if line["n"] not in self.cov.authority:
                what = (f"at line {line['n']} the authority holds {want} lamports, the shadow {mine['cash']} "
                        f"({out['journal_errors']} journal append error(s) in the authority)")
                try:
                    self._record("authority_check", f"authority:{line['n']}", {**out, "what": what})
                except self.ref.LedgerError:
                    pass                                 # (already recorded at this line)
                self.cov = None
                self._open()
        self.status["authority"] = out

    def _cash_by_account(self) -> dict:
        out = {}
        for (book, asset), v in self.lg.balances().items():
            if book.endswith(":cash"):
                out[book.split(":")[0]] = v
        return out

    def _summarize(self, now: float, failed: bool) -> None:
        st = self.status
        st["errors"], st["last_error"], st["checked_at"] = self.errors, self.last_error, now
        cov = self.cov
        if cov is None and self.lg is not None:
            try:
                cov = self.cov = self._derive()
            except Exception:                            # noqa: BLE001 - reported by the next pass
                cov = None
        if cov is None:
            st["incomplete"], st["healthy"] = True, False
            return
        un = cov.unresolved()
        by: dict = {}
        for u in un:
            by[u["state"]] = by.get(u["state"], 0) + 1
        st.update(committed=cov.n, mirrored=cov.n, validated=sum(1 for x in cov.lines if x["state"] == GOOD),
                  validated_through=cov.validated_through(), unresolved=len(un), unresolved_by_state=by,
                  recent_unresolved=un[-20:], recent_divergences=un[-20:], last_divergence=un[-1] if un else None,
                  repaired=len(cov.repaired), faults=[f["what"] for f in cov.faults], epochs=len(cov.epochs),
                  offset=cov.offset, prefix_chain=cov.chain, verified_at=self._verified_at,
                  skipped_non_cash=sum(1 for x in cov.lines if x.get("kind") == "close"),
                  lines=cov.n + (st["lag_lines"] if st["source"] == "ok" else 0))
        try:
            st["accounts"] = self._cash_by_account()
        except Exception:                                # noqa: BLE001
            pass
        st["divergences"] = len(un) + len(cov.faults)
        behind = str((st["authority"] or {}).get("result", "")).startswith("journal behind")
        st["incomplete"] = bool(st["source"] != "ok" or st["lag_lines"] or st["torn_partial"] or behind or
                                st["validated_through"] < cov.n)
        st["healthy"] = not (st["incomplete"] or st["divergences"] or failed)

    # ---- recorded repairs (never edits)
    def resolve(self, item, note: str) -> str:
        """Record that an unresolved line (its number) or authority check ("authority:<line>") was reviewed: `note`
        says what was found. It stays in the store; only the status stops counting it. '' or why not."""
        if not note.strip():
            return "a repair needs a note saying what was found"
        self._open()
        key = str(item)
        if key in self.cov.repaired:
            return f"{key} is already resolved"
        if key.startswith("authority:"):
            n = int(key.split(":")[1])
            if n not in self.cov.authority:
                return f"no authority divergence recorded at line {n}"
            target = self.cov.authority[n]
        else:
            n = int(key)
            if not 1 <= n <= self.cov.n or self.cov.lines[n - 1]["state"] == GOOD:
                return f"line {n} isn't unresolved"
            target = self.cov.lines[n - 1]
        self._record("repair", f"repair:{key}", {"item": key, "note": note[:500], "at": round(time.time(), 3),
                                                 "sha": target.get("sha"), "state": target.get("state", "authority")})
        self.cov = None
        return ""

    def rebuild(self, note: str) -> str:
        """The recorded repair for a source fault (or a full re-derivation): the store is ARCHIVED (renamed, never
        deleted), and a fresh one - whose first record says why - is built from the journal as it is now. Run it
        with the bot stopped, or from the bot itself. '' or why not."""
        if not note.strip():
            return "a rebuild needs a note saying why"
        self._close()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archived = self.db_path.with_name(f"{self.db_path.name}.archived-{stamp}")
        if self.db_path.exists():
            con = sqlite3.connect(str(self.db_path))     # fold the WAL into the file before it moves
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
            self.db_path.rename(archived)
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.rename(Path(str(archived) + suffix))
        self._open()
        self._record("rebuild", "rebuild", {"note": note[:500], "archived": archived.name if archived.exists() else None,
                                            "at": round(time.time(), 3)})
        self.cov, self._verified_at = None, None
        return ""
