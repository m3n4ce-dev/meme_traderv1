"""Research pipeline: chain facts in recordings, feed latency, non-organic (Mayhem agent) trades, recorded
feed health, and the harness that evaluates one frozen strategy (research.py)."""
import asyncio
import base64
import copy
import json
import struct
import time

import pytest

from meme_trader import config
from meme_trader.sniper import research as R
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Health, Launch, Trade, dumps, loads
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import PUMP_PROGRAM, TRADE_EVENT, Feed, FeedQuality, SolanaTradeFeed, SyntheticFeed
from meme_trader.sniper.strategy import evaluate_late_entry
from meme_trader.sniper.tracker import MAYHEM_AGENT, TokenState

P = config.load(config.EXAMPLE)
A = "A" * 40 + "pump"
DEV = "D" * 44


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine", "meme_trader.sniper.research"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine(p=None):
    p = p or copy.deepcopy(P)
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=False)


# ------------------------------------------------------------------ chain facts + latency
def trade_log(chain_ts=1_790_000_000, fee_bps=95, creator_bps=30) -> str:
    raw = (TRADE_EVENT + bytes(range(32)) + struct.pack("<QQ?", 250_000_000, 7_000_000_000_000, True)
           + bytes(range(32, 64)) + struct.pack("<qQQQQ", chain_ts, 31_000_000_000, 1_030_000_000_000_000, 0, 0)
           + bytes(32) + struct.pack("<QQ", fee_bps, 0) + bytes(32) + struct.pack("<QQ", creator_bps, 0) + bytes(60))
    return "Program data: " + base64.b64encode(raw).decode()


def test_trades_carry_slot_chain_time_and_fees():
    logs = [f"Program {PUMP_PROGRAM} invoke [1]", trade_log(), f"Program {PUMP_PROGRAM} success"]
    t, = SolanaTradeFeed.parse_logs({"err": None, "logs": logs, "signature": "s"}, 1_790_000_004.5, slot=123)
    assert (t.slot, t.chain_ts, t.fee_bps, t.creator_fee_bps) == (123, 1_790_000_000.0, 95, 30)
    assert loads(dumps(t)) == t
    old = json.loads(dumps(t))
    for k in ("slot", "chain_ts", "fee_bps", "creator_fee_bps"):
        old.pop(k)
    assert loads(json.dumps(old)).chain_ts == 0.0               # older recordings still load


def test_watchdog_drops_a_feed_that_lags_the_chain():
    q = FeedQuality(max_lag_s=5, min_lags=10)
    for i in range(20):
        q.observe(Trade(f"m{i}", 1000.0 + i, "w", "buy", 1, 1, 30, 1e9, chain_ts=990.0 + i))   # 10 s behind
    assert q.lag_s == 10.0 and q.slow
    feed = SolanaTradeFeed()
    feed.quality = q
    feed.trades_up = True
    feed.last_trade = time.time()
    q.checks.extend([True] * 500)
    assert "behind the chain" in feed.degraded_reason
    assert "behind the chain" in feed._check_stream(time.time(), time.time())
    q2 = FeedQuality(max_lag_s=5, min_lags=10)
    for i in range(20):
        q2.observe(Trade(f"m{i}", 1000.0 + i, "w", "buy", 1, 1, 30, 1e9, chain_ts=999.0 + i))
    assert not q2.slow


# ------------------------------------------------------------------ non-organic trades
def test_mayhem_agent_moves_price_but_is_not_demand():
    s = TokenState(A, Launch(A, 0, DEV, symbol="T"), 0)
    s.on_launch(s.launch)
    k = s.curve.v_sol * s.curve.v_tokens
    s.on_trade(Trade(A, 1, MAYHEM_AGENT, "buy", 5.0, 5e7, 35.0, k / 35.0, new_balance=1.05e9), 5)
    assert s.mayhem and s.curve.v_sol == 35.0 and s.peak_price == s.curve.price
    assert MAYHEM_AGENT not in s.buyers and MAYHEM_AGENT not in s.holders and not s.early_bought
    assert s.volume_sol == 0 and s.window(2, 30) == [] and s.net_flow_sol(2, 30) == 0
    assert s.market_cap_sol == pytest.approx(s.curve.price * 2e9)
    s.on_trade(Trade(A, 2, "w" * 44, "buy", 1.0, 1e7, 36.0, k / 36.0), 5)
    assert s.buyers_in(3, 30) == 1 and s.net_flow_sol(3, 30) == 1.0
    from meme_trader.sniper.features import extract

    f = extract(s, 3)
    assert f["buyers_60s"] == 1 and f["vol_60s"] == pytest.approx(1.0)


def test_non_organic_list_comes_from_config():
    p = copy.deepcopy(P)
    p.sniper.market["non_organic_wallets"] = []
    engine(p)
    assert TokenState.NON_ORGANIC == frozenset()
    engine()
    assert MAYHEM_AGENT in TokenState.NON_ORGANIC


# ------------------------------------------------------------------ recorded health
def test_health_is_recorded_and_replays_pause_entries(tmp_path):
    h = Health(10.0, "host", 0.5, 1.2, "", 151.0)
    assert loads(dumps(h)) == h
    eng = engine()

    async def go():
        await eng.handle(Health(10.0, "host", 9.0, 12.0, "12s behind the chain", 140.0))
        assert "recorded feed degraded" in eng._global_block() and eng.sol_price.usd == 140.0
        await eng.handle(Health(70.0, "host", 0.1, 1.0, "", 141.0))
        assert eng._global_block() == ""
    asyncio.run(go())


def test_live_engine_writes_health_once_a_minute(tmp_path):
    class Live(Quiet):
        realtime = True
        host, gap_pct, lag_s, degraded_reason = "rpc", 0.2, 1.4, ""

    p = copy.deepcopy(P)
    eng = Engine(p, Live(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=False,
                 record_path=tmp_path / "feed-2026-10-03.jsonl")

    async def go():
        for t in (1_791_000_000.0, 1_791_000_030.0, 1_791_000_061.0):     # 2026-10-03 UTC
            eng.now = t
            await eng._tick()
    asyncio.run(go())
    eng.record_file.flush()
    rows = [loads(x) for x in (tmp_path / "feed-2026-10-03.jsonl").read_text().splitlines()]
    hs = [r for r in rows if isinstance(r, Health)]
    assert len(hs) == 2 and hs[0].lag_s == 1.4 and hs[0].host == "rpc"


def test_window_entry_mode_skips_momentum():
    s = TokenState(A, Launch(A, 0, DEV, symbol="T"), 0)
    s.on_launch(s.launch)
    k = s.curve.v_sol * s.curve.v_tokens
    s.on_trade(Trade(A, 1, "w" * 44, "buy", 0.1, 1, 80.0, k / 80.0), 5)       # deep in the window, no momentum
    red = {"max_bundle_pct": 100, "max_early_sold_ratio": 1, "creator_launches": 0,
           "max_creator_launches_24h": 9, "max_cluster_pct": 100}
    L = copy.deepcopy(P.sniper.late)
    L["min_curve_pct"], L["max_curve_pct"] = 0, 100
    assert evaluate_late_entry(s, 2, L, red) == (False, "momentum")
    L["entry_mode"] = "window"
    assert evaluate_late_entry(s, 2, L, red)[0]


# ------------------------------------------------------------------ statistics
def test_stats_helpers():
    trades = [{"opened": 3600 * h, "pnl": p, "cost": 0.1} for h, p in enumerate([1.0, -0.1, -0.1, -0.1, 0.05, -0.05])]
    w = R.winner_dependence(trades)
    assert w["total_sol"] == pytest.approx(0.7) and w["ex_top1_sol"] == pytest.approx(-0.3)
    u = R.block_bootstrap(trades, (0, 6 * 3600), 3600, sims=500)
    assert u["blocks"] == 6 and 0 < u["p_positive"] < 1
    assert R.block_bootstrap(trades, (0, 2 * 3600), 3600) is None              # too few blocks to say anything
    assert R.breakeven_slippage([(0, 1.0), (3, 0.4), (6, -0.2)]) == pytest.approx(5.0)
    assert R.breakeven_slippage([(0, -1.0), (3, -2.0)]) == 0.0
    rb = R.random_baseline(0.95, 2, [{"pnl": x} for x in (0.5, 0.4, -0.2, -0.3)], sims=500)
    assert rb["rule_percentile"] == 1.0


# ------------------------------------------------------------------ policies, freezing, the holdout
POLICY = """name: test-v1
overrides:
  entry.enabled: false
  copy.enabled: false
  callouts.enabled: false
  late.enabled: true
  late.min_curve_pct: 20
  sizing.base_usd: 20
  sizing.max_usd: 20
gates: {min_days: 0.0001, min_trades: 0}
frozen_at: null
frozen_hash: null
"""


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "test-v1.yaml"
    path.write_text(POLICY)
    return str(path)


def recording(tmp_path, seed=3, launches=150, shift=0.0):
    evs = []

    async def go():
        async for e in SyntheticFeed(seed=seed, speed=0, launches=launches, start_ts=1_780_000_000 + shift).events():
            evs.append(e)
    asyncio.run(go())
    path = tmp_path / f"feed-{seed}-{int(shift)}.jsonl"
    path.write_text("".join(dumps(e) + "\n" for e in evs))
    return path


def test_policy_hash_covers_trading_settings_only(policy):
    pol = R.load_policy(policy)
    h = R.policy_hash(R.policy_params(pol))
    assert h == R.policy_hash(R.policy_params(pol, ["feed.max_lag_s=9", "chat.model=haiku"]))
    assert h != R.policy_hash(R.policy_params(pol, ["late.stop_loss_pct=20"]))
    with pytest.raises(config.ConfigError):
        R.load_policy("no-such-policy")


def test_prefilter_replays_exactly_like_the_full_market(tmp_path, policy):
    pol = R.load_policy(policy)
    rec = recording(tmp_path)
    full, _ = R.load_events([rec])
    small, _ = R.load_events([rec], min_progress_pct=15)
    a = R.run_variants(pol, full, [("p", pol, [])], jobs=1)["p"]["trades"]
    b = R.run_variants(pol, small, [("p", pol, [])], jobs=1)["p"]["trades"]
    assert a and a == b and len(small) < len(full)


def test_eval_report_and_registry(tmp_path, policy):
    rec = recording(tmp_path)
    rep = R.cmd_eval(policy, [str(rec)], jobs=1)
    assert rep["result"]["trades"] > 0 and "slippage" in rep and "size_curve" in rep and "timing" in rep
    assert rep["baseline_no_filter"]["trades"] >= rep["result"]["trades"]
    assert set(rep["ablations"]) == set(R.ABLATIONS)
    slip = rep["slippage"]
    assert slip["0%"] > slip["8%"]                                 # worse fills, worse results
    R.cmd_eval(policy, [str(rec)], quick=True, jobs=1)
    log = R.experiments()
    assert [e["kind"] for e in log] == ["eval", "eval"] and log[0]["hashes"] == [rep["hash"]]
    assert R.variants_tried([rec.name]) == 1


def test_freeze_then_holdout_only(tmp_path, policy, monkeypatch):
    rec_dev = recording(tmp_path, seed=3)
    with pytest.raises(config.ConfigError, match="isn't frozen"):
        R.cmd_final(policy, [str(rec_dev)], jobs=1)
    f = R.freeze(policy, now=1_780_004_000)
    assert f["frozen_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_780_004_000))
    with pytest.raises(config.ConfigError, match="already frozen"):
        R.freeze(policy)
    pol = R.load_policy(policy)
    assert pol["frozen_hash"] == f["hash"] and R.frozen_ts(pol) == 1_780_004_000
    # development data stops at the freeze
    ev, st = R.load_events([rec_dev], None, R.frozen_ts(pol))
    assert max(e.ts for e in ev) < 1_780_004_000
    # holdout: only tokens launched after the freeze; a verdict once there's enough, then it's stored
    rec_hold = recording(tmp_path, seed=4, shift=5000)
    rep = R.cmd_final(policy, [str(rec_dev), str(rec_hold)], jobs=1)
    assert rep["verdict"] in ("PASS", "FAIL") and "gates" in rep
    again = R.cmd_final(policy, [str(rec_dev), str(rec_hold)], jobs=1)
    assert again["stored"] and again["verdict"] == rep["verdict"]
    # editing a frozen policy - its settings or its pass mark - is refused
    path = pol["_path"]
    original = open(path).read()
    for old, new in (("late.min_curve_pct: 20", "late.min_curve_pct: 25"), ("min_trades: 0", "min_trades: 1")):
        open(path, "w").write(original.replace(old, new))
        with pytest.raises(config.ConfigError, match="changed after it was frozen"):
            R.cmd_eval(path, [str(rec_dev)], quick=True, jobs=1)


def test_final_shows_nothing_before_enough_holdout(tmp_path, policy, monkeypatch):
    text = open(policy).read().replace("min_days: 0.0001", "min_days: 14")
    open(policy, "w").write(text)
    R.freeze(policy, now=1_780_000_000)
    rec = recording(tmp_path, seed=4, shift=100)
    rep = R.cmd_final(policy, [str(rec)], jobs=1)
    assert rep["verdict"] is None and "no results shown" in rep["progress"] and "result" not in rep


def test_gates():
    pol = {"gates": {"min_days": 1, "min_trades": 2, "min_p_positive": 0.9, "min_breakeven_slippage_pct": 5}}
    rep = {"days": 2, "result": {"trades": 5, "net_after_ops_sol": 0.3}, "uncertainty": {"p_positive": 0.95},
           "winners": {"ex_top3_sol": 0.1}, "breakeven_slippage_pct": 6.0}
    assert R.gates_check(pol, rep)["pass"]
    rep["breakeven_slippage_pct"] = 4.0
    g = R.gates_check(pol, rep)
    assert not g["pass"] and not g["checks"]["survives worse fills"]["ok"]


def test_repo_policy_loads_and_validates():
    pol = R.load_policy("graduation-v1")
    params = R.policy_params(pol)
    assert params["sniper"]["late"]["enabled"] and not params["sniper"]["entry"]["enabled"]
    assert pol["gates"]["min_days"] >= 14
