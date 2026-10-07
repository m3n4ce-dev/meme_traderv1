"""The live PumpSwap price watcher (quotes.py) on a simulated chain: real config accounts, SDK-exact math, every
refusal reason-coded, and the job book's entry -> exit lifecycle with the registered retry clock."""
import base64
import json
import struct
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from meme_trader.sniper import pumpswap as ps
from meme_trader.sniper import quotes as q

FIX = Path(__file__).parent / "fixtures" / "pumpswap"
CONFIGS = json.loads((FIX / "mainnet-configs-2026-10-07.json").read_text())
GOLDEN = json.loads((FIX / "pumpswap-sdk-1.20.0-golden.json").read_text())
GC, FC = (base64.b64decode(a["dataBase64"]) for a in CONFIGS["accounts"])


def key(i: int) -> str:
    return str(Pubkey.from_bytes(bytes([i]) * 32))


POOL, BASE_MINT, BASE_VAULT, QUOTE_VAULT = key(1), key(2), key(3), key(4)


def pool_bytes(base_vault=BASE_VAULT, quote_vault=QUOTE_VAULT, quote_mint=ps.WSOL, virtual=20_000_000_000, n=301):
    b = (ps.POOL_DISCRIMINATOR + bytes([255]) + struct.pack("<H", 0) + bytes(Pubkey.from_string(ps.pool_authority(BASE_MINT)))
         + bytes(Pubkey.from_string(BASE_MINT)) + bytes(Pubkey.from_string(quote_mint)) + bytes(32)
         + bytes(Pubkey.from_string(base_vault)) + bytes(Pubkey.from_string(quote_vault)) + struct.pack("<Q", 1)
         + bytes(Pubkey.from_string(key(9))) + b"\0\0" + virtual.to_bytes(16, "little", signed=True)
         + struct.pack("<Q", 0) + b"\0" + b"\0")
    return b + b"\0" * (n - len(b))


def token_account(mint, authority, amount, state=1):
    return (bytes(Pubkey.from_string(mint)) + bytes(Pubkey.from_string(authority)) + struct.pack("<Q", amount)
            + b"\0" * 36 + bytes([state]) + b"\0" * 12 + b"\0" * 8 + b"\0" * 36)


MINT = base64.b64decode(GOLDEN["mintDecodeVectors"][0]["dataBase64"])           # 10^13 atoms, 6 decimals


def chain(**over):
    accounts = {POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes()),
                BASE_VAULT: (ps.TOKEN_PROGRAM, token_account(BASE_MINT, POOL, 800_000_000_000_000)),
                QUOTE_VAULT: (ps.TOKEN_PROGRAM, token_account(ps.WSOL, POOL, 60_000_000_000)),
                BASE_MINT: (ps.TOKEN_PROGRAM, MINT),
                q.global_config_address(): (ps.PUMP_AMM_PROGRAM, GC), q.fee_config_address(): (q.FEE_PROGRAM, FC)}
    accounts.update(over)

    def fetch(url, keys):
        return 1000, [accounts.get(k) for k in keys]
    return fetch


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def quoter(fetch, ref=1000, clock=None):
    return q.Quoter(lambda: "https://rpc.example/?api-key=SECRET", lambda: ref, fetch, clock or Clock())


def expected(side, amount):
    g, f = q.decode_global_config(GC), q.decode_fee_config(FC)
    eff = 60_000_000_000 + 20_000_000_000
    fees = ps.fees_bps(g, f, ps.pool_authority(BASE_MINT), BASE_MINT, 10 ** 13, 800_000_000_000_000, eff)
    fn = ps.buy_quote_input if side == "buy" else ps.sell_base_input
    out = fn(amount, 0, 800_000_000_000_000, 60_000_000_000, fees, 20_000_000_000, key(9))
    return out["base"] if side == "buy" else out["uiQuote"], fees


def test_the_real_config_accounts_decode():
    e = CONFIGS["expected"]
    assert q.decode_global_config(GC) == e["global"]
    f = q.decode_fee_config(FC)
    assert len(f["tiers"]) == e["fee_tiers"] and f["tiers"][0] == e["first_tier"] and f["flat"] == e["flat"]
    assert [a["address"] for a in CONFIGS["accounts"]] == [q.global_config_address(), q.fee_config_address()]


@pytest.mark.parametrize("side,amount", [("buy", 250_000_000), ("sell", 5_000_000_000_000)])
def test_a_quote_is_the_sdk_math_on_one_coherent_read(side, amount):
    rec = quoter(chain()).quote(POOL, side, amount)
    out, fees = expected(side, amount)
    assert rec["reason"] == "ok" and rec["output"] == str(out) and rec["fees_bps"] == fees
    assert rec["effective_quote"] == str(80_000_000_000) and rec["context_slot"] == 1000
    assert rec["host"] == "rpc.example" and "SECRET" not in json.dumps(rec)        # the key never recorded


@pytest.mark.parametrize("over,reason", [
    ({BASE_VAULT: None}, "missing_account"),
    ({POOL: (ps.TOKEN_PROGRAM, pool_bytes())}, "wrong_owner"),
    ({POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes()[:250])}, "unsupported_layout"),
    ({BASE_VAULT: (ps.TOKEN_PROGRAM, token_account(BASE_MINT, key(7), 10 ** 15))}, "vault_mismatch"),
    ({QUOTE_VAULT: (ps.TOKEN_PROGRAM, token_account(ps.WSOL, POOL, 6 * 10 ** 10, state=2))}, "vault_frozen"),
    ({BASE_MINT: (ps.TOKEN_2022_PROGRAM, MINT[:82] + b"\0" * 83 + b"\x01" + struct.pack("<HH", 1, 8) + b"\0" * 8)},
     "mint_extensions"),
])
def test_every_refusal_has_a_reason(over, reason):
    first = chain()
    qt = quoter(first)
    qt.quote(POOL, "sell", 10 ** 9)                      # discovered against the good chain
    qt.fetch = chain(**over)
    assert qt.quote(POOL, "sell", 10 ** 9)["reason"] == reason


def test_a_pool_that_now_points_elsewhere_is_rediscovered():
    qt = quoter(chain())
    qt.quote(POOL, "sell", 10 ** 9)
    qt.fetch = chain(**{POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes(base_vault=key(7)))})
    assert qt.quote(POOL, "sell", 10 ** 9)["reason"] == "pool_changed" and POOL not in qt.static


def test_non_sol_pools_disabled_trading_stale_state_slow_reads_and_rpc_errors():
    assert quoter(chain(**{POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes(quote_mint=ps.USDC))})).quote(POOL, "buy", 10 ** 8)[
        "reason"] == "non_sol_quote"
    gc = bytearray(GC)
    gc[56] = 0b10000                                     # sells disabled
    fetch = chain(**{q.global_config_address(): (ps.PUMP_AMM_PROGRAM, bytes(gc))})
    assert quoter(fetch).quote(POOL, "sell", 10 ** 9)["reason"] == "trading_disabled"
    assert quoter(fetch).quote(POOL, "buy", 10 ** 8)["reason"] == "ok"
    assert quoter(chain(), ref=1003).quote(POOL, "sell", 10 ** 9)["reason"] == "stale_state"     # 3 slots behind
    clock, good = Clock(), chain()

    def slow(url, keys):
        clock.t += 6
        return good(url, keys)
    assert quoter(slow, clock=clock).quote(POOL, "sell", 10 ** 9)["reason"] == "slow_response"

    def down(url, keys):
        raise ConnectionError("refused")
    assert quoter(down).quote(POOL, "sell", 10 ** 9)["reason"] == "rpc_error"
    assert quoter(chain()).quote(POOL, "sell", 10 ** 19)["reason"] == "insufficient_liquidity"   # the real vault
    assert quoter(chain()).quote(POOL, "sell", 2 ** 64)["reason"] == "quote_error"     # beyond a u64: refused


# --------------------------------------------------------------------------- the job book
def book(tmp_path, fetch, clock):
    return q.QuoteBook(tmp_path / "quotes.db", quoter(fetch, clock=clock), clock=clock)


def test_an_entry_quote_schedules_its_exits_sized_by_the_tokens_it_bought(tmp_path):
    clock = Clock(1000)
    b = book(tmp_path, chain(), clock)
    meta = {"follow": "F", "rule": "r", "delay": 60, "control": False, "holds": {"hold 1 h": 3600}}
    assert b.request("F|entry", "entry", POOL, "buy", 250_000_000, 1000, 1060, meta)
    assert not b.request("F|entry", "entry", POOL, "buy", 250_000_000, 1000, 1060, meta)   # idempotent
    assert b.run() == 1
    entry = json.loads(b.db.execute("SELECT result FROM jobs WHERE id = 'F|entry'").fetchone()[0])
    due, amount = b.db.execute("SELECT due, amount FROM jobs WHERE id = 'F|hold 1 h'").fetchone()
    assert due == 1000 + 3600 and amount == entry["output"]
    clock.t = 4600
    b.run()
    v = b.view(max_age_s=0)
    assert v["coverage"]["entry / signal"]["ok"] == 1 and v["coverage"]["exit / signal"]["first_try_ok"] == 1
    row = v["quote_pnl"][0]
    assert row["exit"] == "hold 1 h" and row["n"] == 1 and -6 < row["mean_pct"] < 0      # the round trip's costs


def test_a_failing_exit_retries_every_thirty_seconds_then_is_unmeasured_with_its_reason(tmp_path):
    clock = Clock(1000)

    def down(url, keys):
        raise TimeoutError("read timed out")
    b = book(tmp_path, down, clock)
    b.request("F|hold 1 h", "exit", POOL, "sell", 10 ** 9, 1000, 1000 + q.RETRY_S, {"follow": "F", "exit": "hold 1 h"})
    tries = 0
    while clock.t <= 1000 + q.RETRY_S + 60:
        tries += b.run()
        clock.t += 10
    state, n, result = b.db.execute("SELECT state, tries, result FROM jobs").fetchone()
    assert state == "unmeasured" and json.loads(result)["reason"] == "timeout"
    assert n == tries == q.RETRY_S // q.RETRY_EVERY_S + 1                  # every 30 s within the window, no more
    assert b.view(max_age_s=0)["coverage"]["exit / signal"]["unmeasured: timeout"] == 1


def test_a_job_found_past_its_deadline_is_missed_not_quoted_late_and_restarts_resume(tmp_path):
    clock = Clock(5000)
    b = book(tmp_path, chain(), clock)
    b.request("G|entry", "entry", POOL, "buy", 10 ** 8, 1000, 1060, {"follow": "G", "control": True})
    b.request("H|entry", "entry", POOL, "buy", 10 ** 8, 4990, 5050, {"follow": "H", "control": True})
    again = book(tmp_path, chain(), clock)                 # a restart: the same database
    assert again.run() == 2
    states = dict(again.db.execute("SELECT id, state FROM jobs").fetchall())
    assert states == {"G|entry": "skipped", "H|entry": "ok"}
    assert again.view(max_age_s=0)["coverage"]["entry / control"]["skipped: missed"] == 1


def test_the_revival_forward_test_requests_quotes_for_both_arms(tmp_path):
    from meme_trader.sniper.revival import DELAYS, ORDER_SOL, Revival
    (tmp_path / "wallets").mkdir()
    rv = Revival(tmp_path)
    rv.quotes = book(tmp_path, chain(), Clock(1000))
    rv._open(POOL, "+40% in 5 min, volume x4", 900.0, 1000.0, 0.5, 5.0, control=False)
    rv._open(POOL, "+40% in 5 min, volume x4", 900.0, 1000.0, None, None, control=True)
    rows = rv.quotes.db.execute("SELECT id, due, amount, meta FROM jobs ORDER BY id").fetchall()
    assert len(rows) == 2 * len(DELAYS) and all(r[2] == str(int(ORDER_SOL * 1e9)) for r in rows)
    metas = [json.loads(r[3]) for r in rows]
    assert {m["control"] for m in metas} == {False, True} and set(metas[0]["holds"]) == {"hold 1 h", "hold 2 h"}
    assert sorted({r[1] for r in rows}) == [1000.0 + d for d in DELAYS]


def test_the_review_package_carries_coverage_and_every_attempt(tmp_path):
    from meme_trader.sniper.review_export import Export, Pseudo
    clock = Clock(1000)
    b = book(tmp_path, chain(), clock)
    b.request("F|entry", "entry", POOL, "buy", 250_000_000, 1000, 1060,
              {"follow": "F", "control": False, "holds": {"hold 1 h": 3600}})
    b.run()
    out = tmp_path / "out"
    out.mkdir()
    Export(tmp_path, out, Pseudo(b"test")).quotes()
    summary = json.loads((out / "quotes_summary.json").read_text())
    rows = [json.loads(x) for x in (out / "quote_attempts.jsonl").read_text().splitlines()]
    assert summary["coverage"]["entry / signal"]["ok"] == 1 and summary["pending"] == 1      # its exit, scheduled
    assert len(rows) == 1 and rows[0]["reason"] == "ok" and "SECRET" not in json.dumps(rows)


def test_unqualified_state_and_outputs_are_reason_coded_and_permanent_refusals_arent_retried(tmp_path):
    over = {QUOTE_VAULT: (ps.TOKEN_PROGRAM, token_account(ps.WSOL, POOL, 6 * 10 ** 10)),
            POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes(virtual=1 << 80))}
    assert quoter(chain(**over)).quote(POOL, "sell", 10 ** 9)["reason"] == "invalid_state"       # E beyond u64
    assert quoter(chain()).quote(POOL, "buy", 1)["reason"] == "unexecutable_output"              # -20 atoms raw
    tail = quoter(chain(**{POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes()[:279] + b"\x07" + pool_bytes()[280:])}))
    rec = tail.quote(POOL, "sell", 10 ** 9)
    assert rec["reason"] == "ok" and rec["qualified"] is False and len(rec["tail_sha256"]) == 64
    clock = Clock(1000)
    b = book(tmp_path, chain(**{POOL: (ps.PUMP_AMM_PROGRAM, pool_bytes(quote_mint=ps.USDC))}), clock)
    b.request("U|entry", "entry", POOL, "buy", 10 ** 8, 1000, 1060, {"follow": "U"})
    b.run()
    assert b.db.execute("SELECT state, tries FROM jobs").fetchone() == ("skipped", 1)       # final at once
