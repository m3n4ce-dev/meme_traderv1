"""Regression tests for the line-by-line review findings (2026-10-03)."""
import asyncio
import copy
import types

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Social, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed, PumpPortalFeed
from meme_trader.sniper.funding import FundingResolver
from meme_trader.sniper.strategy import SniperPosition
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
MINT = "R" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    def __init__(self, realtime=False):
        self.realtime = realtime

    async def events(self):
        return
        yield


def engine(realtime=False):
    p = copy.deepcopy(P)
    return Engine(p, Quiet(realtime), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)


def held(eng):
    s = eng.tokens[MINT] = TokenState(MINT, Launch(MINT, 0, "dev"), 0)
    s.on_launch(s.launch)
    pos = eng.positions[MINT] = SniperPosition(MINT, "R", 0, s.curve.price, 1e6, 1e6, 0.05, 0.05, 70,
                                               peak_price=s.curve.price, exits=[])
    return s, pos


def test_dashboard_websocket_rejects_foreign_origins():
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    eng = engine()

    async def go():
        async with TestClient(TestServer(make_app(eng))) as c:
            host = f"127.0.0.1:{c.port}"
            bad = await c.get("/ws", headers={"Origin": "https://evil.example", "Host": host})
            rebound = await c.get("/ws", headers={"Origin": "http://evil.example:8787", "Host": "evil.example:8787"})
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            assert (await ws.receive_json())["type"] == "hello"        # the page's version check comes first
            snap = await ws.receive_json()
            await ws.close()
            return bad.status, rebound.status, snap
    bad, rebound, snap = asyncio.run(go())
    assert bad == 403 and rebound == 403 and "summary" in snap


def test_manual_sell_never_doubles_an_in_flight_sell_and_close_is_idempotent():
    eng = engine()
    s, pos = held(eng)
    eng.pending.add(MINT)                     # a tick is already selling it
    asyncio.run(eng.sell_now(MINT))
    asyncio.run(eng.kill())
    assert MINT in eng.positions and pos.proceeds_sol == 0
    eng.pending.clear()
    asyncio.run(eng.sell_now(MINT))
    assert MINT not in eng.positions
    eng._close(pos, s)                        # second close: no KeyError, no double row
    assert len(eng.book.closed) == 1


def test_buy_never_stacks_a_second_position_on_one_mint():
    eng = engine()
    s, pos = held(eng)
    asyncio.run(eng._buy(s, 50, 0.05, [], "callout"))
    assert eng.positions[MINT] is pos and eng.book.sol == eng.book.start_sol


def test_live_engine_survives_an_internal_error(monkeypatch):
    eng = engine(realtime=True)

    async def boom(e):
        raise ValueError("bad event")
    monkeypatch.setattr(eng, "_on_trade", boom)
    asyncio.run(eng.handle(Trade(MINT, 1, "w", "buy", 1, 1, 31, 1e9)))     # must not raise
    assert any("internal error" in x["text"] for x in eng.log)


def test_pending_sells_are_not_double_counted_as_positions():
    eng = engine()
    held(eng)
    eng.pending.add(MINT)
    eng.p["capital"]["max_open_positions"] = 2
    assert eng.entries_blocked() == ""        # 1 position (being sold) of 2


def test_calls_on_unpriced_tokens_are_scored_from_the_first_real_price():
    eng = engine()
    asyncio.run(eng.handle(Social(MINT, 1, "telegram", "caller1")))
    assert not eng.callers.stats.get("telegram:caller1") or not eng.callers.stats["telegram:caller1"].pending
    asyncio.run(eng.handle(Trade(MINT, 2, "w", "buy", 1, 1e6, 60.0, 5.0e8)))
    st = eng.callers.stats["telegram:caller1"]
    assert st.pending and st.pending[0][2] == pytest.approx(60.0 / 5.0e8)


def test_pumpportal_parse_tolerates_null_fields():
    t = PumpPortalFeed.parse({"txType": "buy", "mint": MINT, "traderPublicKey": "w", "solAmount": 1,
                              "tokenAmount": 5, "newTokenBalance": None, "vSolInBondingCurve": None}, 1)
    assert t.new_balance == -1.0 and t.v_sol == 0


def test_funding_rpc_errors_are_not_cached_as_unknown(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://rpc.example")

    class Resp:
        status = 429

        async def json(self, content_type=None):
            return {"error": {"code": 429, "message": "rate limited"}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    session = types.SimpleNamespace(post=lambda *a, **k: Resp())
    r = FundingResolver("rpc", 100)
    assert asyncio.run(r.lookup(session, "W" * 44)) is None     # None = retry later, not "no funder"


def test_tx_deltas_reads_the_transaction_not_the_wallet(monkeypatch):
    from meme_trader import wallet as wmod

    w = wmod.Wallet.__new__(wmod.Wallet)
    w.pubkey = "ME"
    tx = {"meta": {"err": None, "preBalances": [1_000_000_000, 0, 5], "postBalances": [897_960_720, 2_039_280, 5],
                   "preTokenBalances": [],
                   "postTokenBalances": [{"accountIndex": 1, "mint": MINT, "owner": "ME",
                                          "uiTokenAmount": {"amount": "7000000"}}]},
          "transaction": {"message": {"accountKeys": [{"pubkey": "ME"}, {"pubkey": "ATA"}, {"pubkey": "X"}]}}}
    monkeypatch.setattr(wmod, "rpc", lambda m, p: tx)
    d = w.tx_deltas("sig", MINT)
    assert d["dtok"] == 7_000_000 and d["rent"] == pytest.approx(0.00203928) and not d["failed"]
    assert abs(d["dsol"]) - d["rent"] == pytest.approx(0.1)        # what the buy actually cost


def test_restored_position_gets_a_fallback_price_on_the_curve(monkeypatch):
    eng = engine(realtime=True)
    s, pos = held(eng)
    s.price_known = False
    px = 1.2e-7
    monkeypatch.setattr("meme_trader.clients.dexscreener.best_pair_by_mint",
                        lambda mints: {MINT: {"priceNative": str(px), "dexId": "pumpfun"}})
    eng.now = 100
    asyncio.run(eng._price_fallback())
    assert s.price_known and s.curve.price == pytest.approx(px) and 0 < s.curve.progress < 1
