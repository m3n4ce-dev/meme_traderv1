"""PumpSwap, pump.fun's AMM where graduated coins trade: decode its Anchor swap events from transaction logs.

Layouts checked against live transactions on 2026-10-04 (BuyEvent 489/504 bytes, SellEvent 441). Only the
fields the wallet study needs are read: chain time, amounts, reserves, pool and user. The recorder uses this
for discovery (which pools are trading right now); the trades themselves come from GeckoTerminal.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass

AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
BUY_EVENT = hashlib.sha256(b"event:BuyEvent").digest()[:8]
SELL_EVENT = hashlib.sha256(b"event:SellEvent").digest()[:8]
MIN_LEN = 344      # discriminator .. coin_creator; newer versions append fields after it


@dataclass(frozen=True, slots=True)
class Swap:
    ts: int            # chain time (s)
    pool: str
    user: str
    side: str          # "buy" | "sell"
    base: int          # token units moved (raw, 6 decimals for pump coins)
    quote: int         # quote units (lamports) the user paid incl. fees (buy) or received (sell)
    pool_base: int     # pool reserves when the swap executed
    pool_quote: int


def _pk(raw: bytes, off: int) -> str:
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(raw[off:off + 32]))


def decode_event(raw: bytes) -> Swap | None:
    """BuyEvent / SellEvent bytes -> Swap. Both start: disc(8) timestamp(i64) 13 x u64, pool, user, ...
    Buy u64s:  base_out, max_quote_in, user_base_res, user_quote_res, pool_base_res, pool_quote_res, quote_in,
               lp_fee_bps, lp_fee, protocol_fee_bps, protocol_fee, quote_in_with_lp_fee, user_quote_in.
    Sell u64s: base_in, min_quote_out, (same 4 reserves), quote_out, (same 4 fee fields),
               quote_out_without_lp_fee, user_quote_out."""
    if len(raw) < MIN_LEN or raw[:8] not in (BUY_EVENT, SELL_EVENT):
        return None
    ts = struct.unpack_from("<q", raw, 8)[0]
    f = struct.unpack_from("<13Q", raw, 16)
    return Swap(ts=ts, pool=_pk(raw, 120), user=_pk(raw, 152), side="buy" if raw[:8] == BUY_EVENT else "sell",
                base=f[0], quote=f[12], pool_base=f[4], pool_quote=f[5])


def parse_logs(logs: list[str]) -> list[Swap]:
    """Swap events the AMM itself emitted. The invoke stack is tracked because any Anchor program with an
    event named BuyEvent emits the same discriminator."""
    out: list[Swap] = []
    stack: list[str] = []
    for line in logs:
        parts = line.split(" ", 3)
        if len(parts) < 3 or parts[0] != "Program":
            continue
        if parts[1] == "data:":
            if stack and stack[-1] == AMM_PROGRAM:
                try:
                    raw = base64.b64decode(parts[2])
                except (binascii.Error, ValueError):
                    continue
                s = decode_event(raw)
                if s:
                    out.append(s)
        elif parts[1].endswith(":"):                 # "Program log: ...", "Program return: ..."
            continue
        elif parts[2] == "invoke":
            stack.append(parts[1])
        elif parts[2] in ("success", "failed:", "failed") and stack:
            stack.pop()
    return out
