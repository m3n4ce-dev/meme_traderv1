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
DISCRIMINATOR = bytes.fromhex("17b7f83760d8ac60")   # sha256("account:BondingCurve")[:8]; checked on live accounts
LEGACY_LEN = 81                                       # a curve from before the creator/Mayhem upgrades ends here


def bonding_curve(mint: str) -> str:
    from solders.pubkey import Pubkey

    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                         Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def parse(data_b64: str, owner: str = PUMP_PROGRAM) -> bool | None:
    """True / False only from a validated pump.fun bonding curve: owned by the pump program, with the BondingCurve
    discriminator, and a flag byte that is 0 or 1. A recognized pre-upgrade layout (no flag yet: Mayhem didn't exist)
    is False. Anything else is None: unknown, never "safe" (a third review, 2026-10-06)."""
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except ValueError:
        return None
    if owner != PUMP_PROGRAM or raw[:8] != DISCRIMINATOR:
        return None
    if len(raw) <= MAYHEM_BYTE:                       # only the two documented pre-Mayhem layouts: the original
        return False if len(raw) in (49, LEGACY_LEN) else None   # (to `complete`) and with the creator; else unknown
    flag = raw[MAYHEM_BYTE]
    return bool(flag) if flag in (0, 1) else None


def lookup(mint: str) -> bool | None:
    """One RPC read (SOLANA_RPC_URL). None: no such account, or not a recognizable pump.fun curve - unknown.
    Raises on RPC errors: also unknown."""
    from ..wallet import rpc

    res = rpc("getAccountInfo", [bonding_curve(mint), {"encoding": "base64", "commitment": "confirmed"}])
    v = (res or {}).get("value")
    if not v:
        return None
    return parse(v["data"][0], v.get("owner", ""))
