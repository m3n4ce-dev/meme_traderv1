"""Live-mode wallet: keypair loading, signing, and Solana JSON-RPC.

The private key is read from the file at env SOLANA_KEYPAIR_PATH (Solana CLI JSON
format) and never logged or written anywhere. Use a dedicated hot wallet holding
only the bot's budget - never your main wallet.
"""
from __future__ import annotations

import base64
import json
import os
import time

from .clients.http import post

RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"


def rpc(method: str, params: list) -> dict:
    r = post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in r:
        raise RuntimeError(f"RPC {method}: {r['error']}")
    return r["result"]


class Wallet:
    def __init__(self, expected_pubkey: str = ""):
        from solders.keypair import Keypair  # imported lazily: only needed for live mode

        path = os.environ.get("SOLANA_KEYPAIR_PATH")
        if not path:
            raise RuntimeError("SOLANA_KEYPAIR_PATH not set")
        with open(path) as f:
            self._kp = Keypair.from_bytes(bytes(json.load(f)))
        self.pubkey = str(self._kp.pubkey())
        if expected_pubkey and expected_pubkey != self.pubkey:
            raise RuntimeError(f"keypair pubkey {self.pubkey} != configured wallet.pubkey {expected_pubkey}")

    def sign_and_send(self, tx_b64: str) -> str:
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [self._kp])
        raw = base64.b64encode(bytes(signed)).decode()
        return rpc("sendTransaction", [raw, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])

    def close_empty_token_accounts(self, mint: str) -> int:
        """Close this wallet's zero-balance token accounts for `mint` and reclaim their rent.
        Works for SPL Token and Token-2022 (program taken from the account's owner). The token
        program rejects closing a non-empty account, so this can never burn tokens."""
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction

        owner = self._kp.pubkey()
        res = rpc("getTokenAccountsByOwner", [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
        ixs = []
        for a in res["value"]:
            if int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) != 0:
                continue
            ixs.append(Instruction(Pubkey.from_string(a["account"]["owner"]), bytes([9]),   # 9 = CloseAccount
                                   [AccountMeta(Pubkey.from_string(a["pubkey"]), False, True),
                                    AccountMeta(owner, False, True), AccountMeta(owner, True, False)]))
        if not ixs:
            return 0
        bh = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        tx = VersionedTransaction(MessageV0.try_compile(owner, ixs, [], Hash.from_string(bh)), [self._kp])
        rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
        return len(ixs)

    def sol_balance(self) -> float:
        return rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])["value"] / 1e9

    def token_balance(self, mint: str) -> int:
        res = rpc("getTokenAccountsByOwner", [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])


def confirm(signature: str, timeout_s: int = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = rpc("getSignatureStatuses", [[signature]])["value"][0]
        if st and st.get("err"):
            return False
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return True
        time.sleep(2)
    return False
