"""Wallet study: PumpSwap decoding, the recorder's bookkeeping, and the study end to end on a synthetic market."""
import json
import shutil
import time

import pytest

from meme_trader import config
from meme_trader.wallets import study
from meme_trader.wallets.pumpswap import AMM_PROGRAM, WSOL, Swap, decode_event, parse_logs
from meme_trader.wallets.recorder import Recorder
from meme_trader.wallets.sources import gt_trade_row

# real events captured from PumpSwap on 2026-10-04
BUY_B64 = ("Z/RSHyz1d3f88sFqAAAAAMkkBAAAAAAAOQEBAAAAAADoNAsAAAAAADkBAQAAAAAAvJLo5cMHAACG0xB73AEAADkBAQAAAAAAFAAAAAAAAACE"
           "AAAAAAAAAAUAAAAAAAAAIQAAAAAAAAD3AAEAAAAAAHMAAQAAAAAAJYdTweZsXDiz2WoXsB/fuwEuz0N/WIMaF2SBArQYzQdbGiVd/JKT+S1T"
           "wFGrMLXqg21PJFJrFdq7W0eZ0fYQvCOeUb0RprNo+bpBqh00cRur4/O8YdTL+fVfSUCZfdfZ/MhjvZraGEw+0BwDQ2zs6cu37bXg0jQKZzJ+"
           "TDPoF/RKwvjQ3Vy8l+MonBl8tQYqVPPZVrnOblEV+WVnqlyz5nfZFZVfiIBzHOtKdaDMlsF0+kCVxOHZlnrPxChFrmeu7p9kr6ITxrKthZlQ"
           "6M5ItLvjNPvaPPlXi/yBCdBxgiwFAAAAAAAAACEAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAvu8DAAAAAAASAAAA"
           "YnV5X2V4YWN0X3F1b3RlX2luAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAEAAAAAAAAADIQR4YBAAAAAAAAAAAAAAAAXoIXlhrjQMAAAAAAAAA"
           "AAAAAAAAAAAAAAAAAAAAAAAA")
SELL_B64 = ("Pi83CqUD3Cr88sFqAAAAAPg5rKoxAAAAYIcqRQAAAAA/zoM33wsAAAAAAAAAAAAAwn0OAvM0AABscUXHSgAAAAZ+tkkAAAAAFAAAAAAAAACy"
            "vSUAAAAAAAUAAAAAAAAAbW8JAAAAAABUwJBJAAAAAI7J+UgAAAAA1uGNTpWDQhB9iriWYii2Fj2EsqBxdGzKqSUj2TU/uA57KYvMCaBWLjaa"
            "MWBV60UYVYkd+08TyZJPrV97cQyche9U0+hCLIzSJ09Y8uWqDZjDQAKKmApuSBvgNn/LDf1wgEXVva0EKD+aMf/zIwagEhHVmXB6frfNoqgP"
            "kmz6WQJKwvjQ3Vy8l+MonBl8tQYqVPPZVrnOblEV+WVnqlyz5nfZFZVfiIBzHOtKdaDMlsF0+kCVxOHZlnrPxChFrmeuq2eR5WTVz3Xys2fL"
            "EkYJ3kLMQYQ4ti/tl/RgECubRFVLAAAAAAAAAFmHjQAAAAAAAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAtrcEAAAAAADIQR4YBAAAAAAAAAAA"
            "AAAAAdt14i1NfQMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
DAY = 86400.0


def test_decode_real_events():
    import base64

    b = decode_event(base64.b64decode(BUY_B64))
    assert b.side == "buy" and b.pool == "3XVmbSbner59muekttRdBtXbwt317BPVLb6Md5NoAQsG"
    assert b.user == "78dDzwcncVUUN7pqCi9RKh59DV3jrbJX44JV2e15dU2P" and b.ts == 1791095548
    assert (b.base, b.quote, b.pool_base, b.pool_quote) == (271561, 65651, 8537957241532, 2046469133190)
    s = decode_event(base64.b64decode(SELL_B64))
    assert s.side == "sell" and s.quote == 1224329614 and s.pool == "FToj9QMPShqrU9iHxvhe5snETb7EAHvFRL1bXKqH9XK3"
    assert decode_event(b"\x00" * 400) is None and decode_event(base64.b64decode(BUY_B64)[:300]) is None


def test_parse_logs_only_takes_the_amms_own_events():
    logs = ["Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
            f"Program {AMM_PROGRAM} invoke [2]", "Program log: Instruction: Buy", f"Program data: {BUY_B64}",
            f"Program {AMM_PROGRAM} consumed 50000 of 200000 compute units", f"Program {AMM_PROGRAM} success",
            f"Program data: {SELL_B64}",                       # emitted by the router, not the AMM: ignored
            "Program data: !!notbase64", "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success"]
    out = parse_logs(logs)
    assert [x.side for x in out] == ["buy"]


def test_gt_trade_row():
    d = {"id": "solana_1_sig_0_1", "attributes": {
        "block_timestamp": "2026-10-04T06:45:09Z", "tx_hash": "sig", "tx_from_address": "W", "kind": "sell",
        "from_token_address": "Mpump", "to_token_address": WSOL, "from_token_amount": "910.5", "to_token_amount": "0.16",
        "price_from_in_currency_token": "0.00018", "volume_in_usd": "19.8"}}
    r = gt_trade_row("P", d)
    assert r["side"] == "sell" and r["mint"] == "Mpump" and r["sol"] == 0.16 and r["px"] == 0.00018 and r["wallet"] == "W"
    d["attributes"]["to_token_address"] = "USDC"
    assert gt_trade_row("P", d) is None                                  # not quoted in SOL


def test_sol_price_from_a_sol_pair():
    from meme_trader.wallets.sources import _sol_usd

    assert _sol_usd({"priceUsd": "0.0242", "priceNative": "0.0002"}) == pytest.approx(121)
    assert _sol_usd({"priceUsd": "1", "priceNative": "0"}) is None and _sol_usd({}) is None


def _cfg(**kw):
    c = config.load(config.EXAMPLE).wallets
    c.update(kw)
    return c


def test_recorder_eligibility_and_polling(tmp_path):
    now = time.time()
    r = Recorder(_cfg(), tmp_path)
    meta = {"dex": "pumpswap", "quote": WSOL, "mint": "Xpump", "created": now - 20 * DAY, "symbol": "X"}
    r.pools = {"P": {"meta": dict(meta), "liq": [[now, 50000]], "last_seen": now},
               "young": {"meta": dict(meta, created=now - 3600), "liq": [[now, 50000]], "last_seen": now},
               "thin": {"meta": dict(meta), "liq": [[now, 100]], "last_seen": now},
               "notpump": {"meta": dict(meta, mint="Xabc"), "liq": [[now, 50000]], "last_seen": now},
               "quiet": {"meta": dict(meta), "liq": [[now, 50000]], "last_seen": now - 30 * DAY}}
    assert [p for p, v in r.pools.items() if r.eligible(v, now)] == ["P"]

    def rows(ts):
        return [{"id": f"i{t}", "t": float(t), "pool": "P"} for t in ts]
    new = r.absorb("P", rows(range(1000, 1300)), now, 300)               # first poll: 300 trades over 300 s
    assert len(new) == 300 and r.pools["P"]["wm"] == 1299
    assert r.pools["P"]["next_poll"] - now == pytest.approx(250, rel=0.01)   # ~1 trade/s -> 250 s for 250 trades
    again = r.absorb("P", rows(range(1250, 1310)), now + 60, 60)
    assert [x["t"] for x in again] == [float(t) for t in range(1300, 1310)] and r.st["gaps"] == 0
    r.absorb("P", rows(range(2000, 2300)), now + 120, 300)               # overflowed: 1310..2000 missed
    assert r.st["gaps"] == 1 and r.status(now)["coverage_est"] < 1
    sw = Swap(ts=5, pool="P", user="U", side="buy", base=2_000_000, quote=1_000_000_000, pool_base=10**14, pool_quote=10**11)
    row = r.swap_row(sw, "sig", 0)
    assert row["px"] == 0.5 and row["sol"] == 1.0 and row["mint"] == "Xpump" and row["usd"] == 0.0
    r.sol_usd = 120.0
    assert r.swap_row(sw, "sig", 0)["usd"] == 120.0
    assert r.swap_row(Swap(5, "P", "U", "buy", 1, 1, 10, 10**12), "s", 0) is None    # SOL as base: skipped


# ---------------------------------------------------------------- the study on a synthetic market
POL = study.load_policy("wallets-v1")


def _row(pool, t, w, side, sol, px, k=[0]):
    k[0] += 1
    return {"id": f"r{k[0]}", "t": t, "pool": pool, "mint": pool + "pump", "wallet": w, "side": side, "sol": sol,
            "tokens": sol / px, "px": px, "usd": sol * 150, "sig": f"s{k[0]}", "sym": pool}


def _runner(pool, t0, insiders, crowd=12):
    """Quiet trading at 1.0, insiders buy early at ~1.0, then a crowd pushes it to 4x; insiders sell at 3.5x."""
    out = [_row(pool, t0 + i * 60, f"bg{i % 3}", "buy" if i % 2 else "sell", 0.3, 1.0) for i in range(10)]
    for j, w in enumerate(insiders):
        out.append(_row(pool, t0 + 700 + j, w, "buy", 1.0, 1.02))
    for i in range(crowd):
        out.append(_row(pool, t0 + 900 + i * 60, f"crowd{pool}{i}", "buy", 0.5, 1.2 + i * 0.25))
    for j, w in enumerate(insiders):
        out.append(_row(pool, t0 + 900 + crowd * 60 + j, w, "sell", 3.0, 3.5))
    return out


def _write(tmp_path, rows):
    d = tmp_path / "wallets"
    d.mkdir(exist_ok=True)
    with (d / "trades-2026-10-01.jsonl").open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return d


def test_selection_finds_repeat_early_wallets_and_skips_one_hit_wonders_and_bots(tmp_path):
    t0 = 1_790_000_000.0
    rows = (_runner("A", t0, ["ins1", "ins2", "lucky"]) + _runner("B", t0 + 2 * DAY, ["ins1", "ins2"])
            + _runner("C", t0 + 4 * DAY, ["ins1", "bot"]) + _runner("D", t0 + 5 * DAY, ["bot"]))
    rows += [_row("E", t0 + i, "bot", "buy", 0.01, 1.0) for i in range(3100)]          # a bot: 3000+ trades
    d = study.load_data(study.trade_files(_write(tmp_path, rows)), min_sol=0.05)
    res = study.select_wallets(d, POL["selection"])
    assert res["moves"] == 4
    got = {w["wallet"]: w for w in res["wallets"]}
    assert set(got) == {"ins1", "ins2"} and got["ins1"]["hits"] == 3 and got["ins1"]["good_exits"] >= 1
    assert res["wallets"][0]["wallet"] == "ins1"
    assert "bot" not in res["control"] and "ins1" in res["control"]


def test_simulate_take_profit_trail_stop_and_costs():
    tr = dict(POL["trade"], fee_pct=0.0, tx_cost_sol=0.0)
    path = [(0.0, "w", True, 1, 1.0), (60.0, "w", True, 1, 1.0), (100.0, "w", True, 1, 2.2), (200.0, "w", True, 1, 3.0),
            (300.0, "w", True, 1, 2.0)]
    r = study.simulate(path, 0.0, tr, None, 10 * DAY)
    assert r["exit"] == "trail" and r["gross_x"] == pytest.approx(0.5 * 2.0 + 0.5 * 2.0)
    stop = study.simulate([(0.0, "w", True, 1, 1.0), (60.0, "w", True, 1, 1.0), (90.0, "w", True, 1, 0.4)], 0.0, tr, None, 10 * DAY)
    assert stop["exit"] == "stop" and stop["net"] == pytest.approx(-0.6)
    costly = study.simulate(path, 0.0, POL["trade"], 50.0, 10 * DAY)
    assert costly["net"] < r["net"]                                       # fees, impact, transactions
    assert study.simulate(path[:2], 0.0, tr, None, 100.0)["exit"] == "open"
    assert study.simulate(path, 400.0, tr, None, 10 * DAY) is None        # no trade after the signal


def test_survived_gate_uses_only_complete_days_before_the_signal():
    c = [[0, 1, 4, 1, 4, 0], [DAY, 4, 4, 1.5, 2, 0], [2 * DAY, 2, 2, 2, 2, 0]]
    assert study.survived(c, 2 * DAY + 10, 50) is True                     # fell 4 -> 1.5 on day 2
    assert study.survived(c, DAY + 10, 50) is False                        # day 2 not complete yet
    assert study.survived(None, DAY, 50) is None


def test_register_freeze_eval_end_to_end(tmp_path, monkeypatch):
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir()
    shutil.copy(study.POLICY_DIR / "wallets-v1.yaml", pol_dir / "wallets-v1.yaml")
    pol = study.load_policy("wallets-v1", pol_dir)
    monkeypatch.setattr(study, "RESEARCH", tmp_path / "research")
    with pytest.raises(study.StudyError, match="isn't registered"):
        study.cmd_select(pol, tmp_path)
    lock = study.register(pol, now=1_790_000_000)
    assert study.register(pol)["registered_at"] == lock["registered_at"]  # idempotent

    t0 = 1_790_000_000.0
    sel = pol["selection"]
    insiders = [f"ins{i}" for i in range(12)]
    rows = []
    for k in range(4):                                                    # period A: 4 runners, same 12 early wallets
        rows += _runner(f"R{k}", t0 + k * 3 * DAY, insiders)
    rows.append(_row("R0", t0 + 15 * DAY, "bg0", "buy", 0.3, 1.0))        # period A spans 15 days
    data_dir = _write(tmp_path, rows)
    res = study.cmd_select(pol, data_dir)
    assert res["qualified"] == 12 and res["days"] >= sel["period_days"]
    study.cmd_freeze(pol, data_dir, now=t0 + 15 * DAY + 1)
    with pytest.raises(study.StudyError, match="was frozen"):
        study.cmd_freeze(pol, data_dir, now=t0 + 15 * DAY + 2)
    lock = study.read_lock(pol)
    assert len(lock["wallets"]) == 12 and lock["frozen_at"]

    # period B: two listed wallets buy an old coin S, which then doubles and fades; a random coin Q stays flat
    tb = t0 + 16 * DAY
    b_rows = [_row("S", tb - 3600 + i * 60, f"fresh{i}", "buy", 0.3, 1.0) for i in range(5)]
    b_rows += [_row("S", tb, "ins0", "buy", 1.0, 1.0), _row("S", tb + 600, "ins1", "buy", 1.0, 1.0)]
    b_rows += [_row("S", tb + 900 + i * 600, f"x{i}", "buy", 0.5, p) for i, p in enumerate([1.1, 1.6, 2.1, 2.6, 1.7])]
    b_rows += [_row("Q", tb - 1800 + i * 600, f"q{i}", "sell" if i == 5 else "buy", 0.5, 1.0) for i in range(30)]
    b_rows.append(_row("S", t0 + 32 * DAY, "late", "buy", 0.3, 1.7))
    _write(tmp_path, b_rows)
    pools = {p: {"meta": {"created": t0 - 30 * DAY, "mint": p + "pump"}, "liq": [[tb - DAY, 80000]]} for p in ("S", "Q")}
    (data_dir / "pools.json").write_text(json.dumps(pools))
    (data_dir / "candles").mkdir()
    for p in ("S", "Q"):
        (data_dir / "candles" / f"{p}.json").write_text(json.dumps({"daily": [[t0 - 20 * DAY, 1, 5, 1, 4, 0], [t0 - 19 * DAY, 4, 4, 1, 1, 0]]}))
    rep = study.cmd_eval(pol, data_dir, quick=True)
    assert rep["signals"] == 1 and rep["trades"] == 1
    t = rep["trade_list"][0]
    assert t["exit"] == "trail" and t["net"] > 0.5                         # half at 2x, rest trailed out at 1.7x
    assert rep["random_coin_mean"] < 0.01 and rep["beats_random_coin_pct"] == 100
    assert rep["verdict"].startswith("not enough data")                   # 1 trade < 30

    (pol_dir / "wallets-v1.yaml").write_text((pol_dir / "wallets-v1.yaml").read_text().replace("runner_x: 3.0", "runner_x: 2.0"))
    with pytest.raises(study.StudyError, match="changed since registration"):
        study.cmd_eval(study.load_policy("wallets-v1", pol_dir), data_dir)
