"""The live order outbox: every signed transaction is on disk before it can reach the network.

A fourth external review (2026-10-07): the executor knows a transaction's signature before sending it, but the engine
used to record an order only after the executor returned. A crash while sending, confirming, or before a fill was saved
could leave a transaction on-chain that a restart knew nothing about.

So, per attempt (each escalating sell retry is its own signed transaction):
  1. build and sign;
  2. commit (signature, blockhash, side, mint, intent) here, synchronously and durably - if that fails, it isn't sent;
  3. send.
On restart the engine reconciles every outbox order it hasn't booked: it becomes an unresolved order (cash reserved,
the coin's other orders paused) until the chain says what happened. Booking is idempotent, keyed by signature: the
engine's saved state lists the signatures it booked, so a crash between booking and marking it here can't book twice.
Live trading only: paper orders never reach the network.
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


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS orders (sig TEXT PRIMARY KEY, created REAL NOT NULL, mint TEXT NOT NULL, "
                        "side TEXT NOT NULL, blockhash TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL, "
                        "updated REAL NOT NULL)")

    def prepare(self, sig: str, blockhash: str, mint: str, side: str, intent: dict) -> None:
        """Durable before the send, or raises (the caller then doesn't send)."""
        now = time.time()
        with self.db:                                   # (an explicit transaction; synchronous=FULL syncs it)
            self.db.execute("INSERT INTO orders (sig, created, mint, side, blockhash, intent, status, updated) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'signed', ?)",
                            (sig, now, mint, side, blockhash or "", json.dumps(intent, default=str), now))

    def mark(self, sigs, status: str) -> None:
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
