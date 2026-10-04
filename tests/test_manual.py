"""Manual trading from the dashboard: buy / ape / sell part / initials / exit, and manual exit rules."""
import asyncio
import copy

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed, SyntheticFeed
from meme_trader.sniper.strategy import SniperPosition, evaluate_manual_exit, initials_fraction

P = config.load(config.EXAMPLE)


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def market(launches=40, **exe):
    p = copy.deepcopy(P)
    p.sniper.execution.update(paper_delay_s=0, **exe)
    for k in ("entry", "late", "copy", "callouts"):
        p.sniper[k]["enabled"] = False                     # only our own trades
    e = Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)

    async def go():
        async for ev in SyntheticFeed(seed=3, speed=0, launches=launches, start_ts=1_780_000_000).events():
            await e.handle(ev)
    asyncio.run(go())
    return e


def live_coin(e):
    return next(s for s in e.tokens.values() if s.price_known and not s.migrated and s.curve.progress < 0.8)


def test_manual_buy_skips_pause_and_seats_but_not_the_kill_switch():
    e = market()
    s = live_coin(e)
    e.paused = True
    e.p.capital["max_open_positions"] = 0
    assert asyncio.run(e.manual_buy(s.mint, 0.1)) == ""
    pos = e.positions[s.mint]
    assert pos.source == "manual" and pos.initial_cost_sol == pytest.approx(0.1, rel=0.05)
    assert "already holding" in asyncio.run(e.manual_buy(s.mint, 0.1))
    other = next(x for x in e.tokens.values() if x.price_known and not x.migrated and x.mint != s.mint)
    assert "at most" in asyncio.run(e.manual_buy(other.mint, 50))
    e.book.halted = "manual kill switch"
    assert asyncio.run(e.manual_buy(other.mint, 0.1)).startswith("halted")


def test_queued_buy_fills_on_the_first_price():
    from meme_trader.sniper.events import Trade

    e = market()
    mint = "Q" * 40 + "pump"
    assert asyncio.run(e.manual_buy(mint, 0.1)) == "" and mint in e.manual_queue and mint not in e.positions

    async def go():
        t = Trade(mint=mint, ts=e.now + 1, trader="T" * 44, side="buy", sol=0.5, tokens=1.7e7, v_sol=31.0,
                  v_tokens=1.03e9)
        await e.handle(t)
        await e._manual_queue_tick()
    asyncio.run(go())
    assert mint in e.positions and mint not in e.manual_queue


def test_initials_and_manual_sells():
    e = market()
    s = live_coin(e)
    asyncio.run(e.manual_buy(s.mint, 0.2))
    pos = e.positions[s.mint]
    assert "not enough profit" in asyncio.run(e.take_initials(s.mint))      # flat: initials = everything
    s.curve.v_sol *= 1.8                                                     # price up 1.8x
    frac = initials_fraction(pos, s.curve.price, e.fee)
    assert 0.5 < frac < 0.65                                                 # ~1/1.8 plus fees
    assert asyncio.run(e.take_initials(s.mint)) == ""
    pos = e.positions[s.mint]
    assert pos.initials_taken and pos.proceeds_sol >= pos.initial_cost_sol * 0.98
    assert asyncio.run(e.manual_sell(s.mint, 0.5)) == "" and s.mint in e.positions
    assert asyncio.run(e.manual_sell(s.mint, 1)) == "" and s.mint not in e.positions
    assert "no open position" in asyncio.run(e.manual_sell(s.mint, 1))


def test_manual_exit_rules():
    class S:
        migrated = False

        class curve:
            price = 1.0
    pos = SniperPosition(mint="m", symbol="X", opened_at=0, entry_price=1.0, tokens=100.0, initial_tokens=100.0,
                         cost_sol=1.0, initial_cost_sol=1.0, score=50.0, peak_price=1.0, source="manual")
    assert evaluate_manual_exit(pos, S, {}) is None                          # nothing set: you manage it
    S.curve.price = 0.5
    assert evaluate_manual_exit(pos, S, {"sl": 40}) == (1.0, "manual stop -50%")
    S.curve.price = 2.2
    m = {"tp": 100, "tp_frac": 0.5}
    assert evaluate_manual_exit(pos, S, m)[0] == 0.5 and m["tp_done"]
    assert evaluate_manual_exit(pos, S, m) is None                            # take profit fires once
    S.curve.price = 1.5
    assert evaluate_manual_exit(pos, S, {"trail": 25})[1].startswith("manual trail")
    S.migrated = True
    assert evaluate_manual_exit(pos, S, {})[1] == "manual: graduated (curve only)"


def test_set_manual_exits_validates():
    e = market()
    s = live_coin(e)
    asyncio.run(e.manual_buy(s.mint, 0.1))
    assert e.set_manual_exits(s.mint, sl=30, tp=100, trail="") == ""
    assert e.positions[s.mint].manual == {"sl": 30.0, "tp": 100.0, "tp_done": False}
    assert "between" in e.set_manual_exits(s.mint, sl=150)
    assert "no open position" in e.set_manual_exits("nope", sl=10)


def test_dashboard_actions(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    e = market()
    s = live_coin(e)

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.receive_json()

            async def ack(cmd):
                await ws.send_json(cmd)
                while (m := await ws.receive_json())["type"] != "ack":
                    pass
                return m
            out = {"bad": await ack({"action": "m_buy", "mint": "nope", "sol": 0.1}),
                   "ape": await ack({"action": "m_ape", "mint": s.mint}),
                   "exits": await ack({"action": "m_exits", "mint": s.mint, "sl": 30, "tp": "", "trail": ""}),
                   "sell": await ack({"action": "m_sell", "mint": s.mint, "fraction": 0.5}),
                   "note": await ack({"action": "mem_add", "text": "a note for the desk", "note": "mine"})}
            await ws.close()
            return out
    out = asyncio.run(go())
    assert not out["bad"]["ok"] and "contract address" in out["bad"]["text"]
    assert out["ape"]["ok"] and "Aped" in out["ape"]["text"]
    assert out["exits"]["ok"] and out["sell"]["ok"]
    assert e.positions[s.mint].manual["sl"] == 30 and e.positions[s.mint].tokens < e.positions[s.mint].initial_tokens
    assert out["note"]["ok"] and out["note"]["memory"]["items"][0]["note"] == "mine"
