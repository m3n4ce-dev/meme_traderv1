"""Is a pump.fun coin in Mayhem mode? Read from its bonding curve account, so it's known as soon as the coin exists
instead of when the Mayhem agent first trades it.

The account (pump.fun's public program docs, docs/PUMP_PROGRAM_README.md): an 8-byte Anchor discriminator, then
virtual_token_reserves, virtual_sol_reserves, real_token_reserves, real_sol_reserves, token_total_supply (u64 each),
complete (bool), creator (32-byte pubkey), is_mayhem_mode (bool) at byte 81, then fields added by later upgrades.
Checked on live coins (2026-10-06): byte 81 was 1 on three Mayhem coins and 0 on three others. A Mayhem coin mints 2B
tokens: 1B on the curve, ~1B with the Mayhem agent.
"""
from __future__ import annotations

import base64

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MAYHEM_BYTE = 81


def bonding_curve(mint: str) -> str:
    from solders.pubkey import Pubkey

    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                         Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def parse(data_b64: str) -> bool | None:
    """True / False from the account's bytes; None when the account predates the field."""
    raw = base64.b64decode(data_b64)
    return bool(raw[MAYHEM_BYTE]) if len(raw) > MAYHEM_BYTE else None


def lookup(mint: str) -> bool | None:
    """One RPC read (SOLANA_RPC_URL). None: no such account (not a pump.fun curve) or too old to say. Raises on RPC
    errors: the caller treats those as still unknown."""
    from ..wallet import rpc

    res = rpc("getAccountInfo", [bonding_curve(mint), {"encoding": "base64", "commitment": "confirmed"}])
    v = (res or {}).get("value")
    if not v:
        return None
    return parse(v["data"][0])
