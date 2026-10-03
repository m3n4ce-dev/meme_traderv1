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

    def sign(self, tx_b64: str) -> tuple[str, str]:
        """(signed tx base64, its signature). The signature is known BEFORE sending, so an order whose
        send call times out can still be looked up instead of being re-sent blindly."""
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        if str(unsigned.message.account_keys[0]) != self.pubkey:
            raise RuntimeError("transaction fee payer is not this wallet - refusing to sign")
        signed = VersionedTransaction(unsigned.message, [self._kp])
        return base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0])

    def send(self, raw_b64: str) -> str:
        return rpc("sendTransaction", [raw_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])

    def sign_and_send(self, tx_b64: str) -> str:
        raw, _ = self.sign(tx_b64)
        return self.send(raw)

    def simulate_sol_change(self, tx_b64: str) -> float:
        """SOL this wallet would gain (+) or lose (-) if the unsigned transaction ran now. Raises if the
        simulation fails (the transaction would fail too)."""
        before = rpc("getBalance", [self.pubkey, {"commitment": "processed"}])["value"]
        sim = rpc("simulateTransaction", [tx_b64, {"encoding": "base64", "sigVerify": False,
                                                    "replaceRecentBlockhash": True, "commitment": "processed",
                                                    "accounts": {"encoding": "base64", "addresses": [self.pubkey]}}])
        v = sim["value"]
        if v.get("err"):
            raise RuntimeError(f"simulation failed: {v['err']} {(v.get('logs') or [''])[-1][:120]}")
        after = (v.get("accounts") or [None])[0]
        if not after:
            raise RuntimeError("simulation returned no balance for this wallet")
        return (after["lamports"] - before) / 1e9

    def close_empty_token_accounts(self, mint: str) -> tuple[str, float]:
        """Close this wallet's zero-balance token accounts for `mint` and reclaim their rent. Returns
        (signature, SOL the accounts held) - ("", 0.0) if there was nothing to close. Works for SPL Token
        and Token-2022 (program taken from the account's owner). The token program rejects closing a
        non-empty account, so this can never burn tokens."""
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction

        owner = self._kp.pubkey()
        res = rpc("getTokenAccountsByOwner", [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
        ixs = []
        lamports = 0
        for a in res["value"]:
            if int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) != 0:
                continue
            lamports += int(a["account"].get("lamports") or 0)
            ixs.append(Instruction(Pubkey.from_string(a["account"]["owner"]), bytes([9]),   # 9 = CloseAccount
                                   [AccountMeta(Pubkey.from_string(a["pubkey"]), False, True),
                                    AccountMeta(owner, False, True), AccountMeta(owner, True, False)]))
        if not ixs:
            return "", 0.0
        bh = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        tx = VersionedTransaction(MessageV0.try_compile(owner, ixs, [], Hash.from_string(bh)), [self._kp])
        sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
        return sig, lamports / 1e9

    def sol_balance(self) -> float:
        return rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])["value"] / 1e9

    def all_token_balances(self) -> dict[str, int]:
        """mint -> raw balance for every non-empty token account (SPL Token + Token-2022)."""
        out: dict[str, int] = {}
        for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            res = rpc("getTokenAccountsByOwner", [self.pubkey, {"programId": program},
                                                  {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for a in res["value"]:
                info = a["account"]["data"]["parsed"]["info"]
                amt = int(info["tokenAmount"]["amount"])
                if amt:
                    out[info["mint"]] = out.get(info["mint"], 0) + amt
        return out

    def tx_deltas(self, signature: str, mint: str) -> dict | None:
        """What a confirmed transaction did to THIS wallet, read from the transaction itself (so other
        trades running at the same time can't pollute it): SOL change, raw token change for `mint`,
        rent paid for a newly created token account, and whether the tx failed. None = not visible yet."""
        tx = rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                "maxSupportedTransactionVersion": 0}])
        if not tx:
            return None
        meta = tx.get("meta") or {}
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        me = keys.index(self.pubkey) if self.pubkey in keys else 0
        dsol = (meta["postBalances"][me] - meta["preBalances"][me]) / 1e9

        def amounts(rows):
            return {r["accountIndex"]: int(r["uiTokenAmount"]["amount"]) for r in rows or []
                    if r.get("mint") == mint and r.get("owner") == self.pubkey}
        pre, post = amounts(meta.get("preTokenBalances")), amounts(meta.get("postTokenBalances"))
        dtok = sum(post.values()) - sum(pre.values())
        rent = sum(meta["postBalances"][i] - meta["preBalances"][i] for i in post if i not in pre) / 1e9
        return {"dsol": dsol, "dtok": dtok, "rent": max(rent, 0.0), "failed": meta.get("err") is not None}

    def token_balance(self, mint: str) -> int:
        res = rpc("getTokenAccountsByOwner", [self.pubkey, {"mint": mint},
                                              {"encoding": "jsonParsed", "commitment": "confirmed"}])
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
