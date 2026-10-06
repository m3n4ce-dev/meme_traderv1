"""Regression tests for the second full code review (of 7a26092, 2026-10-03), findings R01-R29.
Each test follows the review's acceptance check for its finding. No network: transports are faked."""
import asyncio
import base64
import copy
import json
import shlex
import struct
import time
import types

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Metadata, Trade
from meme_trader.sniper.execution import LiveExecutor, PaperExecutor, SniperFill
from meme_trader.sniper.feeds import PUMP_PROGRAM, TRADE_EVENT, Feed, SolanaTradeFeed
from meme_trader.sniper.strategy import SniperPosition
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
A = "A" * 40 + "pump"
B = "B" * 40 + "pump"
DEV = "D" * 44


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    def __init__(self, realtime=False, degraded=False):
        self.realtime = realtime
        self.degraded = degraded
        self.host = "test"

    async def events(self):
        return
        yield


class Gate:
    """Executor whose buys wait until released (a live order confirming), sells fill at once."""

    def __init__(self, ex):
        self.paper = PaperExecutor(ex)
        self.release = asyncio.Event()
        self.buys = 0
        self.sells = 0

    async def buy(self, mint, curve, sol, priority=None):
        self.buys += 1
        await self.release.wait()
        return await self.paper.buy(mint, curve, sol, priority)

    async def sell(self, mint, curve, tokens, priority=None):
        self.sells += 1
        return await self.paper.sell(mint, curve, tokens, priority)


def engine(realtime=False, ex=None, mode="paper", degraded=False, p=None):
    p = p or copy.deepcopy(P)
    return Engine(p, Quiet(realtime, degraded), ex or PaperExecutor(p.sniper.execution), mode=mode,
                  log_to_journal=False, persist=False)


def token(eng, mint, creator=DEV, ts=0.0):
    s = eng.tokens[mint] = TokenState(mint, Launch(mint, ts, creator, symbol=mint[:3]), ts)
    s.on_launch(s.launch)
    return s


def hold(eng, s, tokens=1e6, cost=0.05, source="sniper", opened=0.0):
    pos = eng.positions[s.mint] = SniperPosition(s.mint, s.symbol, opened, s.curve.price, tokens, tokens, cost, cost,
                                                 70, peak_price=s.curve.price, exits=[], source=source)
    s.decided = "entered"
    return pos


def trade_log(mint: bytes, sol: int, tokens: int, buy: bool, user: bytes, v_sol=31_000_000_000,
              v_tokens=1_030_000_000_000_000) -> str:
    raw = (TRADE_EVENT + mint + struct.pack("<QQ?", sol, tokens, buy) + user
           + struct.pack("<qQQQQ", 0, v_sol, v_tokens, 0, 0))
    return "Program data: " + base64.b64encode(raw + bytes(250)).decode()


# --------------------------------------------------------------------------- R01 log provenance
FAKE = trade_log(bytes(range(32)), 250_000_000, 7_000_000_000_000, False, bytes(range(32, 64)))
OTHER = "Ev1L" + "1" * 39


def parsed(logs):
    return SolanaTradeFeed.parse_logs({"err": None, "logs": logs}, 1.0)


def test_r01_only_pump_emitted_trade_events_count():
    def pump(*x, d=1):
        return [f"Program {PUMP_PROGRAM} invoke [{d}]", *x, f"Program {PUMP_PROGRAM} success"]
    assert len(parsed(pump(FAKE))) == 1
    # an aggregator calling pump.fun (pump at depth 2) is fine
    assert len(parsed([f"Program {OTHER} invoke [1]", *pump(FAKE, d=2), f"Program {OTHER} success"])) == 1
    # an unrelated program logging the same bytes, alone or after pump returned
    assert parsed([f"Program {OTHER} invoke [1]", FAKE, f"Program {OTHER} success"]) == []
    assert parsed([*pump("Program log: Instruction: Buy"), f"Program {OTHER} invoke [1]", FAKE,
                   f"Program {OTHER} success"]) == []
    # nested: pump CPIs into a program that logs a fake event
    assert parsed([f"Program {PUMP_PROGRAM} invoke [1]", f"Program {OTHER} invoke [2]", FAKE,
                   f"Program {OTHER} success", f"Program {PUMP_PROGRAM} success"]) == []


def test_r01_malformed_or_truncated_invocation_logs_are_rejected():
    assert parsed([f"Program {PUMP_PROGRAM} invoke [2]", FAKE]) == []            # depth skipped
    assert parsed([f"Program {OTHER} success", FAKE]) == []                       # success without invoke
    assert parsed([f"Program {PUMP_PROGRAM} invoke [1]", f"Program {OTHER} success", FAKE]) == []
    assert parsed([f"Program {PUMP_PROGRAM} invoke [1]", "Log truncated", FAKE]) == []
    failed = {"err": {"InstructionError": [0, "Custom"]},
              "logs": [f"Program {PUMP_PROGRAM} invoke [1]", FAKE, f"Program {PUMP_PROGRAM} failed: custom"]}
    assert SolanaTradeFeed.parse_logs(failed, 1.0) == []


# --------------------------------------------------------------------------- R02 orders don't block the feed
def test_r02_pending_order_does_not_delay_another_tokens_dev_sell_exit():
    async def go():
        ex = Gate(P.sniper.execution)
        eng = engine(realtime=True, ex=ex)
        sa, sb = token(eng, A), token(eng, B)
        hold(eng, sb)
        await eng._buy(sa, 70, 0.05, ["test"])                 # a slow live buy is now in flight
        await asyncio.sleep(0)
        assert A in eng.pending and ex.buys == 1 and A not in eng.positions
        await eng.handle(Trade(B, time.time(), DEV, "sell", 0.5, 1e7, 30.5, 1.06e9))   # B's dev dumps
        await asyncio.sleep(0)                                  # let the dispatched sell run
        assert sb.dev_sold > 0 and ex.sells == 1 and B not in eng.positions
        ex.release.set()
        await asyncio.wait(set(eng.order_tasks), timeout=5)
        assert A in eng.positions and A not in eng.pending and not eng.book.reserved
    asyncio.run(go())


# --------------------------------------------------------------------------- R03 unknown outcomes
class Unknown:
    """Live-like executor: orders come back 'sent, outcome unknown'; resolve() answers from `chain`."""

    def __init__(self):
        self.sent = []
        self.chain: dict[str, SniperFill] = {}

    async def buy(self, mint, curve, sol, priority=None):
        self.sent.append(("buy", mint))
        return SniperFill(False, unknown=True, signature=f"b{len(self.sent)}")

    async def sell(self, mint, curve, tokens, priority=None):
        self.sent.append(("sell", mint))
        return SniperFill(False, unknown=True, signature=f"s{len(self.sent)}")

    async def resolve(self, sig, mint, side):
        return self.chain.get(sig, SniperFill(False, unknown=True, signature=sig))


def test_r03_unknown_sell_is_not_resent_and_is_booked_when_it_lands():
    async def go():
        ex = Unknown()
        eng = engine(ex=ex)
        s = token(eng, A)
        pos = hold(eng, s, tokens=10.0)
        await eng._sell(s, pos, 0.5, "partial")
        await eng._sell(s, pos, 0.5, "partial again")           # must NOT send a second, fresh sell
        assert ex.sent == [("sell", A)] and A in eng.pending and "s1" in eng.unresolved
        ex.chain["s1"] = SniperFill(True, sol=0.03, tokens=5.0, signature="s1")
        await eng._resolve_unresolved()
        assert not eng.unresolved and A not in eng.pending and pos.tokens == pytest.approx(5.0)
        assert eng.book.sol == pytest.approx(P.sniper.capital.starting_sol + 0.03)
    asyncio.run(go())


def test_r03_late_buy_becomes_a_managed_position_and_expired_orders_release():
    async def go():
        ex = Unknown()
        eng = engine(ex=ex)
        s = token(eng, A)
        await eng._buy(s, 70, 0.05, ["x"])
        assert A in eng.book.reserved and A in eng.pending and A not in eng.positions
        ex.chain["b1"] = SniperFill(True, sol=0.05, tokens=1e6, signature="b1", rent=0.002)
        await eng._resolve_unresolved()
        assert A in eng.positions and not eng.book.reserved
        assert eng.book.sol == pytest.approx(P.sniper.capital.starting_sol - 0.052)   # principal + locked rent
        sb = token(eng, B)
        await eng._buy(sb, 70, 0.05, ["x"])
        eng.unresolved["b2"]["sent_at"] -= 1_000                # long past any blockhash expiry, still unseen
        await eng._resolve_unresolved()
        assert B not in eng.pending and B not in eng.book.reserved and B not in eng.positions
    asyncio.run(go())


def test_r03_unresolved_orders_survive_a_restart(tmp_path):
    async def go():
        ex = Unknown()
        eng = engine(ex=ex, mode="live")
        eng.persist = True
        s = token(eng, A)
        hold(eng, s, tokens=10.0)
        await eng._sell(s, eng.positions[A], 1.0, "stop")
        eng.save_state()
        eng2 = engine(ex=Unknown(), mode="live")
        eng2.persist = True
        await eng2.restore_state()
        assert "s1" in eng2.unresolved and A in eng2.pending   # no new sell until the chain answers
    asyncio.run(go())


# --------------------------------------------------------------------------- R04 cash reservation
def test_r04_concurrent_buys_cannot_spend_the_reserve():
    async def go():
        ex = Gate(P.sniper.execution)
        p = copy.deepcopy(P)
        p.sniper.capital.max_open_positions = 5
        eng = engine(realtime=True, ex=ex, p=p)
        eng.book.sol = 0.2
        await eng._buy(token(eng, A), 70, 0.1, ["a"])
        await eng._buy(token(eng, B), 70, 0.1, ["b"])          # would leave < 0.03 reserve if both went
        await asyncio.sleep(0)
        assert ex.buys == 1 and B not in eng.pending and eng.stats["skipped_low_SOL"] == 1
        ex.release.set()
        await asyncio.wait(set(eng.order_tasks), timeout=5)
        assert eng.book.sol >= p.sniper.capital.min_sol_reserve
    asyncio.run(go())


def test_r04_final_size_is_authorized_not_the_preliminary_one():
    async def go():
        eng = engine()
        eng.book.sol = 0.09
        assert eng.entries_blocked(0.05) == ""                  # the copy path's preliminary check passes
        await eng._buy(token(eng, A), 70, 0.08, ["copy"], source="copy:x")   # the real, resized order
        assert A not in eng.positions and eng.book.sol == pytest.approx(0.09)
    asyncio.run(go())


# --------------------------------------------------------------------------- R05 callouts obey account limits
@pytest.mark.parametrize("why", ["daily_loss", "degraded"])
def test_r05_callouts_respect_daily_loss_and_feed_health(why):
    async def go():
        eng = engine(degraded=why == "degraded")
        if why == "daily_loss":
            eng.book.day_pnl = -P.sniper.capital.daily_loss_limit_sol
        s = token(eng, A)
        s.decided = "rejected: test"
        eng.now = 10_000
        import meme_trader.sniper.engine as E
        orig = E.eligible
        E.eligible = lambda *a, **k: (True, 90.0, [])
        try:
            await eng._maybe_callout()
        finally:
            E.eligible = orig
        assert A not in eng.positions and not eng.callouts.calls
    asyncio.run(go())


# --------------------------------------------------------------------------- R06 ledger
def test_r06_failed_fees_and_negative_sells_hit_cash_and_daily_pnl():
    async def go():
        eng = engine()
        s = token(eng, A)
        pos = hold(eng, s, tokens=10.0, cost=0.01)
        start = eng.book.sol
        eng._apply_sell(s, pos, SniperFill(False, error="failed", fees_lost=0.012015), "stop")
        assert eng.book.sol == pytest.approx(start - 0.012015) and eng.book.day_pnl == pytest.approx(-0.012015)
        eng._apply_sell(s, pos, SniperFill(True, sol=-0.006, tokens=10.0), "dust exit")
        assert eng.book.sol == pytest.approx(start - 0.018015)  # a negative net sell is real cash out
    asyncio.run(go())


def test_r06_wallet_sol_is_reconciled():
    async def go():
        eng = engine(mode="live")
        eng.book.sol = 0.8
        eng.ex.wallet = types.SimpleNamespace(sol_balance=lambda: 0.04)
        await eng.check_cash()
        assert eng.book.sol == pytest.approx(0.04) and eng.book.day_pnl == pytest.approx(-0.76)
    asyncio.run(go())


# --------------------------------------------------------------------------- R07 restart keeps safety context
def test_r07_dev_sell_after_restart_still_exits_and_defense_survives():
    async def go():
        eng = engine(mode="live")
        eng.persist = True
        s = token(eng, A)
        hold(eng, s)
        eng.defense_until, eng.defense_reason = 5_000.0, "5 losses in a row"
        eng.save_state()
        eng2 = engine(mode="live")
        eng2.persist = True
        await eng2.restore_state()
        s2 = eng2.tokens[A]
        assert s2.creator == DEV and eng2.defense_until == 5_000.0
        await eng2.handle(Trade(A, 10, DEV, "sell", 0.5, 1e7, 30.5, 1.06e9))
        assert s2.dev_sold > 0 and A not in eng2.positions       # same dev-sell exit as before the restart
    asyncio.run(go())


# --------------------------------------------------------------------------- R08 one bot per wallet/state, UI first
def test_r08_second_instance_refuses(tmp_path):
    from meme_trader.sniper.__main__ import _lock

    first = _lock(tmp_path / "run-live.lock", "busy")
    with pytest.raises(SystemExit, match="busy"):
        _lock(tmp_path / "run-live.lock", "busy")
    first.close()
    _lock(tmp_path / "run-live.lock", "busy").close()           # free again once the first one exits


def test_r08_dashboard_bind_failure_raises_before_trading():
    from meme_trader.ui.server import start

    async def go():
        eng = engine()
        runner = await start(eng, "127.0.0.1", 0)
        port = runner.addresses[0][1]
        with pytest.raises(OSError):
            await start(eng, "127.0.0.1", port)
        await runner.cleanup()
    asyncio.run(go())


def test_r08_helper_task_failures_are_reported():
    from meme_trader.sniper.__main__ import _supervise

    async def go():
        eng = engine()

        async def boom():
            raise RuntimeError("telethon missing")

        async def engine_run():
            await asyncio.sleep(0.05)
        await _supervise(eng, asyncio.create_task(engine_run()), {asyncio.create_task(boom(), name="telegram")})
        assert any("telegram stopped" in x["text"] for x in eng.log)
    asyncio.run(go())


# --------------------------------------------------------------------------- R09 metadata fetching
def test_r09_private_and_non_http_metadata_urls_are_never_requested(monkeypatch):
    from meme_trader.sniper import feeds

    def explode(*a, **k):
        raise AssertionError("no request may be made")
    monkeypatch.setattr("aiohttp.ClientSession", explode)
    for url in ("http://127.0.0.1:8787/", "http://10.0.0.5/x.json", "http://[::1]/", "http://169.254.169.254/",
                "ftp://example.com/x", "file:///etc/passwd", ""):
        assert asyncio.run(feeds.fetch_metadata(url)) == {}


def test_r09_resolver_refuses_private_addresses():
    from meme_trader.sniper.feeds import _PublicOnlyResolver

    async def go():
        r = _PublicOnlyResolver()
        try:
            with pytest.raises(OSError):
                await r.resolve("localhost", 80)
        finally:
            await r.close()
    asyncio.run(go())


def test_r09_oversized_bodies_and_bad_fields_are_dropped(monkeypatch):
    from aiohttp import web

    from meme_trader.sniper import feeds

    monkeypatch.setattr(feeds, "_public_ip", lambda host: True)     # let the test server on loopback through

    async def go():
        async def big(r):
            return web.Response(body=b"{" + b" " * 200_000 + b"}")

        async def ok(r):
            return web.json_response({"twitter": "x.com/a", "website": ["not", "a str"]})

        async def hop(r):
            raise web.HTTPFound("http://10.0.0.1/ok")
        app = web.Application()
        app.router.add_get("/big", big)
        app.router.add_get("/ok", ok)
        app.router.add_get("/hop", hop)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            monkeypatch.setattr(feeds, "_public_ip", lambda host: host == "127.0.0.1")
            assert await feeds.fetch_metadata(f"http://127.0.0.1:{port}/big") == {}
            assert await feeds.fetch_metadata(f"http://127.0.0.1:{port}/ok") == {"twitter": "x.com/a"}
            assert await feeds.fetch_metadata(f"http://127.0.0.1:{port}/hop") == {}   # redirect to private
        finally:
            await runner.cleanup()
    asyncio.run(go())


# --------------------------------------------------------------------------- R10 SOL quote identity
def test_r10_sol_pairs_are_identified_by_mint_not_symbol(monkeypatch):
    from meme_trader.clients import dexscreener as dx

    real = {"chainId": "solana", "baseToken": {"address": A}, "quoteToken": {"address": dx.WSOL_MINT, "symbol": "SOL"},
            "priceNative": "0.00001", "liquidity": {"usd": 5_000}}
    fake = {"chainId": "solana", "baseToken": {"address": A}, "quoteToken": {"address": "FAKEsol", "symbol": "SOL"},
            "priceNative": "100", "liquidity": {"usd": 9_000_000}}
    other = dict(real, baseToken={"address": B})
    monkeypatch.setattr(dx, "pairs_for", lambda mints: [fake, real, other])
    best = dx.best_pair_by_mint([A])
    assert best == {A: real}


# --------------------------------------------------------------------------- R11 funding from failed txs
def test_r11_failed_transactions_create_no_funding_edge_and_gaps_retry():
    from meme_trader.sniper.funding import FundingResolver, first_sol_source

    W = "W" * 44
    ok_tx = {"meta": {"err": None}, "transaction": {"message": {"instructions": [
        {"parsed": {"type": "transfer", "info": {"destination": W, "source": "REALfunder", "lamports": 10}}}]}}}
    bad_tx = copy.deepcopy(ok_tx)
    bad_tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    bad_tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]["source"] = "FAKE_FUNDER"
    assert first_sol_source(bad_tx, W) == "" and first_sol_source(ok_tx, W) == "REALfunder"

    calls = []

    class Resp:
        def __init__(self, d):
            self.d, self.status = d, 200

        async def json(self, content_type=None):
            return self.d

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def session(answers):
        def post(url, json):
            calls.append(json["method"])
            return Resp({"result": answers[json["method"]](json["params"])})
        return types.SimpleNamespace(post=post)

    sigs = [{"signature": "newer", "err": None}, {"signature": "oldest", "err": {"x": 1}}]   # newest first
    txs = {"newer": ok_tx}
    r = FundingResolver("rpc", 100)
    f = asyncio.run(r._rpc(session({"getSignaturesForAddress": lambda p: sigs,
                                    "getTransaction": lambda p: txs.get(p[0])}), W))
    assert f.funder == "REALfunder"                             # the failed oldest signature was skipped
    with pytest.raises(RuntimeError):                           # not retrievable now: retry later, don't cache
        asyncio.run(r._rpc(session({"getSignaturesForAddress": lambda p: [{"signature": "gone", "err": None}],
                                    "getTransaction": lambda p: None}), W))


# --------------------------------------------------------------------------- R12 results by mode/session
def test_r12_trades_are_tagged_and_reports_never_mix_modes(tmp_path):
    from meme_trader.sniper.report import load_trades, sessions

    async def close_one(mode):
        p = copy.deepcopy(P)
        eng = Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode=mode, log_to_journal=True, persist=False)
        s = token(eng, A)
        pos = hold(eng, s, tokens=10.0, cost=0.1)
        pos.proceeds_sol = 0.2
        eng._close(pos, s)
    for mode in ("paper-synthetic", "paper", "live"):
        asyncio.run(close_one(mode))
    (tmp_path / "trades-2026-01-01.jsonl").write_text(json.dumps({"closed": 1, "pnl": 9.9}) + "\n")   # untagged
    rows = load_trades(tmp_path)
    assert {r.get("mode", "unknown") for r in rows} == {"paper-synthetic", "paper", "live", "unknown"}
    paper = load_trades(tmp_path, mode="paper")
    assert len(paper) == 1 and paper[0]["session"].startswith("paper-") and paper[0]["config"]
    assert load_trades(tmp_path, mode="unknown")[0]["pnl"] == 9.9
    assert sessions(paper)[0][1] == P.sniper.capital.starting_sol


# --------------------------------------------------------------------------- R13 leader discovery
def test_r13_open_bags_count_net_of_cost_and_wins_use_the_whole_round_trip():
    from meme_trader.sniper.copytrade import discover

    k = 1.0e9 * 30.0
    ev = [Launch("M1" + "x" * 42, 0, DEV), Launch("M2" + "y" * 42, 0, DEV)]
    loser, winner = "L" * 44, "G" * 44
    ev += [Trade("M1" + "x" * 42, 10, loser, "buy", 10.0, 1e8, 40, k / 40),        # 10 SOL in ...
           Trade("M1" + "x" * 42, 20, "z" * 44, "sell", 1.0, 1e6, 4, k / 4)]       # ... now worth ~1 SOL
    ev += [Trade("M2" + "y" * 42, 10, winner, "buy", 1.0, 1e7, 31, k / 31),
           Trade("M2" + "y" * 42, 20, winner, "sell", 1.5, 1e7, 30, k / 30)]       # +0.5 SOL round trip
    ev += [Trade("M2" + "y" * 42, 30, loser, "buy", 8.0, 1e7, 31, k / 31),
           Trade("M2" + "y" * 42, 40, loser, "sell", 0.05, 5e6, 30, k / 30),        # -3.95 leg
           Trade("M2" + "y" * 42, 50, loser, "sell", 0.05, 5e6, 30, k / 30)]        # final leg "profitable"? no
    ranked = discover(ev, min_tokens=1)
    by = {w.wallet: w for w in ranked}
    assert by[loser].open_pnl_sol < -8 and ranked[0].wallet == winner
    assert by[loser].wins == 0 and by[winner].wins == 1


def test_r13_loss_then_profitable_final_leg_is_still_a_loss():
    from meme_trader.sniper.copytrade import discover

    k = 1.0e9 * 30.0
    w = "Q" * 44
    m = "M3" + "q" * 42
    ev = [Launch(m, 0, DEV), Trade(m, 1, w, "buy", 10.0, 1e7, 40, k / 40),
          Trade(m, 2, w, "sell", 0.1, 9e6, 30, k / 30),       # sold 90% for 0.1 SOL: -8.9
          Trade(m, 3, w, "sell", 2.0, 1e6, 30, k / 30)]       # last 10% for 2.0: +1.0 on this leg
    ws = discover(ev, min_tokens=1)[0]
    assert ws.closed == 1 and ws.wins == 0 and ws.realized_sol == pytest.approx(-7.9)


# --------------------------------------------------------------------------- R14 no future information
def test_r14_purged_split_keeps_training_strictly_before_testing():
    from meme_trader.sniper.sweep import split

    ev = []
    for i in range(10):
        m = f"T{i}" + "t" * 42
        ev += [Launch(m, i * 100.0, DEV), Trade(m, i * 100.0 + 500, "w", "buy", 1, 1, 31, 1e9)]
    tr, te = split(ev, 0.6, embargo_s=150)
    cutoff = 600.0
    assert max(e.ts for e in tr) < cutoff <= min(e.ts for e in te)
    train_mints = {e.mint for e in tr if isinstance(e, Launch)}
    assert all(e.ts < cutoff - 150 for e in tr if isinstance(e, Launch))       # embargoed tokens dropped
    assert "T5" + "t" * 42 not in train_mints and "T5" + "t" * 42 not in {e.mint for e in te}


def test_r14_replays_ignore_models_trained_on_their_data_and_start_neutral(tmp_path, monkeypatch):
    from meme_trader.sniper.predictor import LogisticModel
    from tests.test_predict import _separable

    (tmp_path / "callers.json").write_text(json.dumps({"x:alpha": {"calls": 50, "avg_return": 100}}))
    X, y = _separable(100)
    m = LogisticModel.fit(X, y)
    m.info = {"data_end_ts": 5_000.0, "source": "recorded", "promoted": True}
    m.save(tmp_path / "model.json")
    monkeypatch.setattr("meme_trader.sniper.engine.ROOT", tmp_path)
    p = copy.deepcopy(P)
    p.sniper.predict.model_path = str(tmp_path / "model.json")

    def replay(first_ts):
        eng = Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="backtest", log_to_journal=False)
        asyncio.run(eng.handle(Launch(A, first_ts, DEV)))
        return eng
    assert replay(4_000.0).model is None                        # overlaps what the model was fitted on
    assert replay(6_000.0).model is not None                    # strictly after: a fair out-of-sample test
    assert replay(6_000.0).callers.weight("x:alpha") == 0.25    # today's caller record doesn't leak in


# --------------------------------------------------------------------------- R15 promotion + display only
def test_r15_display_only_model_changes_nothing():
    from meme_trader.sniper.predictor import LogisticModel
    from tests.test_predict import _separable

    X, y = _separable(100)
    model = LogisticModel.fit(X, y)

    def size(with_model):
        eng = engine()
        s = token(eng, A)
        for i in range(30):
            s.on_trade(Trade(A, 1 + i, f"w{i}", "buy", 0.5, 1e6, 31 + i * 0.5, 1.07e9 * 30 / (31 + i * 0.5)), 2)
        eng.now = 40
        eng.model = model if with_model else None
        return eng._size(s, "sniper", 80, 0.05, None, [])
    assert size(True) == size(False)                            # display_only: true by default


def test_r15_synthetic_candidates_cannot_be_promoted(tmp_path, monkeypatch):
    from meme_trader.sniper.__main__ import _promote
    from meme_trader.sniper.predictor import LogisticModel
    from tests.test_predict import _separable

    monkeypatch.setattr("meme_trader.config.ROOT", tmp_path)
    X, y = _separable(100)
    m = LogisticModel.fit(X, y)
    m.info = {"source": "synthetic", "test": {"auc": 0.99}}
    m.save(tmp_path / "cand.json")
    p = copy.deepcopy(P)
    p.sniper.predict.model_path = str(tmp_path / "model.json")
    with pytest.raises(SystemExit, match="synthetic"):
        _promote(types.SimpleNamespace(file_model=str(tmp_path / "cand.json"), force=True), p)
    assert not (tmp_path / "model.json").exists()


# --------------------------------------------------------------------------- R16 censored labels
def test_r16_unfinished_horizons_are_censored_not_losses():
    from meme_trader.sniper.predictor import build_dataset

    k = 1.073e9 * 30

    def events(end):
        ev = [Launch(A, 0, DEV)]
        ev += [Trade(A, 1 + i, f"b{i}", "buy", 0.1, 1e6, 30 + 0.1 * (i + 1), k / (30 + 0.1 * (i + 1)))
               for i in range(5)]
        ev.append(Trade(A, end, "z", "buy", 0.001, 1, 30.5, k / 30.5))
        return ev
    p = copy.deepcopy(P)
    _, y, _ = build_dataset(events(16), p, checkpoints=[15], horizon_s=600)
    assert y == []                                              # recording ended 1 s into a 600 s horizon
    _, y, _ = build_dataset(events(700), p, checkpoints=[15], horizon_s=600)
    assert y == [0]                                             # watched the whole horizon: never doubled


# --------------------------------------------------------------------------- R17 metadata events
def test_r17_metadata_arrives_with_its_own_time_in_training_and_replay():
    from meme_trader.sniper.features import extract
    from meme_trader.sniper.predictor import build_dataset

    k = 1.073e9 * 30
    ev = [Launch(A, 0, DEV)]
    ev += [Trade(A, 1 + i, f"b{i}", "buy", 0.1, 1e6, 30 + 0.1 * (i + 1), k / (30 + 0.1 * (i + 1)))
           for i in range(5)]
    ev += [Metadata(A, 20, twitter="x.com/a", telegram="t.me/a", website="a.xyz"),
           Trade(A, 900, "z", "buy", 0.001, 1, 30.5, k / 30.5)]
    p = copy.deepcopy(P)
    X, _, meta = build_dataset(ev, p, checkpoints=[15, 30], horizon_s=600)
    from meme_trader.sniper.features import FEATURES
    soc = FEATURES.index("socials_count")
    assert [row[soc] for row in X] == [0.0, 3.0]                # unknown at 15 s, known from 20 s on
    eng = engine()
    for e in ev[:-1]:
        asyncio.run(eng.handle(e))
    assert extract(eng.tokens[A], 30, eng._ctx(eng.tokens[A]))["socials_count"] == 3.0


# --------------------------------------------------------------------------- R18 drawdown memory
def test_r18_max_drawdown_survives_chart_rollover():
    from meme_trader.sniper.analytics import compute

    eng = engine()
    eng.book.mark(1.0)
    eng.book.mark(0.5)
    for i in range(3000):                                       # a long flat tail scrolls the chart buffer
        eng.book.equity_hist.append((i, 0.5))
        eng.book.mark(0.5)
    a = compute([], list(eng.book.equity_hist), 1.0, 30, observed_max_dd_pct=eng.book.max_dd_pct)
    assert a["drawdown"]["max_pct"] == pytest.approx(50.0)


# --------------------------------------------------------------------------- R19 AI desk fails closed
def test_r19_desk_errors_never_make_approval_easier():
    from meme_trader.sniper.desk import Vote, aggregate

    w = dict(P.sniper.desk.weights)
    def err(p):
        return Vote(p, "pass", 0, error="timeout")
    v = aggregate([Vote("veteran", "buy", 60), err("narrative"), err("skeptic"), err("quant")], w, 0.45, 75)
    assert not v.approve
    v = aggregate([Vote("veteran", "buy", 90), Vote("narrative", "buy", 90), Vote("quant", "buy", 90),
                   err("skeptic")], w, 0.45, 75)
    assert not v.approve and "skeptic" in v.summary
    v = aggregate([Vote(x, "buy", 90) for x in ("veteran", "narrative", "skeptic", "quant")], w, 0.45, 75)
    assert v.approve


# --------------------------------------------------------------------------- R20 X rules
def test_r20_x_setup_keeps_other_apps_rules_and_backs_off():
    from meme_trader.sniper.signals import X_RULE_TAG, sync_x_rules, x_retry_delay

    posted = []

    class R:
        def __init__(self, status, d=None):
            self.status, self.d = status, d or {}

        async def json(self):
            return self.d

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    s = types.SimpleNamespace(
        get=lambda url: R(200, {"data": [{"id": "mine", "tag": X_RULE_TAG}, {"id": "other-app-rule", "tag": "x"}]}),
        post=lambda url, json: posted.append(json) or R(201))
    asyncio.run(sync_x_rules(s, "base", ["alice"]))
    assert posted[0] == {"delete": {"ids": ["mine"]}}
    assert posted[1]["add"][0]["tag"] == X_RULE_TAG
    assert x_retry_delay(429, {"x-rate-limit-reset": str(time.time() + 120)}, 0) > 100
    assert x_retry_delay(503, {}, 3) >= 40


# --------------------------------------------------------------------------- R21 Telegram delivery
def test_r21_failed_alerts_are_not_counted_as_sent(monkeypatch):
    from meme_trader.sniper.notify import Notifier

    async def no_sleep(_):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    class R:
        def __init__(self, status, d):
            self.status, self.d = status, d

        async def json(self, content_type=None):
            return self.d

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def session(answers):
        return types.SimpleNamespace(post=lambda url, json: answers.pop(0))
    n = Notifier(["error"])
    assert not asyncio.run(n.deliver(session([R(403, {"ok": False, "description": "bot blocked"})]), "x"))
    assert n.sent == 0 and n.failed == 1 and "403" in n.last_error
    ok = asyncio.run(n.deliver(session([R(429, {"ok": False, "parameters": {"retry_after": 1}}),
                                        R(200, {"ok": True})]), "x"))
    assert ok and n.sent == 1


# --------------------------------------------------------------------------- R22 review proposals
def test_r22_review_proposals_are_validated_and_quoted():
    from meme_trader.sniper.__main__ import apply_overrides
    from meme_trader.sniper.review import validate_changes

    sn = dict(P["sniper"])
    changes = [{"key": "copy.leaders", "value": '[{"address": "abc", "mode": "signal"}]', "rationale": ""},
               {"key": "exit.stop_loss_pct", "value": "25", "rationale": ""},
               {"key": "exit.stop_loss_pct; rm -rf ~", "value": "1", "rationale": ""},
               {"key": "entry.min_score", "value": '"$(curl evil)"', "rationale": ""}]
    ok, rejected = validate_changes(changes, sn)
    assert [c["key"] for c in ok] == ["copy.leaders", "exit.stop_loss_pct"] and len(rejected) == 2
    argv = shlex.split(shlex.join([x for c in ok for x in ("--set", c["arg"])]))
    assert argv[1] == 'copy.leaders=[{"address":"abc","mode":"signal"}]'     # one argument, JSON intact
    p = apply_overrides(copy.deepcopy(P), [argv[1], argv[3]])
    assert p.sniper.copy.leaders == [{"address": "abc", "mode": "signal"}] and p.sniper.exit.stop_loss_pct == 25


# --------------------------------------------------------------------------- R23 sniper config validation
@pytest.mark.parametrize("setting", ["capital.starting_sol=0", "capital.max_drawdown_pct=.nan",
                                     "execution.sell_slippage_steps=[]", "sizing.max_usd=1", "late.max_curve_pct=99",
                                     "no.such.key=1"])
def test_r23_invalid_sniper_settings_fail_early(setting):
    from meme_trader.sniper.__main__ import apply_overrides

    with pytest.raises(config.ConfigError):
        apply_overrides(copy.deepcopy(P), [setting])


def test_r23_dashboard_changes_are_validated_and_reverted():
    eng = engine()
    eng.p.late["exit_curve_pct"] = 90
    before = eng.p.exit.stop_loss_pct
    assert eng.set_control("exit.stop_loss_pct", "nan")
    assert eng.p.exit.stop_loss_pct == before
    assert eng.set_control("sizing.max_usd", 10) == "" and eng.p.sizing.max_usd == 10


# --------------------------------------------------------------------------- R24-R26 DexScreener bot
def test_r24_paper_state_never_loads_into_live(tmp_path, monkeypatch):
    from meme_trader.agents import risk

    monkeypatch.setattr(risk, "DATA", tmp_path)
    monkeypatch.setattr(risk, "LEGACY_STATE", tmp_path / "state.json")
    paper = risk.Portfolio.load(1.0, "paper")
    paper.sol = 123.0
    paper.save()
    (tmp_path / "state.json").write_text(json.dumps({"sol": 9.0, "start_sol": 1.0, "positions": {}}))
    live = risk.Portfolio.load(1.0, "live", "W" * 44)
    assert live.sol == 1.0 and live.mode == "live"              # fresh, not the paper or untagged book
    live.save()
    with pytest.raises(RuntimeError):                           # same file name prefix, different wallet
        (tmp_path / "state-live-WWWWWWWW.json").write_text(json.dumps({**json.loads(
            (tmp_path / "state-live-WWWWWWWW.json").read_text()), "wallet": "X" * 44}))
        risk.Portfolio.load(1.0, "live", "W" * 44)


def test_r25_kill_switch_sells_everything_even_on_take_profit_or_no_price(monkeypatch, tmp_path):
    from meme_trader import orchestrator
    from meme_trader.agents import risk
    from meme_trader.models import Fill, Position

    monkeypatch.setattr(risk, "DATA", tmp_path)
    o = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    o.p = P
    o.pf = risk.Portfolio(sol=0.5, start_sol=1.0, halted="drawdown")
    o.risk = risk.RiskManager(P, o.pf)
    sent = []
    o.exec = types.SimpleNamespace(execute=lambda order, quote=None: sent.append(order) or Fill(order, False))
    for m in ("TP", "NOPRICE"):
        o.pf.positions[m] = Position(mint=m, symbol=m, entry_price_sol=1e-6, initial_tokens=100, tokens=100,
                                     cost_sol=0.01, decimals=6, peak_price_sol=1e-6)
    monkeypatch.setattr(orchestrator, "record", lambda *a, **k: None)
    o.manage_positions({"TP": 1.6e-6})                          # +60%: the monitor alone would sell only 50
    assert sorted((x.mint, x.token_amount) for x in sent) == [("NOPRICE", 100), ("TP", 100)]


def test_r26_lp_lock_is_checked_on_the_selected_pool():
    from meme_trader.agents import safety
    from meme_trader.models import Candidate

    c = Candidate(mint=A, pair_address="SELECTED", liquidity_usd=1e6, pair_created_at=time.time() - 7200)
    rc = {"score_normalised": 0, "totalHolders": 1000, "topHolders": [],
          "markets": [{"pubkey": "SELECTED", "lp": {"lpLockedPct": 0}}, {"pubkey": "TINY", "lp": {"lpLockedPct": 100}}]}
    rep = safety.evaluate(c, rc, P.safety)
    assert not rep.passed and any("LP locked 0%" in r for r in rep.reasons)
    rc["markets"] = [{"pubkey": "TINY", "lp": {"lpLockedPct": 100}}]
    assert any("unknown" in r for r in safety.evaluate(c, rc, P.safety).reasons)


# --------------------------------------------------------------------------- R27-R29
def test_r27_dust_writeoff_reaches_the_daily_ledger():
    async def go():
        eng = engine()
        s = token(eng, A)
        pos = hold(eng, s, tokens=10.0, cost=0.1)
        s.curve.v_sol = 1e-9                                    # remainder now worth dust
        eng._apply_sell(s, pos, SniperFill(True, sol=0.0, tokens=5.0), "stop")
        row = eng.book.closed[-1]
        assert row["pnl"] == pytest.approx(-0.1) and eng.book.day_pnl == pytest.approx(row["pnl"])
    asyncio.run(go())


def test_r28_restored_graduation_play_honours_its_own_max_hold():
    async def go():
        eng = engine()
        s = eng.tokens[A] = TokenState(A, None, 0)              # restored: price unknown
        pos = hold(eng, s, source="late")
        eng.now = P.sniper.late.max_hold_s + 10
        await eng._check_exit(s)
        assert A not in eng.positions or pos.exits             # sold despite no price
    asyncio.run(go())


def test_r29_liquidity_cap_is_a_hard_ceiling():
    from meme_trader.sniper.sizing import size_usd

    z = P.sniper.sizing
    usd, why = size_usd(z, 1.0, "copy", 0.1, 150)               # cap $0.45 < minimum order
    assert usd == 0.0 and "no trade" in why
    assert size_usd(z, 1.0, "sniper", 0.0, 150)[0] == 0.0       # zero liquidity: no trade, not "uncapped"
    usd, _ = size_usd(z, 1.0, "sniper", 3.456, 150)
    assert usd <= 3.456 * z.max_pct_of_curve_sol / 100 * 150


# --------------------------------------------------------------------------- signing an opaque payload
def test_payload_that_would_drain_the_wallet_is_never_signed(monkeypatch):
    signed = []

    class W:
        pubkey = "W" * 44

        def simulate_sol_change(self, tx_b64):
            return -0.9                                         # a 0.1 SOL buy that also ships 0.8 elsewhere

        def sign(self, tx_b64):
            signed.append(tx_b64)
            return "raw", "sig"

    monkeypatch.setattr("httpx.post", lambda url, timeout, data: types.SimpleNamespace(status_code=200,
                                                                                         content=b"tx", text=""))
    fill = asyncio.run(LiveExecutor(P.sniper.execution, W()).buy(A, None, 0.1))
    assert not fill.ok and "refused to sign" in fill.error and not signed


# --------------------------------------------------------------------------- follow-ups found while fixing
def test_restore_drops_reservations_without_an_unresolved_order():
    async def go():
        eng = engine(mode="live")
        eng.persist = True
        eng.book.reserved = {A: 0.05, B: 0.05}                  # B's order died with the process, unsent
        eng.unresolved = {"b1": {"mint": A, "side": "buy", "sol": 0.05, "score": 1, "notes": [], "source": "sniper",
                                 "leader": "", "sent_at": time.time()}}
        eng.save_state()
        eng2 = engine(mode="live")
        eng2.persist = True
        await eng2.restore_state()
        assert eng2.book.reserved == {A: 0.05}
    asyncio.run(go())


def test_cash_check_skips_when_the_ledger_moved_during_the_read():
    async def go():
        eng = engine(mode="live")
        eng.book.sol = 0.5

        def balance():                                          # a sell's proceeds get booked mid-read
            eng.book.sol = 0.6
            return 0.5
        eng.ex.wallet = types.SimpleNamespace(sol_balance=balance)
        await eng.check_cash()
        assert eng.book.sol == pytest.approx(0.6) and eng.book.day_pnl == 0.0
    asyncio.run(go())


def test_order_task_errors_are_reported_not_lost():
    async def go():
        eng = engine(realtime=True)

        async def broken():
            raise ValueError("bug in an order path")
        await eng._dispatch(broken())
        await asyncio.wait(set(eng.order_tasks), timeout=2)
        await asyncio.sleep(0)
        assert any("internal error in an order task" in x["text"] for x in eng.log)
    asyncio.run(go())


def test_trade_feed_subscribes_at_the_configured_commitment():
    sent = []

    class WS:
        async def send_json(self, d):
            sent.append(d)
            raise OSError("stop after subscribing")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def ws_connect(self, *a, **k):
            return WS()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def go():
        f = SolanaTradeFeed("wss://example.org", commitment="confirmed")
        import aiohttp
        orig = aiohttp.ClientSession
        aiohttp.ClientSession = lambda *a, **k: Session()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(f._trades(asyncio.Queue()), 0.2)
        finally:
            aiohttp.ClientSession = orig
    asyncio.run(go())
    assert sent[0]["params"][1] == {"commitment": "confirmed"}
    assert P.sniper.feed.commitment == "confirmed"


# --------------------------------------------------------------------------- feed watchdog (2026-10-03 outage)
def _chain(mint, n, skip_every=0):
    """n consecutive buys of 1M tokens on one curve; every `skip_every`-th one is dropped (a missed trade)."""
    out, vt = [], 1_000_000_000.0
    for i in range(n):
        vt -= 1_000_000
        if skip_every and i % skip_every == 0:
            continue
        out.append(Trade(mint, i, "w", "buy", 0.03, 1_000_000, 31, vt))
    return out


def test_watchdog_measures_missing_trades():
    from meme_trader.sniper.feeds import FeedQuality

    q = FeedQuality(max_gap_pct=5, min_checks=100)
    for t in _chain(A, 300):
        q.observe(t)
    assert q.measured and q.gap_pct == 0 and not q.bad
    q.reset()
    for t in _chain(A, 300, skip_every=8):                # ~1 in 8 trades never arrived
        q.observe(t)
    assert q.bad and 10 < q.gap_pct < 16


def test_watchdog_endpoint_order(monkeypatch):
    from meme_trader.sniper.feeds import FALLBACK_WS, ws_urls

    monkeypatch.setenv("SOLANA_WS_URL", "wss://paid.example/k, wss://solana-rpc.publicnode.com")
    assert ws_urls() == ["wss://paid.example/k", *FALLBACK_WS]                 # deduped, env first
    assert ws_urls(["wss://a.example", "wss://b.example"])[:2] == ["wss://a.example", "wss://b.example"]


def test_watchdog_flags_a_silent_open_connection_and_retries_primary():
    f = SolanaTradeFeed("wss://primary.example", stall_s=60)
    now = 1_000.0
    f.last_trade = now - 61
    assert "no pump.fun trades for 61s" in f._check_stream(now, now - 100)
    f.last_trade = now
    f.ws_idx = 1
    assert f._check_stream(now, now - f.RETRY_PRIMARY_S - 1) == "retry"
    f.ws_idx = 0
    assert f._check_stream(now, now - f.RETRY_PRIMARY_S - 1) == ""


def test_watchdog_switches_endpoint_when_trades_go_missing(monkeypatch):
    import aiohttp

    seen_urls = []
    pump = [f"Program {PUMP_PROGRAM} invoke [1]", None, f"Program {PUMP_PROGRAM} success"]

    def note(t):
        logs = list(pump)
        logs[1] = trade_log(bytes(32), 30_000_000, int(t.tokens * 1e6), True, bytes(range(32)),
                            v_tokens=int(t.v_tokens * 1e6))
        return types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(
            {"params": {"result": {"value": {"err": None, "logs": logs, "signature": "s"}}}}))

    class WS:
        def __init__(self, msgs):
            self.msgs = msgs

        async def send_json(self, d):
            pass

        async def receive(self, timeout=None):
            if self.msgs:
                return self.msgs.pop(0)
            await asyncio.get_running_loop().create_future()      # an open socket that never speaks

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def ws_connect(self, url, **k):
            seen_urls.append(url)
            gappy = [note(t) for t in _chain(A, 700, skip_every=5)]                # 20% missing
            return WS(gappy if len(seen_urls) == 1 else [])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    real_sleep = asyncio.sleep

    async def quick_sleep(_):
        await real_sleep(0)                                   # skip the backoff, still yield
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: Session())

    async def go():
        f = SolanaTradeFeed("wss://flaky.example", max_gap_pct=5)
        monkeypatch.setattr(asyncio, "sleep", quick_sleep)
        task = asyncio.ensure_future(f._trades(asyncio.Queue()))
        for _ in range(500):
            await real_sleep(0)
            if len(seen_urls) >= 2:
                break
        task.cancel()
        return f
    asyncio.run(asyncio.wait_for(go(), 20))
    assert seen_urls[0] == "wss://flaky.example" and seen_urls[1] == "wss://solana-rpc.publicnode.com"


def test_watchdog_never_moves_to_an_endpoint_measured_slower(monkeypatch):
    """2026-10-04: mainnet-beta spiked past 5 s, the watchdog moved to PublicNode (10 s behind) and back,
    40 times in 35 minutes. A slow endpoint is only left for one that isn't known to be slower."""
    from meme_trader.sniper.feeds import FeedQuality
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)              # (no metered backup in this test)
    monkeypatch.delenv("SOLANA_WS_BACKUP_URL", raising=False)

    f = SolanaTradeFeed("wss://fast.example, wss://slow.example", stall_s=60, max_lag_s=5)
    f.ws_urls = ["wss://fast.example", "wss://slow.example"]
    now = 10_000.0
    f.last_trade, f.trades_up = now, True
    f.quality = FeedQuality(max_lag_s=5, min_lags=10)
    f.quality.checks.extend([True] * 500)
    for i in range(20):                                          # the fast one, briefly 6 s behind
        f.quality.observe(Trade(f"m{i}", now, "w", "buy", 1, 1, 30, 1e9, chain_ts=now - 6))
    f.endpoint_lag[1] = (10.0, now - 60)                         # the other was 10 s behind a minute ago
    assert f._check_stream(now, now - 100) == ""                 # stay: entries stay paused, no churn
    assert "behind the chain" in f.degraded_reason
    f.endpoint_lag[1] = (10.0, now - f.LAG_MEMORY_S - 1)         # that measurement is old: try it again
    assert "behind the chain" in f._check_stream(now, now - 100)
    assert f._next_endpoint("6s behind the chain", now) == 1
    f.endpoint_lag[1] = (2.0, now - 60)                          # known faster: go
    assert f._next_endpoint("6s behind the chain", now) == 1
    # on a fallback, the 30-minute retry of the first endpoint skips it while it's known slower
    f.ws_idx, f.endpoint_lag = 1, {0: (12.0, now - 60)}
    f.quality = FeedQuality(max_lag_s=5, min_lags=10)
    f.quality.checks.extend([True] * 500)
    for i in range(20):
        f.quality.observe(Trade(f"m{i}", now, "w", "buy", 1, 1, 30, 1e9, chain_ts=now - 1.5))
    assert f._check_stream(now, now - f.RETRY_PRIMARY_S - 1) == ""
    f.endpoint_lag = {0: (12.0, now - f.LAG_MEMORY_S - 1)}
    assert f._check_stream(now, now - f.RETRY_PRIMARY_S - 1) == "retry"
    assert f._next_endpoint("connection closed (CLOSE)", now) == 0


def test_feed_starts_on_the_fastest_endpoint_it_remembers(tmp_path, monkeypatch):
    """Every restart used to begin on PublicNode (10 s behind) and pause entries until the watchdog moved on."""
    monkeypatch.delenv("SOLANA_WS_URL", raising=False)
    mem = tmp_path / "feed_endpoints.json"
    f = SolanaTradeFeed(memory_path=mem)                       # nothing remembered: first in the list
    assert f.ws_idx == 0 and f.n_configured == 0
    now = time.time()
    f.endpoint_lag = {0: (10.2, now), 1: (1.4, now)}
    f._save_lags(now)
    saved = json.loads(mem.read_text())
    assert set(saved) == {"solana-rpc.publicnode.com", "api.mainnet-beta.solana.com"}   # hostnames only
    g = SolanaTradeFeed(memory_path=mem)
    assert g.ws_urls[g.ws_idx].endswith("api.mainnet-beta.solana.com")
    mem.write_text(json.dumps({"api.mainnet-beta.solana.com": [1.4, now - 7 * 3600]}))   # too old to trust
    assert SolanaTradeFeed(memory_path=mem).ws_idx == 0
    # on a free fallback, the 30-minute retry of a free "primary" no longer happens
    g.last_trade = now
    assert g._check_stream(now, now - g.RETRY_PRIMARY_S - 1) == ""


def test_a_hang_up_on_the_fastest_endpoint_reconnects_to_it(monkeypatch):
    """2026-10-05: the public RPC hung up every 20 s-5 min; the bot flapped to PublicNode (10 s behind) and back,
    pausing entries each time. Now a hang-up on the fastest known endpoint is a reconnect to it."""
    monkeypatch.delenv("SOLANA_WS_URL", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)              # (no metered backup in this test)
    monkeypatch.delenv("SOLANA_WS_BACKUP_URL", raising=False)
    f = SolanaTradeFeed()
    pn, mb = f.ws_urls.index("wss://solana-rpc.publicnode.com"), f.ws_urls.index("wss://api.mainnet-beta.solana.com")
    now = 10_000.0
    f.endpoint_lag = {mb: (1.7, now), pn: (10.0, now)}
    f.ws_idx = mb
    for k in range(3):                                       # a few hang-ups: stay on the fast one
        assert f._next_endpoint("connection closed (CLOSE)", now + k) == mb
    assert f._next_endpoint("connection closed (CLOSE)", now + 4) == pn       # hanging up all the time: move on
    f.ws_idx = pn                                            # a hang-up on a slow one: the usual next endpoint
    assert f._next_endpoint("ClientConnectorError: refused", now + 5) != pn
    f.ws_idx = mb                                            # slow data still moves to a faster endpoint, never a slower one
    assert f._dropped("connection closed (CLOSE)") and not f._dropped("12s behind the chain") and not f._dropped("retry")
    f.switches.extend([(now, "a", "a", "x"), (now, "a", "b", "y")])
    st = f.stream_stats(now + 10)
    assert st["reconnects_1h"] == 1 and st["switches_1h"] == 1


def test_the_metered_backup_is_used_only_while_the_free_feeds_fail(monkeypatch, tmp_path):
    """Your Helius key's websocket backs up the free endpoints during outages, within a daily allowance."""
    monkeypatch.delenv("SOLANA_WS_URL", raising=False)
    monkeypatch.delenv("SOLANA_WS_BACKUP_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "k-secret")
    f = SolanaTradeFeed(backup_mb_per_day=100)
    b = f.backup_idx
    assert b == len(f.ws_urls) - 1 and f._hostname(f.ws_urls[b]) == "mainnet.helius-rpc.com"   # (the key never shows)
    pn, mb = f.ws_urls.index("wss://solana-rpc.publicnode.com"), f.ws_urls.index("wss://api.mainnet-beta.solana.com")
    now = 50_000.0
    f.endpoint_lag = {mb: (1.7, now), pn: (10.0, now), b: (0.4, now)}
    f.ws_idx = mb
    assert f._best_known(now) == mb and f._faster_endpoint(now) is None    # the backup is never just "faster"
    for k in range(3):
        assert f._next_endpoint("connection closed (CLOSE)", now + k) == mb
    assert f._next_endpoint("connection closed (CLOSE)", now + 3) == b    # keeps hanging up: the backup
    f.ws_idx = b                                    # on the backup: free endpoints are measured on the side first
    f.last_trade = now + f.BACKUP_RETRY_S + 1
    assert f._check_stream(now + 10, now) == ""
    assert f._check_stream(now + f.BACKUP_RETRY_S + 1, now) == ""      # (no loop here: the probe can't start)
    f.probe_log.append((now + 320, "api.mainnet-beta.solana.com", 6.5))   # measured: still behind -> stay
    assert f._check_stream(now + 330, now) == ""
    f.probe_log.append((now + 650, "api.mainnet-beta.solana.com", 1.4))   # measured good -> the stream goes back
    assert f._check_stream(now + 660, now) == "retry free"
    assert f._next_endpoint("retry free", now + 400) == mb
    f.backup_bank, f.backup_bank_ts = 0.0, now + 20                      # the allowance is spent
    assert f._check_stream(now + 20, now) == "backup allowance used up"
    f.ws_idx = mb                                                         # a slow free feed: no backup left, stay
    f.closes.clear()
    assert not f._backup_ok(now) and f._next_endpoint("connection closed (CLOSE)", now + 500) == mb
    st = f.stream_stats(now)
    assert st["backup"]["host"] == "mainnet.helius-rpc.com" and st["backup"]["mb_left"] < 1
    # a slow free feed with allowance left goes to the backup instead of waiting it out
    g = SolanaTradeFeed(backup_mb_per_day=100)
    g.ws_idx = g.ws_urls.index("wss://api.mainnet-beta.solana.com")
    g.endpoint_lag = {g.ws_urls.index("wss://solana-rpc.publicnode.com"): (10.0, now)}
    g.last_trade, g.trades_up = now, True
    from meme_trader.sniper.feeds import FeedQuality
    g.quality = FeedQuality(max_lag_s=5, min_lags=10)
    g.quality.checks.extend([True] * 500)
    for i in range(20):
        g.quality.observe(Trade(f"m{i}", now, "w", "buy", 1, 1, 30, 1e9, chain_ts=now - 7))
    assert "behind the chain" in g._check_stream(now, now - 100)
    assert g._next_endpoint("7s behind the chain", now) == g.backup_idx
    # with no Helius key there's no backup, and nothing changes
    monkeypatch.delenv("HELIUS_API_KEY")
    assert SolanaTradeFeed().backup_idx is None



def test_backup_usage_survives_a_restart_and_probes_measure_on_the_side(monkeypatch, tmp_path):
    import time
    monkeypatch.delenv("SOLANA_WS_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "k")
    mem = tmp_path / "feed_endpoints.json"
    f = SolanaTradeFeed(memory_path=mem, backup_mb_per_day=100)
    t = time.time()
    f.backup_bank, f.backup_bank_ts = 58.0, t
    f._save_usage(t)
    g = SolanaTradeFeed(memory_path=mem, backup_mb_per_day=100)          # the bot restarts: no free refill
    assert abs(g._backup_left_mb(t + 1) - 58.0) < 0.01
    assert abs(g._backup_left_mb(t + 86400 / 2) - 108.0) < 0.01         # half a day later: +50 MB
    assert g._backup_left_mb(t + 86400 * 60) == g.backup_bank_cap == 4800.0   # saved up to the bank's size, no further

    async def fake_probe(i, secs=None):
        return 1.6
    g._probe = fake_probe
    asyncio.run(g._probe_free())
    when, host, lag = g.probe_log[-1]
    assert lag == 1.6 and host in ("api.mainnet-beta.solana.com", "solana-rpc.publicnode.com")
    assert any(v[0] == 1.6 for v in g.endpoint_lag.values())             # the measurement counts for choosing


def test_a_spent_backup_is_not_hopped_onto_for_a_few_seconds_of_refill(monkeypatch, tmp_path):
    """Seen 2026-10-05: allowance spent, the refill made the backup look usable every 30 s: 33 switches an hour."""
    monkeypatch.delenv("SOLANA_WS_URL", raising=False)
    monkeypatch.delenv("SOLANA_WS_BACKUP_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "k-secret")
    f = SolanaTradeFeed(backup_mb_per_day=1200)
    b, mb = f.backup_idx, f.ws_urls.index("wss://api.mainnet-beta.solana.com")
    now = 50_000.0
    f.ws_idx = mb
    f.endpoint_lag = {i: (10.0, now) for i in range(len(f.ws_urls)) if i not in (b, mb)}   # the other free ones: slower
    f.backup_bank, f.backup_bank_ts = 0.0, now - 30           # spent; 30 s of refill is ~0.4 MB
    assert 0 < f._backup_left_mb(now) < 1 and not f._backup_ok(now)
    f.quality.lags.extend([6.0] * f.quality.min_lags)            # slow, and nothing faster or affordable: stay put
    f.last_trade = now
    assert f._check_stream(now, now - 100) == ""
    f.backup_bank, f.backup_bank_ts = 0.0, now - 2.1 * 3600      # two hours of refill: worth moving for again
    assert f._backup_ok(now) and "behind the chain" in f._check_stream(now, now - 100)
    assert f._next_endpoint("6s behind the chain", now) == b


def test_the_launch_backup_pauses_entries_only_when_it_goes_quiet():
    import time
    f = SolanaTradeFeed("wss://example.org/")
    f.launches.urls.append("wss://pumpdev.io/ws")
    f.trades_up = True
    for _ in range(f.quality.min_checks):
        f.quality.checks.append(True)
    f.launches.url_idx = 1                                      # new coins come from pumpdev.io now
    f.launches.last_launch = time.time() - 5
    assert not f.degraded and f.host == "example.org"           # (the trade server, not the launch one)
    f.launches.last_launch = time.time() - 90
    assert f.degraded and "no new coins" in f.degraded_reason


def test_a_reconnect_to_the_endpoint_that_was_fine_keeps_entries_open():
    """2026-10-05: 109 minutes of entries paused 'checking trade data' after reconnects, mostly to the same endpoint."""
    import time
    f = SolanaTradeFeed("wss://example.org/")
    f.trades_up = True
    f.launches.last_launch = time.time()
    assert "checking" in f.degraded_reason                      # a fresh connection, nothing known about it
    f._good = (f.ws_idx, time.time() - 5)                       # it measured fine 5 s before the hang-up
    assert not f.degraded and f._trusted()
    f._good = (f.ws_idx, time.time() - f.TRUST_S - 1)            # too long ago
    assert "checking" in f.degraded_reason
    f._good = (f.ws_idx + 1, time.time())                        # a different endpoint
    assert "checking" in f.degraded_reason
    f._good = (f.ws_idx, time.time())
    f.quality.lags.extend([9.0] * f.quality.min_lags)            # trusted, but this connection turns out slow
    assert f.degraded and "behind the chain" in f.degraded_reason


def test_a_newly_configured_endpoint_is_tried_first_at_startup(tmp_path, monkeypatch):
    """A paid endpoint just added has no measured lag yet; the remembered free ones mustn't beat it for 30 minutes."""
    mem = tmp_path / "feed_endpoints.json"
    now = time.time()
    mem.write_text(json.dumps({"api.mainnet-beta.solana.com": [1.4, now], "solana-rpc.publicnode.com": [10.0, now]}))
    monkeypatch.setenv("SOLANA_WS_URL", "wss://paid.example.com/?api_key=k")
    f = SolanaTradeFeed(memory_path=mem)
    assert f.n_configured == 1 and f.ws_urls[f.ws_idx].startswith("wss://paid.example.com")
    mem.write_text(json.dumps({"paid.example.com": [3.0, now], "api.mainnet-beta.solana.com": [1.4, now]}))
    g = SolanaTradeFeed(memory_path=mem)                     # once measured, the fastest wins as before
    assert g.ws_urls[g.ws_idx].endswith("api.mainnet-beta.solana.com")
