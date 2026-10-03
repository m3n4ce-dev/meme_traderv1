"""Jupiter Swap API v1 (Metis router). Requires an API key from https://portal.jup.ag
(lite-api.jup.ag was deprecated 2026-01-31)."""
from __future__ import annotations

import os

from .http import get, post

BASE = "https://api.jup.ag/swap/v1"


def _headers() -> dict:
    key = os.environ.get("JUPITER_API_KEY", "")
    return {"x-api-key": key} if key else {}


def has_key() -> bool:
    return bool(os.environ.get("JUPITER_API_KEY"))


def quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    return get(f"{BASE}/quote", headers=_headers(), params={
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": str(amount), "slippageBps": slippage_bps,
    })


def swap_tx(quote_response: dict, user_pubkey: str, priority_fee_lamports: int) -> str:
    """Returns a base64 unsigned VersionedTransaction."""
    r = post(f"{BASE}/swap", headers=_headers(), json={
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": priority_fee_lamports,
    })
    return r["swapTransaction"]
