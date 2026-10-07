"""The reviewer package exporter, on synthetic data only: every row accounted for, cohorts kept apart, wallets
pseudonymized consistently (coins stay public), and a secret anywhere fails the export."""
import csv
import gzip
import hashlib
import json

import pytest

from meme_trader.sniper import review_export as rx
from meme_trader.sniper.revival import Store

OWNER = "OwnerWa11etAddressXXXXXXXXXXXXXXXXXXXXXXXXX"[:44]
LEADER = "LeaderWa11etAddressYYYYYYYYYYYYYYYYYYYYYYYY"[:44]
COIN = "CoinAddressZZZZZZZZZZZZZZZZZZZZZZZZZZZZpump"
POOL = "PoolAddressQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"[:44]
DAY = 1791244800.0                                     # 2026-10-06 00:00 UTC


def jl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    base = {"symbol": "C", "cost": 0.1, "proceeds": 0.09, "pnl": -0.01, "pnl_pct": -10, "peak_gain_pct": 5,
            "score": 70, "initials": False, "exit": "stop", "desk": "", "p": None, "mae_pct": -12}
    jl(d / "trades-2026-10-05.jsonl", [
        {**base, "mint": COIN, "opened": DAY - 5000, "closed": DAY - 4000, "source": "late", "mode": "paper",
         "session": "s1", "config": "c", "model": ""},
        {**base, "mint": COIN, "opened": DAY - 3000, "closed": DAY - 2000, "source": "copy:x", "mode": "paper",
         "session": "s1", "leader": LEADER},
        {**base, "mint": COIN, "opened": DAY - 1000, "closed": DAY - 900, "source": "late", "mode": "paper-synthetic"},
        {**base, "mint": COIN, "opened": DAY - 800, "closed": DAY - 700, "source": "sniper"},       # no mode yet
    ])
    with open(d / "trades-2026-10-05.jsonl", "a") as f:
        f.write('{"mint": "unfinished')                 # a truncated last line: counted, not fatal
    jl(d / "journal-2026-10-07.jsonl", [                # a day with no trades
        {"ts": DAY + 86400 + 10, "agent": "sniper", "event": "buy", "mint": COIN,
         "text": f"copied {LEADER} into {COIN}; owner {OWNER}"}])
    (d / "lab").mkdir()
    (d / "lab" / "experiments.json").write_text(json.dumps([{"id": "e1", "key": "late.x", "value": 2, "now": 1,
                                                             "status": "done"}]))
    (d / "lab" / "e1.result.json").write_text(json.dumps({"verdict": "no clear difference", "per_day": []}))
    prov = d / "research" / "exploratory"
    (prov / "outputs").mkdir(parents=True)
    (prov / "provenance").mkdir()
    (prov / "provenance" / "t8-compare-wt-cr.commit").write_text("abc123\n")
    (prov / "outputs" / "edge2-compare.out").write_text(
        "Traceback ...\n  [1/40] base         feed-2026-10-03        P&L -0.021  n=79  (403s)\n"
        "  [2/40] fill0.5s     feed-2026-10-03        P&L +2.272  n=90  (1199s)\n")
    st = Store(d / "revival-v2.db")
    st.commit([{"id": "F1", "exit": "hold 1 h", "pnl_pct": 3.0, "censored": False, "signal_t": DAY, "pool": POOL,
                "mint": COIN, "rule": "R", "delay": 5, "control": False}],
              {"open": {POOL: [{"id": "F2", "pool": POOL, "mint": COIN, "rule": "R", "delay": 60, "signal_t": DAY,
                                "exits": {"hold 1 h": {}, "hold 2 h": {}}}]}, "saved": DAY})
    jl(d / "exit_lab.jsonl", [{"mint": COIN, "kind": "pick", "pnl_pct": -9}])
    (d / "model.json").write_text(json.dumps({"features": ["a"], "feature_version": 2, "w": [0], "b": 0}))
    with gzip.open(d / "feed-2026-10-06.jsonl.gz", "wt") as f:
        for i, (ts, ei) in enumerate([(DAY + 1, 0), (DAY + 2, 0), (DAY + 200, 1)]):
            f.write(json.dumps({"kind": "trade", "ts": ts, "chain_ts": ts - 1.5, "signature": "S", "event_index": ei,
                                "trader": LEADER, "mint": COIN}) + "\n")      # the second one is a duplicate
        f.write(json.dumps({"kind": "health", "ts": DAY + 60, "lag_s": 1.2, "gap_pct": 0.4, "degraded": "",
                            "host": "rpc.example"}) + "\n")
    root = tmp_path / "root"
    (root / "research").mkdir(parents=True)
    (root / ".env").write_text("SOLANA_WS_URL=wss://rpc.example/?api_key=SUPERSECRETVALUE123\n")
    (root / "research" / "hypotheses.csv").write_text("id,hypothesis\nT1,x\n")
    return d, root


def export(data, root, tmp_path, **kw):
    out = tmp_path / "pkg"
    conf = {"wallet": {"pubkey": OWNER}, "wallets": ["a"], "sniper": {"desk": {"base_url": "http://x"}, "late": {"a": 1},
                                                                      "hq": {"extra_services": ["private-unit"]}}}
    return rx.run(data, out, scan_feeds=True, root=root, owner_wallet=OWNER, config=conf, **kw), out


def test_every_row_is_exported_and_cohorts_stay_apart(data, tmp_path):
    d, root = data
    man, out = export(d, root, tmp_path)
    rows = list(csv.DictReader(open(out / "paper_trades.csv")))
    assert len(rows) == 4 and man["counts"]["paper_trades.csv"]["malformed_input_lines"] == 1
    by = {r["source"]: r["interval"] for r in rows if r["mode"] != "paper-synthetic"}
    assert by["late"].startswith("brief cohort") and by["sniper"].startswith("untagged")
    assert sum(r["interval"].startswith("synthetic") for r in rows) == 1
    assert len({r["trade_id"] for r in rows}) == 4
    out_rows = [json.loads(x) for x in open(out / "revival_outcomes.jsonl")]
    assert [r["status"] for r in out_rows] == ["closed", "open", "open"]
    assert {json.loads(x)["signal_id"] for x in open(out / "revival_signals.jsonl")} == {"F1", "F2"}
    t8 = [json.loads(x) for x in open(out / "graduation_latency_runs.jsonl") if "t8" in x]
    assert [(r["variant"], r["pnl_sol"], r["trades"]) for r in t8] == [("base", -0.021, 79), ("fill0.5s", 2.272, 90)]
    q = {r["day_utc"]: r for r in csv.DictReader(open(out / "day_quality.csv"))}
    assert q["2026-10-06"]["repeat_same_slot"] == "1" and q["2026-10-06"]["lag_p50_s"] == "1.5"
    assert q["2026-10-06"]["gaps_over_60s"] == "1" and "2026-10-07" in q        # a day with no trades is listed
    assert man["unavailable"] and (out / "README_REVIEW_PACKAGE.md").exists()
    for f in man["outputs"]:
        if f["file"] != "manifest.json":
            assert hashlib.sha256((out / f["file"]).read_bytes()).hexdigest() == f["sha256"]
    states = {i["file"]: i["state"] for i in man["inputs"]}
    assert states["feed-2026-10-06.jsonl.gz"] == "sealed" and states["revival-v2.db"] == "growing"


def test_wallets_are_pseudonyms_and_coins_stay_public(data, tmp_path):
    d, root = data
    _, out = export(d, root, tmp_path)
    blob = "".join(p.read_text(errors="replace") for p in out.rglob("*") if p.is_file())
    assert OWNER not in blob and LEADER not in blob and "OWNER_WALLET" in blob and COIN in blob
    trade = next(r for r in csv.DictReader(open(out / "paper_trades.csv")) if r["leader"])
    note = json.loads(open(out / "paper_orders.jsonl").readline())
    assert trade["leader"].startswith("w_") and trade["leader"] in note["text"]     # the same pseudonym everywhere
    conf = json.loads((out / "config_effective.json").read_text())
    assert "wallet" not in conf and "wallets" not in conf and "base_url" not in conf["sniper"]["desk"]
    assert "private-unit" not in json.dumps(conf)                      # this machine's service names stay home


def test_a_secret_anywhere_fails_the_export_and_keeps_nothing(data, tmp_path):
    d, root = data
    with open(d / "journal-2026-10-07.jsonl", "a") as f:
        f.write(json.dumps({"ts": DAY + 86400 + 20, "event": "info", "text": "ws wss://rpc.example/?api_key=SUPERSECRETVALUE123"})
                + "\n")
    with pytest.raises(SystemExit, match="secret scan failed"):
        export(d, root, tmp_path)
    assert not (tmp_path / "pkg").exists()


def test_it_never_writes_into_a_used_directory(data, tmp_path):
    d, root = data
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x").write_text("1")
    with pytest.raises(SystemExit, match="isn't empty"):
        export(d, root, tmp_path)


def test_the_account_ledger_is_by_close_day_and_reconciles_cash(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    rows = [{"mint": COIN, "symbol": "C", "opened": DAY - 90000, "closed": DAY - 86000, "cost": 0.1, "proceeds": 0.2,
             "pnl": 0.1, "pnl_pct": 100, "failed_fees_sol": 0.01, "source": "late", "mode": "paper"},      # schema 1
            {"mint": COIN, "symbol": "C", "opened": DAY + 100, "closed": DAY + 86500, "cost": 0.1, "proceeds": 0.05,
             "pnl": -0.05, "gross_pnl": -0.05, "pnl_schema": 2, "pnl_pct": -50, "failed_fees_sol": 0.0,
             "source": "sniper", "mode": "paper"}]
    jl(d / "trades-2026-10-05.jsonl", rows[:1])
    jl(d / "trades-2026-10-07.jsonl", rows[1:])
    (d / "sniper_state_paper.json").write_text(json.dumps({
        "book": {"sol": 9.0 + 0.09 - 0.05 - 0.2 - 0.003, "start_sol": 9.0, "deposits": [[DAY, 4.0]], "closed": rows},
        "positions": {"X": {"proceeds_sol": 0.0, "initial_cost_sol": 0.2, "rent_sol": 0.0, "failed_fees_sol": 0.0}}}))
    root = tmp_path / "root"
    root.mkdir()
    rx.run(d, tmp_path / "pkg", root=root)
    led = list(csv.DictReader(open(tmp_path / "pkg" / "account_ledger.csv")))
    assert [r["close_day_utc"] for r in led] == ["2026-10-05", "2026-10-06", "2026-10-07"]     # 10-06: nothing closed
    assert float(led[0]["net_pnl_sol"]) == pytest.approx(0.09) and float(led[0]["gross_pnl_sol"]) == pytest.approx(0.1)
    rec = json.loads((tmp_path / "pkg" / "account_reconciliation.json").read_text())
    assert rec["net_pnl_sol"] == pytest.approx(0.04) and rec["open_positions_cash_effect_sol"] == pytest.approx(-0.2)
    assert rec["residual_sol"] == pytest.approx(-0.003)             # a fee no position carries (a failed buy, say)
