"""A closed trade's P&L is net of everything it cost (a sixth review, 2026-10-07).

Cash always paid the fees of transactions that landed but failed (a sell retried at more slippage, say), and so did
the day's P&L - but a closed position's `pnl` was `proceeds - cost`, with those fees only reported beside it
(`failed_fees_sol`). Win/loss counts, defense mode, copy pausing, analytics and research all read `pnl` as net.

Schema 2: `pnl` = `gross_pnl` - `failed_fees_sol` (`pnl_pct` of the cost likewise); `gross_pnl` is kept. Successful
fills are already all-in (their own fees are inside the SOL received or spent), so nothing is subtracted twice. Rent is
neither: it's a refundable deposit, outside P&L. Rows written before schema 2 are upgraded when loaded, from their
own recorded fees, and flagged `net_derived` when that changed them.
"""
from __future__ import annotations

SCHEMA = 2


def upgrade(row: dict) -> dict:
    """The row with schema-2 net P&L (in place; idempotent)."""
    if not isinstance(row, dict) or row.get("pnl_schema", 1) >= SCHEMA or row.get("pnl") is None:
        return row
    fees = float(row.get("failed_fees_sol") or 0.0)
    row["gross_pnl"] = row["pnl"]
    if fees:
        row["pnl"] = row["pnl"] - fees
        cost = float(row.get("cost") or 0.0)
        if cost > 0:
            row["pnl_pct"] = row["pnl"] / cost * 100
        row["net_derived"] = True
    row["pnl_schema"] = SCHEMA
    return row


def upgrade_all(rows):
    return [upgrade(r) for r in rows]
