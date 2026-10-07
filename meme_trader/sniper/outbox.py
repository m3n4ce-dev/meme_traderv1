"""The live order outbox: every signed transaction is on disk before it can reach the network.

A fourth external review (2026-10-07): the executor knows a transaction's signature before sending it, but the engine
used to record an order only after the executor returned. A crash while sending, confirming, or before a fill was saved
could leave a transaction on-chain that a restart knew nothing about.

So, per attempt (each escalating sell retry is its own signed transaction):
  1. build and sign;
  2. commit (signature, blockhash, side, mint, intent) here, synchronously and durably - if that fails, it isn't sent;
  3. send.
On restart the engine reconciles every outbox order it hasn't booked: it becomes an unresolved order (cash reserved,
the coin's other orders paused) until the chain says what happened. Live trading only: paper orders never reach the
network.

Booking protocol (two stores: the book is a JSON file, this is SQLite; a fifth review, 2026-10-07):
  1. `prepare`: the row is committed here as "signed" before the send.
  2. The outcome is applied to the book together with a booking receipt (the signature, unacknowledged), and that
     state is saved: fsync'd file, atomic rename, fsync'd directory.
  3. `mark(..., "booked")` here; once it commits, the receipt is acknowledged in memory and in the next save.
Recovery: a "signed" row with a receipt is already booked (it's marked, never re-applied); one without a receipt was
never booked (it becomes unresolved). An unacknowledged receipt is never pruned - only acknowledged ones are, oldest
first - so a failed `mark` can't turn into a second booking however many orders follow. A crash between 2 and 3, or
a failed 3, leaves an unacknowledged receipt that's retried until it succeeds.

The store belongs to one wallet on one network (`meta`): a different wallet's engine refuses to start on it, and an
unbound store that already has orders is used only after the owner claims it (`python -m meme_trader.sniper.outbox
claim <wallet>`).
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
import time
from pathlib import Path

# what the engine meant by the order now being sent (side, size, why): carried into the executor's thread
ORDER_INTENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("order_intent", default=None)

OPEN = ("signed",)                                     # committed before sending; outcome not yet booked
NETWORK = "solana-mainnet"
SCHEMA = 2                                             # 2: owner-bound (meta), booking receipts acknowledged
RECEIPTS_KEPT = 5000                                   # acknowledged receipts kept (unacknowledged: all of them)
ACK_ALERT = 50                                         # unacknowledged receipts that warrant telling the owner


class StoreOwnerError(RuntimeError):
    """A recovery store that belongs to another wallet or network, or an unbound one nobody has claimed."""


def owner_of(executor) -> dict:
    pub = str(getattr(getattr(executor, "wallet", None), "pubkey", "") or "")
    if not pub:
        raise StoreOwnerError("live trading needs the wallet's public key to bind its order store")
    return {"network": NETWORK, "wallet": pub, "schema": SCHEMA}


def same_owner(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and a.get("network") == b.get("network") and a.get("wallet") == b.get("wallet")


class Receipts:
    """Signatures whose outcome the book has booked (see the protocol above): sig -> acknowledged by the outbox.
    Unacknowledged receipts are pinned; acknowledged ones are pruned oldest first past `keep`."""

    def __init__(self, keep: int = RECEIPTS_KEPT):
        self.keep = keep
        self._d: dict[str, bool] = {}

    @classmethod
    def load(cls, data, keep: int = RECEIPTS_KEPT) -> "Receipts":
        r = cls(keep)
        for x in data or []:
            if isinstance(x, str):                     # (saved before receipts were acknowledged: re-confirmed)
                r._d.setdefault(x, False)
            elif isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[0], str):
                r._d[x[0]] = bool(x[1])
        return r

    def to_json(self) -> list:
        return [[k, v] for k, v in self._d.items()]

    def append(self, sig: str) -> None:
        if sig:
            self._d.setdefault(sig, False)

    def extend(self, sigs) -> None:
        for x in sigs or []:
            self.append(x)

    def ack(self, sigs) -> None:
        for x in sigs or []:
            if x in self._d:
                self._d[x] = True
        acked = [k for k, v in self._d.items() if v]
        for k in acked[:max(0, len(acked) - self.keep)]:
            del self._d[k]

    def unacked(self) -> list[str]:
        return [k for k, v in self._d.items() if not v]

    def __contains__(self, sig) -> bool:
        return sig in self._d

    def __iter__(self):
        return iter(list(self._d))

    def __len__(self) -> int:
        return len(self._d)


class Outbox:
    def __init__(self, path: Path, owner: dict | None = None):
        """owner (the engine always passes one): this store must belong to that wallet and network, or it raises
        StoreOwnerError. Without one (offline tools, tests) the store is opened as it is."""
        self.path = Path(path)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS orders (sig TEXT PRIMARY KEY, created REAL NOT NULL, mint TEXT NOT NULL, "
                        "side TEXT NOT NULL, blockhash TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL, "
                        "updated REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.owner = self.stored_owner()
        if owner is not None:
            self._bind(owner)

    def stored_owner(self) -> dict | None:
        m = dict(self.db.execute("SELECT k, v FROM meta").fetchall())
        if "wallet" not in m:
            return None
        return {"network": m.get("network", ""), "wallet": m["wallet"], "schema": int(m.get("schema", 0) or 0)}

    def _bind(self, owner: dict) -> None:
        have = self.owner
        if have is not None:
            if not same_owner(have, owner):
                raise StoreOwnerError(f"{self.path.name} belongs to wallet {have['wallet'][:6]}… on {have['network']}, "
                                      f"not {owner['wallet'][:6]}… on {owner['network']}: refusing to recover or send "
                                      "with it. Point this wallet at its own data directory.")
            if have.get("schema", 0) > SCHEMA:
                raise StoreOwnerError(f"{self.path.name} was written by a newer version (schema {have['schema']})")
            return
        n = self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if n:
            raise StoreOwnerError(f"{self.path.name} has {n} order(s) but no owner recorded: check whose they are, then "
                                  f"run `python -m meme_trader.sniper.outbox claim {owner['wallet']}`")
        self.claim(owner)

    def claim(self, owner: dict) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
                                [("network", owner["network"]), ("wallet", owner["wallet"]),
                                 ("schema", str(owner.get("schema", SCHEMA)))])
        self.owner = self.stored_owner()

    def prepare(self, sig: str, blockhash: str, mint: str, side: str, intent: dict) -> None:
        """Durable before the send, or raises (the caller then doesn't send)."""
        now = time.time()
        with self.db:                                   # (an explicit transaction; synchronous=FULL syncs it)
            self.db.execute("INSERT INTO orders (sig, created, mint, side, blockhash, intent, status, updated) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'signed', ?)",
                            (sig, now, mint, side, blockhash or "", json.dumps(intent, default=str), now))

    def mark(self, sigs, status: str) -> None:
        """Raises if it can't be committed (the caller keeps its receipt unacknowledged). A signature with no row
        (paper, or from before the outbox) is a no-op."""
        sigs = [s for s in sigs if s]
        if not sigs:
            return
        with self.db:
            self.db.executemany("UPDATE orders SET status = ?, updated = ? WHERE sig = ?",
                                [(status, time.time(), s) for s in sigs])

    def unbooked(self) -> list[dict]:
        rows = self.db.execute("SELECT sig, created, mint, side, blockhash, intent FROM orders WHERE status IN ('signed') "
                               "ORDER BY created").fetchall()
        return [{"sig": r[0], "created": r[1], "mint": r[2], "side": r[3], "blockhash": r[4], "intent": json.loads(r[5])}
                for r in rows]


def _main(argv=None) -> int:
    """`python -m meme_trader.sniper.outbox claim <wallet>`: bind an unbound order store and live book (written before
    stores were bound) to that wallet, after the owner has checked they're its. A store bound to another wallet is
    never rebound here."""
    import argparse

    from ..journal import DATA
    ap = argparse.ArgumentParser(prog="python -m meme_trader.sniper.outbox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim", help="bind the unbound live order store and book to this wallet")
    c.add_argument("wallet")
    c.add_argument("--data", default=str(DATA))
    a = ap.parse_args(argv)
    owner = {"network": NETWORK, "wallet": a.wallet, "schema": SCHEMA}
    data = Path(a.data)
    ob = Outbox(data / "orders.db")
    if ob.owner is not None and not same_owner(ob.owner, owner):
        print(f"orders.db already belongs to {ob.owner['wallet']} on {ob.owner['network']}: not changed")
        return 1
    ob.claim(owner)
    n = ob.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    print(f"orders.db: bound to {a.wallet} ({n} order(s))")
    sp = data / "sniper_state_live.json"
    if sp.exists():
        st = json.loads(sp.read_text())
        if st.get("owner") and not same_owner(st["owner"], owner):
            print(f"{sp.name} already belongs to {st['owner'].get('wallet')}: not changed")
            return 1
        st["owner"] = owner
        tmp = sp.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, default=str))
        tmp.replace(sp)
        print(f"{sp.name}: bound to {a.wallet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
