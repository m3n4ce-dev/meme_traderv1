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


def test_live_chart_marks_your_trades_and_wallets_worth_seeing(tmp_path):
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    e = market()
    s = live_coin(e)
    assert asyncio.run(e.manual_buy(s.mint, 0.1)) == ""
    smart = "S" * 44
    e._study_cache = (time.time(), {smart: "study wallet (cluster 1)"})
    t, p = s.trades[-1][0], s.trades[-1][1]
    if s.creator:
        s.trades.append((t + 1, p, "sell", 0.4, s.creator))
    s.trades.append((t + 2, p, "buy", 3.0, "W" * 44))                # a whale
    s.trades.append((t + 3, p, "buy", 0.3, smart))
    d = e.chart_data(s.mint)
    who = {m["who"] for m in d["marks"]}
    assert {"you", "whale", "smart"} <= who and ("dev" in who or not s.creator)
    assert d["entry"] == e.positions[s.mint].entry_price and len(d["pts"]) > 2
    assert e.chart_data(s.mint, since=t + 3)["pts"] == []             # updates carry only new trades
    assert asyncio.run(e.manual_sell(s.mint, 1.0)) == ""
    d = e.chart_data(s.mint)
    assert any(m["who"] == "you" and m["side"] == "sell" for m in d["marks"])   # your past trade on it stays marked
    assert e.chart_data("nope") is None

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.send_json({"action": "chart_sub", "mints": [s.mint, "x", 5]})
            for _ in range(20):
                m = await ws.receive_json(timeout=5)
                if m.get("type") == "ticks":
                    return m
    m = asyncio.run(go())
    assert list(m["charts"]) == [s.mint] and m["charts"][s.mint]["full"] and m["charts"][s.mint]["pts"]


def test_kol_list_parses_and_names_kols_on_the_chart(tmp_path):
    import json as _json

    from meme_trader.sniper import kols

    k1, k2 = "K" * 43 + "a", "J" * 44
    rsc = ('[{"wallet_address":"%s","name":"Cook\\u00e9r","telegram":null,"twitter":"https://x.com/c","profit":146.9,'
           '"wins":45,"losses":8,"timeframe":1},{"wallet_address":"%s","name":"Zef","telegram":null,"twitter":null,"pfp":"x"}]' % (k1, k2))
    html = '<script>self.__next_f.push([1,%s])</script>' % _json.dumps(rsc)
    d = kols.parse(html)
    assert d["kols"] == {k1: "Cookér", k2: "Zef"} and d["board"][0]["wins"] == 45 and d["board"][0]["days"] == 1
    assert kols.load(tmp_path / "missing.json") == {"kols": {}}
    e = market()
    s = live_coin(e)
    e.kols = {"kols": {k1: "Cooker"}}
    s.trades.append((s.trades[-1][0] + 1, s.trades[-1][1], "buy", 0.5, k1))
    m = [x for x in e.chart_data(s.mint)["marks"] if x["who"] == "kol"]
    assert m and m[-1]["label"] == "KOL Cooker bought 0.50 SOL"


def test_strategy_buttons_save_the_owners_choice(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    e = market()
    saved = []
    e.save_setting = lambda k, v, path=None: saved.append((k, v))   # (never the real config/params.yaml)
    e.p.entry["enabled"] = True

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.send_json({"action": "set", "key": "entry.enabled", "value": False, "save": True})
            for _ in range(20):
                m = await ws.receive_json(timeout=5)
                if m.get("type") == "ack":
                    return m
    m = asyncio.run(go())
    assert m["ok"] and "Sniper off (saved)" in m["text"]
    assert e.p.entry.enabled is False and saved == [("entry.enabled", False)]   # survives the next restart


def test_coin_families_mark_the_og_and_number_the_copies():
    from meme_trader.sniper.events import Launch

    e = market(launches=5)
    t0 = e.now
    e.fam_since = t0 - 3600

    async def go():
        for i, (sym, name) in enumerate([("GIZMO", "Gizmo"), ("$gizmo", "Gizmo Cat"), ("GIZ", "gizmo!"), ("OTHER", "Other")]):
            await e.handle(Launch(mint=f"M{i}" + "x" * 40, ts=t0 + 60 * i, creator=f"C{i}" + "y" * 40, symbol=sym, name=name))
    asyncio.run(go())
    og, c2, c3, other = (e.family(f"M{i}" + "x" * 40, full=True) for i in range(4))
    assert other is None                                            # a coin with no namesakes has no family
    assert og["og"] and og["n"] == 3 and og["rank"] == 1 and og["og_known"]
    assert not c2["og"] and c2["rank"] == 2 and c2["og_symbol"] == "GIZMO" and c2["after_og_s"] == 60
    assert c3["rank"] == 3 and c3["after_og_s"] == 120              # same name, different ticker: still a copy
    assert {m["rank"] for m in og["members"]} == {1, 2, 3}
    row = e._pulse_token(e.tokens["M1" + "x" * 40], 100.0, False)
    assert row["fam"]["rank"] == 2 and row["fam"]["og"] is False
    e.now = t0 + 7 * 3600
    asyncio.run(e._tick())
    assert e.family("M0" + "x" * 40) is None                        # families forget after 6 h


def test_buy_and_give_to_the_bots(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    e = market()
    s = live_coin(e)

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.send_json({"action": "m_buy", "mint": s.mint, "sol": 0.1, "hand": True})
            for _ in range(20):
                m = await ws.receive_json(timeout=5)
                if m.get("type") == "ack":
                    return m
    m = asyncio.run(go())
    assert m["ok"] and "bots ride it" in m["text"]
    asyncio.run(e._tick())                                   # the fill has landed: the bots take it from here
    pos = e.positions[s.mint]
    assert pos.source == "manual" and pos.bot == "ride" and s.mint not in e.hand_after
    e.hand_after["gone" * 10] = e.now - 1000                 # a buy that never filled is forgotten
    asyncio.run(e._tick())
    assert "gone" * 10 not in e.hand_after


def test_market_cap_in_and_out_is_recorded_and_backfilled(tmp_path):
    import json as _json

    from meme_trader.sniper import mcapfill
    from meme_trader.sniper.analytics import by_entry_mcap

    e = market()
    s = live_coin(e)
    e.sol_price.usd = 150.0
    assert asyncio.run(e.manual_buy(s.mint, 0.1)) == ""
    row = next(p for p in e.snapshot()["positions"] if p["mint"] == s.mint)
    assert row["entry_mcap_sol"] == pytest.approx(s.market_cap_sol, rel=0.2) and row["mcap_sol"] == pytest.approx(s.market_cap_sol)
    assert asyncio.run(e.manual_sell(s.mint, 1.0)) == ""
    c = e.book.closed[-1]
    assert c["entry_mcap_sol"] > 0 and c["exit_mcap_sol"] > 0 and c["entry_mcap_usd"] == round(c["entry_mcap_sol"] * 150)
    assert by_entry_mcap([c])[0]["n"] == 1
    # an older trade (no market caps) gets them from the recorded feed
    old = {"mint": "M" * 44, "opened": 1000.0, "closed": 1100.0, "source": "manual", "pnl": 0.01, "pnl_pct": 10, "cost": 0.1}
    (tmp_path / "trades-2026-10-04.jsonl").write_text(_json.dumps(old) + "\n")
    feed = [{"mint": "M" * 44, "ts": 999.0, "v_sol": 40.0, "v_tokens": 800_000_000.0},
            {"mint": "M" * 44, "ts": 1099.0, "v_sol": 60.0, "v_tokens": 600_000_000.0}]
    (tmp_path / "feed-2026-10-04.jsonl").write_text("".join(_json.dumps(x) + "\n" for x in feed))
    r = mcapfill.run(tmp_path)
    assert r["filled"] == 1
    rows = [dict(old)]
    assert mcapfill.apply(rows, tmp_path, 100.0) == 1
    assert rows[0]["entry_mcap_sol"] == 50.0 and rows[0]["exit_mcap_sol"] == 100.0 and rows[0]["entry_mcap_usd"] == 5000


def test_kol_tracker_and_hot_names():
    from meme_trader.sniper.events import Launch, Trade

    e = market()
    k = "K" * 44
    e.kols = {"kols": {k: "Cooker"}}
    e._known_cache = None
    s = live_coin(e)
    t0 = s.trades[-1][0]

    async def go():
        for i, side in enumerate(("buy", "sell")):
            await e.handle(Trade(mint=s.mint, ts=t0 + 10 + 30 * i, trader=k, side=side, sol=1.5, tokens=1e6,
                                 v_sol=s.curve.v_sol, v_tokens=s.curve.v_tokens, new_balance=-1.0))
        for i in range(4):                                       # a copycat wave: 4 coins named PEPE
            await e.handle(Launch(mint=f"P{i}" + "z" * 42, ts=e.now + i, creator=f"C{i}" + "q" * 42, symbol="PEPE", name="Pepe"))
    asyncio.run(go())
    v = e.kol_view()
    assert [x["side"] for x in v["tape"]] == ["sell", "buy"] and v["tape"][0]["held_s"] == 30   # sold after 30 s
    c = v["coins"][0]
    assert c["mint"] == s.mint and c["names"] == ["Cooker"] and c["kols"] == 1 and c["buys"] == 1 and c["sells"] == 1
    hot = e.hot_names()
    assert hot and hot[0]["n"] >= 4 and hot[0]["og_symbol"] == "PEPE"



def test_bot_trades_keep_what_the_bot_saw_at_entry():
    """Research: each bot trade carries its entry features, so dumps can be told from winners later."""
    e = market(launches=40)
    s = max((s for s in e.tokens.values() if not s.migrated and s.curve.progress < 0.9 and s.price_known), key=lambda s: s.curve.progress)
    f = e._entry_features(s, "late")
    assert {"curve_progress_pct", "net_flow_sol_window", "buyers_window", "top10_holders_pct", "family"} <= set(f)
    assert f["family"] in ("og", "copy", "alone")

    async def go():
        await e._buy(s, 60, 0.05, ["t"], source="late")
    asyncio.run(go())
    pos = e.positions.get(s.mint)
    assert pos is not None and pos.feat and pos.feat["curve_progress_pct"] == f["curve_progress_pct"]
