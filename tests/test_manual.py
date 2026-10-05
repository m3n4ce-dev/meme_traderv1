"""Manual trading from the dashboard: buy / add / sell part / initials / exit, manual exit rules, hand-over."""
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
    assert asyncio.run(e.manual_buy(s.mint, 0.1)) == "" and e.positions[s.mint].adds   # a second buy adds to it
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
    assert evaluate_manual_exit(pos, S, {})[1] == "manual: graduated (live sells at graduation)"


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
                   "buy": await ack({"action": "m_buy", "mint": s.mint, "sol": 0.1}),
                   "exits": await ack({"action": "m_exits", "mint": s.mint, "sl": 30, "tp": "", "trail": ""}),
                   "sell": await ack({"action": "m_sell", "mint": s.mint, "fraction": 0.5}),
                   "note": await ack({"action": "mem_add", "text": "a note for the desk", "note": "mine"})}
            await ws.close()
            return out
    out = asyncio.run(go())
    assert not out["bad"]["ok"] and "contract address" in out["bad"]["text"]
    assert out["buy"]["ok"] and "Buy 0.1 SOL sent" in out["buy"]["text"]
    assert out["exits"]["ok"] and out["sell"]["ok"]
    assert e.positions[s.mint].manual["sl"] == 30 and e.positions[s.mint].tokens < e.positions[s.mint].initial_tokens
    assert out["note"]["ok"] and out["note"]["memory"]["items"][0]["note"] == "mine"


def test_buying_a_coin_you_hold_adds_to_the_position():
    """Owner: 'I want to be able to add more funds to already open positions' (it used to refuse)."""
    e = market()
    s = live_coin(e)
    assert asyncio.run(e.manual_buy(s.mint, 0.1)) == ""
    pos = e.positions[s.mint]
    t1, c1, p1 = pos.tokens, pos.initial_cost_sol, pos.entry_price
    s.curve.v_sol *= 1.5                                       # price up 50%, then add more
    cash = e.book.sol
    assert asyncio.run(e.manual_buy(s.mint, 0.2)) == ""
    pos = e.positions[s.mint]
    assert pos.initial_cost_sol == pytest.approx(c1 + 0.2, rel=0.02) and pos.tokens > t1
    assert p1 < pos.entry_price < s.curve.price                 # token-weighted average entry
    assert e.book.sol == pytest.approx(cash - 0.2, rel=0.02) and len(pos.adds) == 1
    assert asyncio.run(e.manual_sell(s.mint, 1)) == ""
    row = e.book.closed[-1]
    assert row["cost"] == pytest.approx(c1 + 0.2, rel=0.02)      # P&L counts both buys
    assert "at most" in asyncio.run(e.manual_buy(live_coin(e).mint, 50))


def test_adds_land_late_in_paper_too():
    e = market()
    e.p.execution["paper_delay_s"] = 2.0
    s = live_coin(e)

    async def go():
        assert await e.manual_buy(s.mint, 0.1) == ""
        e.now += 3
        await e._settle_deferred()
        first = e.positions[s.mint].initial_cost_sol
        assert await e.manual_buy(s.mint, 0.1) == ""
        assert "in flight" in await e.manual_buy(s.mint, 0.1)  # one order at a time per coin
        e.now += 3
        await e._settle_deferred()
        return first, e.positions[s.mint]
    first, pos = asyncio.run(go())
    assert pos.initial_cost_sol == pytest.approx(first + 0.1, rel=0.03) and len(pos.adds) == 1


def test_hand_your_positions_to_the_bots_and_take_them_back():
    """Owner: 'tell the agents to take over certain or all positions including manual ones in case I need to leave'."""
    e = market()
    a = live_coin(e)
    assert asyncio.run(e.manual_buy(a.mint, 0.1)) == ""
    b = next(x for x in e.tokens.values() if x.price_known and not x.migrated and x.mint != a.mint and x.curve.progress < .8)
    assert asyncio.run(e.manual_buy(b.mint, 0.1)) == ""
    a.curve.v_sol *= 0.5                                       # down hard: your rules (none set) keep holding
    asyncio.run(e._check_exit(a))
    assert a.mint in e.positions
    n, err = e.hand_over(a.mint, True)
    assert n == 1 and not err and e.positions[a.mint].bot == "ride" and e.positions[a.mint].handed_price > 0
    assert "already handed over" in e.hand_over(a.mint, True)[1]
    asyncio.run(e._check_exit(a))
    assert a.mint in e.positions                               # no instant sell: they ride from here
    a.curve.v_sol *= 0.5                                       # half again
    asyncio.run(e._check_exit(a))                              # 40% under the hand-over price: their stop sells it
    assert a.mint not in e.positions
    assert "Away mode on" in e.set_away(True) and e.away
    assert e.positions[b.mint].bot                             # away: everything you hold goes to the bots
    c = next(x for x in e.tokens.values() if x.price_known and not x.migrated and x.mint not in (a.mint, b.mint) and x.curve.progress < .8)
    asyncio.run(e.manual_buy(c.mint, 0.1))
    assert e.positions[c.mint].bot                             # and anything you open while away
    assert "back" in e.set_away(False).lower()
    assert not any(p.bot for p in e.positions.values())


def test_restart_button_saves_and_restarts(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    e = market()
    called = []

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path, restarter=lambda eng: called.append(eng)))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.receive_json()
            await ws.send_json({"action": "restart"})
            while (m := await ws.receive_json())["type"] != "ack":
                pass
            await asyncio.sleep(0.8)
            await ws.close()
            return m
    m = asyncio.run(go())
    assert m["ok"] and m["restarting"] and called == [e]


def test_the_owner_can_lift_the_kill_switch_and_it_counts_from_there(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    e = market()
    start = e.book.start_sol
    e.book.sol = start * 0.55                                   # 45% down, nothing open
    asyncio.run(e._tick())
    assert e.book.halted.startswith("drawdown")
    assert e.clear_halt("agent") and e.book.halted                # never the agent's call
    assert e.clear_halt("dashboard") == "" and not e.book.halted
    assert e.book.kill_base == pytest.approx(start * 0.55)       # re-measured from today's equity...
    assert e.kill_at() == pytest.approx(start * 0.55 * (1 - e.p.capital.max_drawdown_pct / 100))
    asyncio.run(e._tick())
    assert not e.book.halted                                     # ...so it doesn't trip again at once
    e.deposit_paper(1.0)
    assert e.book.kill_base == pytest.approx(start * 0.55 + 1)   # a top-up moves the line with the cash
    e.book.sol = e.kill_at() - 0.01
    asyncio.run(e._tick())
    assert e.book.halted                                          # and it still trips past the new line
    e.persist = True
    e.save_state()
    e2 = market(launches=1)
    asyncio.run(e2.restore_state())
    assert e2.book.kill_base == pytest.approx(e.book.kill_base)

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.receive_json()
            await ws.send_json({"action": "unhalt"})
            while (m := await ws.receive_json())["type"] != "ack":
                pass
            await ws.close()
            return m
    m = asyncio.run(go())
    assert m["ok"] and "trips again at" in m["text"] and not e.book.halted


def test_graduated_coins_trade_on_paper_at_the_pool_price_and_live_says_why_not():
    """Owner hit 'it has graduated off the bonding curve' buying from Pulse's Graduated column."""
    e = market()
    e.p.execution["paper_delay_s"] = 2.0
    s = live_coin(e)
    s.migrated = True                                    # left the curve; the price now comes from its pool

    async def go():
        assert await e.manual_buy(s.mint, 0.1) == ""
        e.now += 3
        await e._settle_deferred()                       # the delayed paper order still lands on a graduated coin
        held = s.mint in e.positions
        assert await e.manual_sell(s.mint, 1.0) == ""
        e.now += 3
        await e._settle_deferred()
        return held
    assert asyncio.run(go()) and s.mint not in e.positions
    e2 = market(launches=1)
    s2 = live_coin(e2)
    s2.migrated = True
    e2.mode = "live"
    assert "PumpSwap" in asyncio.run(e2.manual_buy(s2.mint, 0.1))


def test_the_bots_ride_a_handed_position_for_a_runner_instead_of_selling_at_once():
    """Owner: 'whenever I hand over to the bots they pretty much sell instantly. I'm trying to catch 2xs.'"""
    from types import SimpleNamespace

    from meme_trader.sniper.engine import RIDE_DEFAULTS
    from meme_trader.sniper.strategy import evaluate_ride_exit

    pos = SniperPosition(mint="m", symbol="X", opened_at=0, entry_price=1.0, tokens=100, initial_tokens=100, cost_sol=0.1,
                         initial_cost_sol=0.1, score=50, peak_price=1.2, source="manual", bot="ride", handed_price=1.2, handed_peak=1.2)
    tok = SimpleNamespace(curve=SimpleNamespace(price=1.0), migrated=False)
    step = lambda px, live=False: (setattr(tok.curve, "price", px), evaluate_ride_exit(pos, tok, RIDE_DEFAULTS, live))[1]
    assert step(1.05) is None and step(0.9) is None and step(1.3) is None        # chop, no time or stall exit
    frac, why = step(2.05)
    assert frac == 0.5 and "2x" in why and pos.ride_tp                          # half out at 2x
    assert step(2.6) is None and step(2.0) is None                              # the rest runs...
    assert step(1.8)[1].startswith("bots: trail")                               # ...until it gives back 30% of its peak
    pos2 = SniperPosition(mint="n", symbol="Y", opened_at=0, entry_price=1.0, tokens=100, initial_tokens=100, cost_sol=0.1,
                          initial_cost_sol=0.1, score=50, peak_price=1.0, source="manual", bot="ride", handed_price=0.8, handed_peak=0.8)
    tok.curve.price = 0.5
    assert evaluate_ride_exit(pos2, tok, RIDE_DEFAULTS) is None                 # 40% under the hand-over price is 0.48
    tok.curve.price = 0.47
    assert evaluate_ride_exit(pos2, tok, RIDE_DEFAULTS)[1].startswith("bots: stop")
    tok.migrated, tok.curve.price = True, 0.9
    assert evaluate_ride_exit(pos2, tok, RIDE_DEFAULTS) is None                 # graduated: paper keeps riding
    assert "graduated" in evaluate_ride_exit(pos2, tok, RIDE_DEFAULTS, live=True)[1]


def test_the_exit_manager_describes_your_positions_by_your_rules_not_the_bots():
    """The Desk said a manual position's 'closest exit is time held (12 min of 30 min)': yours have no time exit."""
    e = market()
    a = live_coin(e)
    assert asyncio.run(e.manual_buy(a.mint, 0.1)) == ""
    hold = {h["mint"]: h for h in e.desk_view()["holding"]}
    assert hold[a.mint]["manual"] and not any("time" in g["label"] for g in hold[a.mint]["watch"])
    e.hand_over(a.mint, True)
    labels = [g["label"] for g in {h["mint"]: h for h in e.desk_view()["holding"]}[a.mint]["watch"]]
    assert labels[0] == "stop" and any("2x" in x for x in labels) and "trailing stop" in labels
