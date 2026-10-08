"""The live PumpSwap price watcher: fresh, executable quotes from coherent on-chain state, with a reason for every
quote it couldn't give. Measurement only - nothing is signed or sent.

Each quote is ONE `getMultipleAccounts` read at `confirmed` of the pool, its two vaults, the base mint, the AMM's
global config and the fee program's fee config, so everything priced comes from the same context slot. It's then:
- validated: every account present and owned by the right program; the pool's discriminator and layout; the pool
  still pointing at the vaults and mints first discovered (else `pool_changed`); the vaults' mints and authority (the
  pool); the vaults not frozen; a SOL quote; the mint without unsupported extensions (`pumpswap.decode_mint`); buys or
  sells not disabled; the response within MAX_RESPONSE_S; its context slot no more than MAX_SLOT_LAG behind the feed's
  newest slot at the same commitment;
- priced by `pumpswap.py` (the official SDK's integer math): fees from the fee config's schedule, effective quote
  reserves = vault + signed virtual reserves, sells checked against the real vault.
A quote is not a fill: landing, slippage after it and transaction failure aren't modeled here.

`QuoteBook` schedules quotes and keeps every attempt (data/quotes.db): an entry quote at a follow's decision + delay,
then exit quotes at each hold's due time, retried every RETRY_EVERY_S until RETRY_S after it (the registered policy),
else the exit is unmeasured with its last reason. Results: `view()` - coverage by reason, both arms, and quote-priced
P&L next to the forward test's print-priced one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import statistics
import struct
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..redact import describe, register, safe_error
from . import pumpswap as ps

AMM, FEE_PROGRAM = ps.PUMP_AMM_PROGRAM, "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
GLOBAL_CONFIG_DISC = bytes([149, 8, 156, 202, 160, 252, 176, 217])
FEE_CONFIG_DISC = bytes([143, 52, 146, 187, 219, 123, 76, 155])
COMMITMENT = "confirmed"
MAX_RESPONSE_S = 5.0                 # a quote is fresh only if the read came back this fast (proposed; to calibrate)
MAX_SLOT_LAG = 2                     # ... and its state is at most this many slots behind the feed's newest
RETRY_EVERY_S, RETRY_S = 30, 900     # an exit with no valid quote: retried this often, for this long (registered)
# refusals a retry can't change (a pool's quote asset, its mint's extensions, its layout, its accounts' owners):
# final at once, not retried every 30 s for 15 minutes
PERMANENT = {"non_sol_quote", "mint_extensions", "unsupported_layout", "wrong_owner", "vault_mismatch",
             "vault_program_mismatch"}
ENTRY_GRACE_S = 60                   # an entry quote is tried for this long after its decision, then skipped
TX_COST_SOL = 0.001005               # one transaction's priority + base fee (the paper bot's), for net P&L
# an attempt record's schema: 2 = qualification reasons, account hashes, the job clock (an eleventh review). A record
# without a version (1) predates the qualification rules: its qualification is UNKNOWN, never assumed
# 3 = the official 287-byte Pool layout (fee buckets decoded; bytes past 287 are what's undocumented), sells checked
# against real reserves (the vault less the fee buckets), the raw pool account kept (public), SDK/IDL pinned (a
# twelfth review). Version 2's "undocumented bytes" were judged by the 271-byte layout.
# 4 = version 3 plus every account's raw public bytes (base64) beside its hash, so an auditor can re-derive every
# qualification input, not only the pool's (a thirteenth review). The qualification rules are version 3's.
RECORD_VERSION = 4
EXPORT_VERSION = 2                   # the package's quote rows: job_id / attempt_n / meta / record, never merged
FRESHNESS_REF = "the feed's newest slot at confirmed"


def _pda(seeds: list[bytes], program: str) -> str:
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address(seeds, Pubkey.from_string(program))[0])


def global_config_address() -> str:
    return _pda([b"global_config"], AMM)


def fee_config_address() -> str:
    from solders.pubkey import Pubkey
    return _pda([b"fee_config", bytes(Pubkey.from_string(AMM))], FEE_PROGRAM)


class Reject(Exception):
    """A quote that can't be given: a reason code (for coverage) and a detail."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


# --------------------------------------------------------------------------- account layouts (IDL 1.20.0, SPL)
def decode_token_account(data: bytes, owner: str) -> dict:
    """An SPL token account (legacy 165 bytes; Token-2022 165 + account type + extensions)."""
    from solders.pubkey import Pubkey
    if owner not in (ps.TOKEN_PROGRAM, ps.TOKEN_2022_PROGRAM):
        raise Reject("wrong_owner", "vault not owned by a token program")
    if len(data) < 165 or (len(data) > 165 and (owner != ps.TOKEN_2022_PROGRAM or data[165] != 2)):
        raise Reject("unsupported_layout", f"token account of {len(data)} bytes")
    state = data[108]
    if state != 1:
        raise Reject("vault_frozen" if state == 2 else "vault_uninitialized")
    return {"mint": str(Pubkey.from_bytes(data[0:32])), "authority": str(Pubkey.from_bytes(data[32:64])),
            "amount": struct.unpack_from("<Q", data, 64)[0]}


def decode_global_config(data: bytes) -> dict:
    if len(data) < 949 or data[:8] != GLOBAL_CONFIG_DISC:
        raise Reject("unsupported_layout", "global config")
    lp, proto = struct.unpack_from("<QQ", data, 40)
    creator, = struct.unpack_from("<Q", data, 313)
    return {"lp": lp, "protocol": proto, "creator": creator, "disable_flags": data[56],
            "creator_fee_configurable": bool(data[940])}


def decode_fee_config(data: bytes) -> dict:
    if len(data) < 8 + 1 + 32 + 24 + 4 or data[:8] != FEE_CONFIG_DISC:
        raise Reject("unsupported_layout", "fee config")
    o = 8 + 1 + 32

    def fees(o):
        lp, proto, creator = struct.unpack_from("<QQQ", data, o)
        return {"lp": lp, "protocol": proto, "creator": creator}, o + 24

    def tiers(o):
        n, = struct.unpack_from("<I", data, o)
        o += 4
        if n > 256 or o + 40 * n > len(data):
            raise Reject("unsupported_layout", "fee tiers")
        out = []
        for _ in range(n):
            f, _ = fees(o + 16)
            out.append({"threshold": int.from_bytes(data[o:o + 16], "little"), "fees": f})
            o += 40
        return out, o
    flat, o = fees(o)
    t, o = tiers(o)
    st, o = tiers(o)
    if o + 24 > len(data):
        raise Reject("unsupported_layout", "fee config")
    exotic, o = fees(o)
    return {"flat": flat, "tiers": t, "stable_tiers": st, "exotic": exotic}


# --------------------------------------------------------------------------- one quote
def rpc_accounts(url: str, keys: list[str], timeout: float = 8.0) -> tuple[int, list]:
    """getMultipleAccounts at COMMITMENT: (context slot, [(owner, data bytes) or None])."""
    import base64

    import httpx
    r = httpx.post(url, timeout=timeout, json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                                               "params": [keys, {"encoding": "base64", "commitment": COMMITMENT}]})
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"getMultipleAccounts: {str(d['error'])[:160]}")
    res = d["result"]
    return int(res["context"]["slot"]), [(a["owner"], base64.b64decode(a["data"][0])) if a else None
                                         for a in res["value"]]


class Quoter:
    """Quotes `pool` for a buy of `amount` lamports or a sell of `amount` base atoms. `url()` gives the RPC URL (never
    recorded: only its host), `ref_slot()` the feed's newest slot at the same commitment (0 = unknown)."""

    def __init__(self, url, ref_slot=lambda: 0, fetch=rpc_accounts, clock=time.time, code: str | None = None):
        self.url, self.ref_slot, self.fetch, self.clock, self.code = url, ref_slot, fetch, clock, code
        self.static: dict[str, dict] = {}                # pool -> the accounts it pointed at when discovered
        self.global_config, self.fee_config = global_config_address(), fee_config_address()

    def _discover(self, pool: str) -> dict:
        _, (acc,) = self.fetch(self.url(), [pool])
        if acc is None:
            raise Reject("missing_account", "pool")
        owner, data = acc
        if owner != AMM:
            raise Reject("wrong_owner", "pool")
        try:
            p = ps.decode_pool(data)
        except ps.QuoteError as ex:
            raise Reject("unsupported_layout", str(ex)) from None
        st = {k: p[k] for k in ("pool_base_token_account", "pool_quote_token_account", "base_mint", "quote_mint")}
        self.static[pool] = st
        return st

    def quote(self, pool: str, side: str, amount: int) -> dict:
        from urllib.parse import urlparse
        if self.code is None:
            from .research import code_revision
            self.code = code_revision()
        rec = {"v": RECORD_VERSION, "code": self.code, "pool": pool, "side": side, "amount": str(amount),
               "requested_at": round(self.clock(), 3), "commitment": COMMITMENT}
        try:
            url = self.url()
            register(url)                                # its key is redacted wherever an error text carries it
            rec["host"] = urlparse(url).hostname or ""
            st = self.static.get(pool) or self._discover(pool)
            if st["quote_mint"] != ps.WSOL:
                raise Reject("non_sol_quote", st["quote_mint"])
            keys = [pool, st["pool_base_token_account"], st["pool_quote_token_account"], st["base_mint"],
                    self.global_config, self.fee_config]
            t0 = self.clock()
            slot, accs = self.fetch(url, keys)
            rec["responded_at"] = round(self.clock(), 3)
            rec["response_s"] = round(rec["responded_at"] - t0, 3)
            rec["context_slot"], rec["ref_slot"] = slot, int(self.ref_slot() or 0)
            rec["ref"] = FRESHNESS_REF
            names = ("pool", "base_vault", "quote_vault", "base_mint", "global_config", "fee_config")
            missing = [n for n, a in zip(names, accs) if a is None]
            if missing:
                raise Reject("missing_account", ",".join(missing))
            # what was priced, reproducibly: each account's hash
            rec["accounts"] = {n: {"address": k, "owner": a[0], "bytes": len(a[1]),
                                   "sha256": hashlib.sha256(a[1]).hexdigest(),
                                   "b64": base64.b64encode(a[1]).decode()} for n, k, a in zip(names, keys, accs)}
            (po, pd), (bo, bd), (qo, qd), (mo, md), (go, gd), (fo, fd) = accs
            if po != AMM or go != AMM or fo != FEE_PROGRAM:
                raise Reject("wrong_owner", "pool or config")
            try:
                p = ps.decode_pool(pd)
            except ps.QuoteError as ex:
                raise Reject("unsupported_layout", str(ex)) from None
            if any(p[k] != st[k] for k in st):
                self.static.pop(pool, None)              # rediscovered next time
                raise Reject("pool_changed", "the pool points at other accounts than first discovered")
            base, quote = decode_token_account(bd, bo), decode_token_account(qd, qo)
            if base["mint"] != p["base_mint"] or quote["mint"] != p["quote_mint"] or \
                    base["authority"] != pool or quote["authority"] != pool:
                raise Reject("vault_mismatch")
            try:
                mint = ps.decode_mint(md, mo)
            except ps.QuoteError as ex:
                raise Reject("mint_extensions" if "extension" in str(ex) else "unsupported_layout", str(ex)) from None
            if bo != mo or qo != ps.TOKEN_PROGRAM:       # a token account belongs to its mint's program (WSOL: Token)
                raise Reject("vault_program_mismatch", "a vault's token program isn't its mint's")
            rec.update(mint_supply=str(mint["supply"]), mint_decimals=mint["decimals"], mint_program=mo)
            g, fc = decode_global_config(gd), decode_fee_config(fd)
            if g["disable_flags"] & (0b1000 if side == "buy" else 0b10000):
                raise Reject("trading_disabled", side)
            eff = quote["amount"] + p["virtual_quote_reserves"]
            buckets = p["protocol_fees"] + p["creator_fees"]           # fees kept in the vault: not liquidity
            if buckets > quote["amount"]:
                raise Reject("invalid_state", f"fee buckets {buckets} exceed the quote vault {quote['amount']}")
            rec.update(base_reserve=str(base["amount"]), quote_vault=str(quote["amount"]),
                       virtual_quote=str(p["virtual_quote_reserves"]), effective_quote=str(eff),
                       protocol_fees=str(p["protocol_fees"]), creator_fees=str(p["creator_fees"]),
                       real_quote=str(quote["amount"] - buckets),
                       mayhem=p["is_mayhem_mode"], undocumented_tail=p["undocumented_tail"],
                       mint_extensions=mint["extensions"], pool_bytes=p["bytes"], serialized_end=p["serialized_end"],
                       padding_bytes=p["padding_bytes"], padding_sha256=p["padding_sha256"],
                       pool_b64=base64.b64encode(pd).decode(), sdk=ps.SDK, idl=ps.POOL_LAYOUT,
                       instruction="buy" if side == "buy" else "sell")     # the per-trade-fee instructions
            fees = ps.fees_bps({**g, "creator_fee_configurable": g["creator_fee_configurable"]}, fc, p["creator"],
                               p["base_mint"], mint["supply"], base["amount"], eff, p["quote_mint"],
                               p["is_mayhem_mode"], p["creator_fee_bps"])
            rec["fees_bps"] = fees
            fn = ps.buy_quote_input if side == "buy" else ps.sell_base_input
            try:
                out = fn(int(amount), 0, base["amount"], quote["amount"], fees, p["virtual_quote_reserves"],
                         p["coin_creator"], **({"fee_buckets": buckets} if side == "sell" else {}))
            except ps.QuoteError as ex:
                code = "insufficient_liquidity" if "Insufficient real quote" in str(ex) else \
                    {"state": "invalid_state", "output": "unexecutable_output"}.get(ex.code, "quote_error")
                raise Reject(code, str(ex)) from None
            rec["output"] = str(out["base"] if side == "buy" else out["uiQuote"])
            if side == "buy":
                rec["round_trip"] = _round_trip(int(amount), out, base["amount"], quote["amount"], fees, p, buckets)
            # qualified: the whole account is documented (a pool with undocumented bytes past the known prefix is
            # quoted from that prefix and labelled - its layout risk stays explicit, a tenth review), AND its
            # freshness was checked against a reference (no reference is no evidence of freshness, an eleventh)
            unq = (["undocumented_pool_bytes"] if p["undocumented_tail"] else []) + \
                ([] if rec["ref_slot"] else ["no_freshness_reference"])
            rec.update(qualified=not unq, unqualified=unq, layout=p["layout"], tail_sha256=p["tail_sha256"])
            if rec["response_s"] > MAX_RESPONSE_S:
                raise Reject("slow_response", f"{rec['response_s']} s")
            if rec["ref_slot"] and slot < rec["ref_slot"] - MAX_SLOT_LAG:
                raise Reject("stale_state", f"{rec['ref_slot'] - slot} slots behind the feed")
            rec["reason"] = "ok"
        except Reject as ex:
            rec["reason"], rec["detail"] = ex.reason, ex.detail[:200]
        except Exception as ex:                          # the RPC itself: timeouts, HTTP errors, malformed answers
            # never the exception's own text: an HTTP error's message is its whole request URL, key included
            rec["error"] = safe_error(ex)
            rec["reason"] = "timeout" if "imeout" in rec["error"]["error"] else "rpc_error"
            rec["detail"] = describe(ex)
        rec["finished_at"] = round(self.clock(), 3)      # when this quote (or its refusal) was in hand
        return rec


def _round_trip(lamports: int, bought: dict, base_reserve: int, quote_vault: int, fees: dict, p: dict,
                buckets: int = 0) -> dict:
    """The tokens just bought, sold back at once into the pool as our own buy would leave it (our SOL less the
    protocol and creator fees enters the quote vault - the LP fee stays in the pool - and the tokens leave the base
    vault; the same fee tier; no other trader, no network fee): the round-trip cost of this size at this state. The
    sell back draws only on real reserves (the vault less the fee buckets). MODELLED from the quote's own state."""
    eff = bought["internalQuoteWithoutFees"]
    try:
        back = ps.sell_base_input(bought["base"], 0, base_reserve - bought["base"],
                                  quote_vault + eff + ps.fee(eff, fees["lp"]), fees, p["virtual_quote_reserves"],
                                  p["coin_creator"], fee_buckets=buckets)["uiQuote"]
    except ps.QuoteError as ex:
        return {"lamports_back": None, "why": ex.code}
    return {"lamports_back": str(back), "pct": round(100 * (back / lamports - 1), 3),
            "model": "post-buy state (per-trade-fee buy), same fee tier, real reserves for the sell, no network fee"}


# --------------------------------------------------------------------------- the schedule and its record
TALLIES = ("first_try_raw_ok", "first_try_qualified_ok", "eventual_raw_ok", "eventual_qualified_ok")
OK_LABEL = {"qualified": "ok (qualified)", "prefix-only": "ok, prefix-only (undocumented pool bytes)",
            "unqualified": "ok, unqualified (see the attempt's reasons)",
            "unknown": "ok, qualification unknown (a record from before the rules)"}


def qualification(r: dict) -> str:
    """A successful quote record's population: qualified; prefix-only (undocumented pool bytes only); unqualified
    (another reason, e.g. no freshness reference); or unknown (a record from before qualification was recorded -
    missing is unknown, not true)."""
    if r.get("v", 1) < 2 or "qualified" not in r:
        return "unknown"
    if r["qualified"] is True:
        return "qualified"
    return "prefix-only" if r.get("unqualified") == ["undocumented_pool_bytes"] else "unqualified"


def _finite_pos(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _pair(m: dict, amount, due, qual: str, entries: dict) -> str:
    """A round trip's population. QUALIFIED only with complete, matching evidence (fail closed - a twelfth review):
    the follow's entry is found; both ends are qualified; the exit sells what the entry bought, for the entry's stake;
    its exit name is one of the follow's declared holds, a finite positive duration; the entry's record carries its
    in-hand time (`job.usable_at`, never the bare response time); and the exit fell due exactly that hold after it.
    Missing or malformed evidence is `unknown`. Otherwise the worse end's label (unknown < unqualified < prefix-only)."""
    e = entries.get(m.get("follow"))
    if e is None:
        return "unknown"
    er, eamount, equal = e
    hold = _finite_pos((m.get("holds") or {}).get(m.get("exit"))) if isinstance(m.get("holds"), dict) else None
    entry_at = _finite_pos((er.get("job") or {}).get("usable_at"))
    due_t = _finite_pos(due)
    matched = (hold is not None and entry_at is not None and due_t is not None
               and str(amount) == str(er.get("output")) and str(m.get("entry_lamports")) == str(eamount)
               and abs(due_t - (entry_at + hold)) < 0.01)
    if not matched:
        return "unknown"
    for label in ("unknown", "unqualified", "prefix-only"):
        if label in (qual, equal):
            return label
    return "qualified"


class QuoteBook:
    """Quote jobs and every attempt, in SQLite (data/quotes.db). A job: a key, a pool, buy/sell, an amount, when it's
    due and until when it may be retried. An entry job that succeeds creates its exit jobs (one per hold), each due
    `hold` seconds after the entry quote and sized by the tokens it bought."""

    def __init__(self, path: Path, quoter: Quoter, clock=time.time):
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        # `due` is the job's SCHEDULED time - the hold clock, Q1's window and pool-day, the recovery baseline - and
        # never changes; `next_at` is only the retry queue (a thirteenth review: retries had overwritten `due`)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, pool TEXT NOT NULL, "
                        "side TEXT NOT NULL, amount TEXT NOT NULL, due REAL NOT NULL, deadline REAL NOT NULL, "
                        "state TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, result TEXT, meta TEXT, "
                        "created REAL NOT NULL, next_at REAL, due_known INTEGER NOT NULL DEFAULT 1)")
        self._migrate()
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_next ON jobs (state, next_at)")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (job TEXT NOT NULL, n INTEGER NOT NULL, rec TEXT NOT NULL, "
                        "PRIMARY KEY (job, n))")
        self.quoter, self.clock = quoter, clock
        self.lock = threading.Lock()                     # the event loop requests while a worker thread runs jobs
        self._view, self._view_at = None, 0.0

    BOOK_VERSION = 2

    def _migrate(self) -> None:
        """Book version 1 -> 2, once, in one transaction: add `next_at` and `due_known`. A version-1 job that had a
        retry scheduled (tries >= 2, or still pending after a try) had its `due` overwritten by that retry: its
        original scheduled time is NOT reconstructed - `due_known` = 0 marks it unknown, and `next_at` takes over the
        queue."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= self.BOOK_VERSION:
            return
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if "next_at" not in cols:
                self.db.execute("ALTER TABLE jobs ADD COLUMN next_at REAL")
            if "due_known" not in cols:
                self.db.execute("ALTER TABLE jobs ADD COLUMN due_known INTEGER NOT NULL DEFAULT 1")
                self.db.execute("UPDATE jobs SET due_known = 0, next_at = due WHERE tries >= 2 OR "
                                "(state = 'pending' AND tries >= 1)")
            self.db.execute(f"PRAGMA user_version = {self.BOOK_VERSION}")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @classmethod
    def read_only(cls, path: Path) -> "QuoteBook":
        """The book opened read-only, for exports (no quoter: nothing is run)."""
        b = cls.__new__(cls)
        b.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False)
        b.quoter, b.clock, b.lock, b._view, b._view_at = None, time.time, threading.Lock(), None, 0.0
        return b

    def request(self, key: str, kind: str, pool: str, side: str, amount: int, due: float, deadline: float,
                meta: dict | None = None) -> bool:
        """A new job (an existing key is left alone: requests are idempotent across restarts)."""
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO jobs (id, kind, pool, side, amount, due, deadline, state, "
                                  "meta, created) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                                  (key, kind, pool, side, str(int(amount)), due, deadline, json.dumps(meta or {}),
                                   self.clock()))
        return bool(cur.rowcount)

    def due(self, now: float, limit: int = 20) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT id, kind, pool, side, amount, due, deadline, tries, meta FROM jobs WHERE "
                                   "state = 'pending' AND COALESCE(next_at, due) <= ? ORDER BY COALESCE(next_at, due) "
                                   "LIMIT ?", (now, limit)).fetchall()

    def run(self, now: float | None = None, limit: int = 20) -> int:
        """Run the due jobs (blocking: call it off the event loop). Returns how many were attempted.

        The clock (an eleventh review): a job is attempted only before its deadline, and its quote counts only if it
        was IN HAND by the deadline - `usable_at` (when the read was validated and priced) <= deadline, inclusive.
        A quote that arrives later is kept as an attempt but never accepted, and never re-timed into the window: a
        late entry is skipped, a late exit unmeasured (`late_response`). Every attempt is persisted, ok or not."""
        jobs = self.due(self.clock() if now is None else now, limit)
        for key, kind, pool, side, amount, due, deadline, tries, meta in jobs:
            meta = json.loads(meta or "{}")
            t = self.clock()
            if t > deadline:                             # missed (the process was down, or a backlog): never quoted late
                with self.lock:
                    self._finish(key, "unmeasured" if kind == "exit" else "skipped",
                                 {"reason": "missed", "detail": "past its deadline when run"})
                continue
            rec = self.quoter.quote(pool, side, int(amount))     # (the RPC call: outside the lock)
            usable = float(rec.get("finished_at") or self.clock())
            rec["job"] = {"due": due, "deadline": deadline, "started_at": round(t, 3), "usable_at": round(usable, 3),
                          "overrun_s": round(max(0.0, usable - deadline), 3)}
            on_time = usable <= deadline
            final = "unmeasured" if kind == "exit" else "skipped"
            retry_at = None
            if rec["reason"] != "ok" and rec["reason"] not in PERMANENT:
                nxt = usable + (RETRY_EVERY_S if kind == "exit" else 10)
                retry_at = nxt if nxt <= deadline else None
            rec["job"]["next_attempt_at"] = retry_at and round(retry_at, 3)
            with self.lock:                              # released on every path, a failed BEGIN's included
                began = False
                try:
                    self.db.execute("BEGIN IMMEDIATE")
                    began = True
                    self.db.execute("INSERT INTO attempts (job, n, rec) VALUES (?, ?, ?)",     # (never overwritten)
                                    (key, tries + 1, json.dumps(rec)))
                    if rec["reason"] == "ok" and on_time:
                        self.db.execute("UPDATE jobs SET state = 'ok', tries = ?, result = ? WHERE id = ?",
                                        (tries + 1, json.dumps(rec), key))
                        if kind == "entry":
                            for name, hold in (meta.get("holds") or {}).items():
                                at = usable + hold
                                self.db.execute("INSERT OR IGNORE INTO jobs (id, kind, pool, side, amount, due, "
                                                "deadline, state, meta, created) VALUES (?, 'exit', ?, 'sell', ?, ?, ?, "
                                                "'pending', ?, ?)",
                                                (f"{meta['follow']}|{name}", pool, rec["output"], at, at + RETRY_S,
                                                 json.dumps({**meta, "exit": name, "entry_lamports": str(amount)}), t))
                    elif rec["reason"] == "ok":              # a valid quote, but in hand after the window closed
                        late = {"reason": "late_response", "detail": f"in hand {rec['job']['overrun_s']} s after the "
                                f"deadline", "raw_reason": "ok", "raw_output": rec.get("output"), "job": rec["job"],
                                "v": RECORD_VERSION}
                        self.db.execute("UPDATE jobs SET state = ?, tries = ?, result = ? WHERE id = ?",
                                        (final, tries + 1, json.dumps(late), key))
                    else:
                        if retry_at is not None:
                            self.db.execute("UPDATE jobs SET next_at = ?, tries = ? WHERE id = ?",   # (never `due`)
                                            (retry_at, tries + 1, key))
                        else:
                            self.db.execute("UPDATE jobs SET state = ?, tries = ?, result = ? WHERE id = ?",
                                            (final, tries + 1, json.dumps(rec), key))
                    self.db.execute("COMMIT")
                except BaseException:
                    if began:
                        try:
                            self.db.execute("ROLLBACK")
                        except sqlite3.Error:            # (the original failure is the one that matters)
                            pass
                    raise
        return len(jobs)

    def _finish(self, key: str, state: str, rec: dict) -> None:
        self.db.execute("UPDATE jobs SET state = ?, result = ? WHERE id = ?", (state, json.dumps(rec), key))

    def view(self, max_age_s: float = 30) -> dict:
        """Coverage by job kind and arm, and quote-priced net P&L - with raw and qualified results kept apart.

        Coverage counts each finished job's outcome: `ok (qualified)`, `ok, prefix-only` (undocumented pool bytes),
        `ok, unqualified: <reasons>`, `ok, qualification unknown` (a record from before the qualification rules -
        unknown, never assumed), or `<state>: <reason>`; and first-try / eventual successes, raw and qualified.
        P&L pairs each exit with its follow's entry: a QUALIFIED round trip needs both ends qualified and matched (the
        exit sells what the entry bought, for the entry's stake, on the entry's clock). Every other row is labelled
        with its population (prefix-only, unqualified, unknown) and never pooled with the qualified one."""
        if self._view is not None and self.clock() - self._view_at < max_age_s:
            return self._view
        with self.lock:
            rows = self.db.execute("SELECT id, kind, state, tries, amount, due, result, meta FROM jobs "
                                   "WHERE state != 'pending'").fetchall()
            pending = self.db.execute("SELECT COUNT(*) FROM jobs WHERE state = 'pending'").fetchone()[0]
        cov: dict = defaultdict(Counter)
        tally: dict = defaultdict(Counter)
        entries, exits = {}, []
        for key, kind, state, tries, amount, due, result, meta in rows:
            m, r = json.loads(meta or "{}"), json.loads(result or "{}")
            arm = "control" if m.get("control") else "signal"
            qual = qualification(r) if state == "ok" else None
            cov[(kind, arm)][OK_LABEL[qual] if qual else f"{state}: {r.get('reason', '?')}"] += 1
            t = tally[(kind, arm)]
            if state == "ok":
                t["eventual_raw_ok"] += 1
                t["eventual_qualified_ok"] += qual == "qualified"
                t["first_try_raw_ok"] += tries == 1
                t["first_try_qualified_ok"] += tries == 1 and qual == "qualified"
            if kind == "entry" and state == "ok":
                entries[m.get("follow")] = (r, amount, qual)
            if kind == "exit" and state == "ok":
                exits.append((m, r, amount, due, qual, arm))
        pnl: dict = defaultdict(list)
        for m, r, amount, due, qual, arm in exits:
            cost = int(m.get("entry_lamports", 0)) / 1e9
            if cost <= 0:
                continue
            got = int(r.get("output", 0)) / 1e9
            net = (got - TX_COST_SOL) / (cost + TX_COST_SOL) - 1      # each transaction's network fee
            pnl[(m.get("rule", ""), m.get("delay"), m.get("exit"), arm, _pair(m, amount, due, qual, entries))
                ].append(net * 100)
        out_cov = {f"{k[0]} / {k[1]}": {"done": sum(v.values()), **{n: tally[k][n] for n in TALLIES}, **dict(v)}
                   for k, v in sorted(cov.items())}
        out_pnl = [{"rule": k[0], "delay_s": k[1], "exit": k[2], "arm": k[3], "qualification": k[4],
                    "qualified": k[4] == "qualified", "n": len(xs), "mean_pct": round(statistics.fmean(xs), 2),
                    "median_pct": round(statistics.median(xs), 2)}
                   for k, xs in sorted(pnl.items(), key=lambda kv: tuple(str(x) for x in kv[0]))]
        self._view = {"coverage": out_cov, "quote_pnl": out_pnl, "pending": pending, "record_version": RECORD_VERSION,
                      "note": "quotes are not fills; raw-math success and qualified success are counted apart; "
                              "exploratory (the revival forward test's follows), not a declared qualification window"}
        self._view_at = self.clock()
        return self._view

    def _cols(self) -> str:
        have = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        return ", ".join(c if c in have else f"NULL AS {c}" for c in ("next_at", "due_known"))

    def jobs(self) -> list[dict]:
        """Every job and its outcome - including those that finished with no attempt (missed) and those pending - for
        the package (export version 2: identity, schedule, outcome and the job's full metadata, holds included, are
        separate fields; nothing is merged over anything)."""
        with self.lock:
            rows = self.db.execute(f"SELECT id, kind, pool, side, amount, due, deadline, state, tries, result, meta, "
                                   f"created, {self._cols()} FROM jobs ORDER BY created, id").fetchall()
        out = []
        for key, kind, pool, side, amount, due, deadline, state, tries, result, meta, created, nxt, known in rows:
            r = json.loads(result or "{}")
            out.append({"export_version": EXPORT_VERSION, "job_id": key, "kind": kind, "pool": pool, "side": side,
                        "amount": amount, "scheduled_due": due, "due_known": bool(known if known is not None else 1),
                        "deadline": deadline, "next_attempt_at": nxt, "created": created, "state": state,
                        "tries": tries, "reason": r.get("reason"),
                        "qualification": qualification(r) if state == "ok" else None, "meta": json.loads(meta or "{}")})
        return out

    def attempts(self) -> list[dict]:
        """Every attempt, for the review package (pool addresses, amounts and account bytes are public chain data).
        Export version 2: `job_id` and `attempt_n` identify it, `record` is the stored attempt as written (its own
        `job` clock inside it) - never expanded over the identity fields (a thirteenth review)."""
        with self.lock:
            rows = self.db.execute("SELECT a.job, a.n, a.rec, j.kind FROM attempts a JOIN jobs j ON j.id = a.job "
                                   "ORDER BY a.rowid").fetchall()
        return [{"export_version": EXPORT_VERSION, "job_id": j, "attempt_n": n, "kind": k, "record": json.loads(rec)}
                for j, n, rec, k in rows]
