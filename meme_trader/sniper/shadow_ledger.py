"""The paper SHADOW ledger: the account journal (data/account-<mode>.jsonl, the authority) mirrored into the
transactional ledger prototype (research/ledger), to prove it on real events before it could ever be trusted.
Default OFF (`ledger.shadow` in params); NON-AUTHORITATIVE (a thirteenth and fourteenth review).

What it is allowed to do: read the authority's journal, write its own database (data/shadow-ledger-<mode>.db),
report status. What it can't: sign, send, retry, release a reservation, decide an order settled, or change anything
the bot trades on - it has no handle to any of it; a divergence or an error is a status line, never an action.

How it mirrors:
- each complete journal line is one shadow event, keyed by (the authority's account id, its line number) - stable
  across restarts because the journal is append-only; a replay of a mirrored line returns the original (no-op), and
  different content under its key is a conflict (reported);
- the WATERMARK is the highest line mirrored, read back from the shadow's own events: a crash anywhere - before the
  mirror ran, after its commit and before anything was acknowledged - is caught up by re-reading the journal;
- a partial last line (the authority mid-write) waits for the next pass;
- after each line, the shadow's cash is compared with that line's `cash_after` - the same watermark on both sides;
- cash and rent are mirrored (the journal carries no token amounts: inventory needs the trade log - not yet).
SQLite: WAL, synchronous=FULL (durable against a process crash; a power loss is only as safe as the disk's flush).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from pathlib import Path

from ..config import ROOT

TOLERANCE_LAMPORTS = 10              # the authority's float cash vs exact sums of its 9-decimal events
MAX_LINES_PER_PASS = 5000            # a pass is bounded: a long backlog catches up over several


def _reference():
    spec = importlib.util.spec_from_file_location("ledger_reference_shadow", ROOT / "research/ledger/reference.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def lamports(sol) -> int:
    return int(round(float(sol or 0.0) * 1_000_000_000))


class ShadowLedger:
    def __init__(self, journal: Path, db: Path):
        self.journal, self.db_path = Path(journal), Path(db)
        self.ref = _reference()
        self.lg = None
        self.status: dict = {"enabled": True, "journal": self.journal.name, "db": self.db_path.name, "lines": 0,
                             "mirrored": 0, "lag_lines": 0, "divergences": 0, "last_divergence": None,
                             "recent_divergences": [], "errors": 0,
                             "last_error": "", "skipped_non_cash": 0, "checked_at": None, "accounts": {}}

    # ---- the shadow's own store
    def _open(self):
        if self.lg is None:
            self.lg = self.ref.Ledger(self.db_path, code_revision="shadow")    # the file gate: quarantine, never touch
            self.lg.db.execute("PRAGMA journal_mode=WAL")
            self.lg.db.execute("PRAGMA synchronous=FULL")
            self.lg.add_asset("SOL", "native", 9)
        return self.lg

    def _books(self, acct: str) -> None:
        lg = self.lg
        if not lg.db.execute("SELECT 1 FROM accounts WHERE account_id = ?", (acct,)).fetchone():
            lg.add_account(acct, "paper", "mainnet", "paper")
        for b, kind in (("cash", "cash"), ("rent", "rent"), ("unresolved", "unresolved")):
            lg.add_book(f"{acct}:{b}", acct, kind)
        for b in ("opening", "owner", "market", "network", "reconciliation"):
            lg.add_book(f"external:{b}", None, "external")

    def watermark(self) -> int:
        """The highest journal line mirrored (1-based), from the shadow's own events: 0 if none."""
        lg = self._open()
        top = 0
        for (key,) in lg.db.execute("SELECT effect_key FROM events"):
            if key.startswith("line:"):
                top = max(top, int(key.split(":")[1]))
        return top

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
        if k == "wallet_sync":
            return "wallet_sync", [(cash, "SOL", delta), ("external:reconciliation", "SOL", -delta)], ()
        if k == "close" or delta == 0:
            return None
        return "unknown_kind", [(f"{acct}:unresolved", "SOL", delta), ("external:reconciliation", "SOL", -delta)], ()

    # ---- a pass
    def sync(self, now: float | None = None) -> dict:
        """Mirror what the authority has committed since the watermark (bounded), comparing cash line by line. Never
        raises: every failure is counted and kept in `status` - the bot's trading never waits on or hears from it."""
        try:
            self._sync()
        except Exception as ex:                          # noqa: BLE001 - a shadow problem is a status, never an action
            from ..redact import describe
            self.status["errors"] += 1
            self.status["last_error"] = describe(ex)
            if isinstance(ex, sqlite3.Error) or "quarantined" in str(ex):
                self.lg = None                           # reopen next pass (a lock clears; a quarantine stays)
            else:
                try:                                     # what did get committed before the failure
                    self.status["mirrored"] = self.watermark()
                    self.status["lag_lines"] = self.status["lines"] - self.status["mirrored"]
                except Exception:                        # noqa: BLE001
                    pass
        self.status["checked_at"] = now if now is not None else time.time()
        return self.status

    def _sync(self) -> None:
        lg = self._open()
        start = self.watermark()
        try:
            data = self.journal.read_bytes()
        except FileNotFoundError:
            return
        complete = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
        lines = complete.splitlines()
        self.status["lines"] = len(lines)
        cash = self._cash_by_account()                   # running totals: no full re-sum per line
        for i in range(start, min(len(lines), start + MAX_LINES_PER_PASS)):
            line = i + 1
            try:
                r = json.loads(lines[i])
            except ValueError:
                self._diverge(line, "unreadable line in the authority journal")
                continue
            acct = f"S-{r.get('account', '?')}"
            self._books(acct)
            spec = self._legs(r, acct, line)
            if spec is None:                             # no cash: a marker event keeps the watermark moving
                self.status["skipped_non_cash"] += 1
                lg.append(acct, "marker", [(f"{acct}:unresolved", "SOL", 0)], {"line": line, "kind": r.get("kind")},
                          key=f"line:{line}")
            else:
                kind, legs, effects = spec
                try:
                    lg.append(acct, kind, legs, {"line": line, "row": r}, key=f"line:{line}", effects=effects)
                    cash[acct] = cash.get(acct, 0) + sum(x for b, _, x in legs if b == f"{acct}:cash")
                except self.ref.LedgerError as ex:       # refused (a conflict, a negative holding, a double fill)
                    self._diverge(line, f"refused: {ex}")
                    lg.append(acct, "marker", [(f"{acct}:unresolved", "SOL", 0)],
                              {"line": line, "refused": str(ex)[:200]}, key=f"line:{line}")
            self._compare(cash.get(acct, 0), r, line)
        self.status["mirrored"] = self.watermark()
        self.status["lag_lines"] = len(lines) - self.status["mirrored"]
        self.status["accounts"] = cash

    def _cash_by_account(self) -> dict:
        out = {}
        for (book, asset), v in self.lg.balances().items():
            if book.endswith(":cash"):
                out[book.split(":")[0]] = v
        return out

    def _compare(self, have: int, r: dict, line: int) -> None:
        """The same watermark on both sides: the shadow's cash after this line against the authority's."""
        if "cash_after" not in r:
            return
        want = lamports(r["cash_after"])
        if abs(have - want) > TOLERANCE_LAMPORTS:
            self._diverge(line, f"cash {have} lamports in the shadow, {want} in the authority")

    def _diverge(self, line: int, what: str) -> None:
        d = {"line": line, "what": what[:300]}
        self.status["divergences"] += 1
        self.status["last_divergence"] = d
        self.status["recent_divergences"] = (self.status["recent_divergences"] + [d])[-20:]
