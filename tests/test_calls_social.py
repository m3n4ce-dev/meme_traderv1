"""The call ledger (tamper-evident, scored honestly), limit orders and alerts, the Pulse board, the desk's replies
to your notes, and posting to X and Telegram (signing, costs, limits; never without a click)."""
import asyncio
import copy
import json
import time

import pytest

from meme_trader import config
from meme_trader.sniper.calls import GENESIS, CallLedger, LedgerError, chain_hash
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed, SyntheticFeed

P = config.load(config.EXAMPLE)
MINT = "C" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def market(launches=40):
    p = copy.deepcopy(P)
    p.sniper.execution.update(paper_delay_s=0)
    for k in ("entry", "late", "copy", "callouts"):
        p.sniper[k]["enabled"] = False
    e = Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)

    async def go():
        async for ev in SyntheticFeed(seed=3, speed=0, launches=launches, start_ts=1_780_000_000).events():
            await e.handle(ev)
    asyncio.run(go())
    return e


def live_coin(e, skip=()):
    return next(s for s in e.tokens.values() if s.price_known and not s.migrated and s.curve.progress < 0.8
                and s.mint not in skip)


# ---------------------------------------------------------------- the ledger
def test_ledger_chain_detects_any_rewrite(tmp_path):
    L = CallLedger(tmp_path / "calls.jsonl")
    a = L.call(MINT, "AAA", "you", 50_000, 1e-7, 150, thesis="fresh buyers", ts=1000)
    b = L.call("D" * 40 + "pump", "BBB", "bot", 80_000, 2e-7, 150, ts=2000)
    assert a["prev"] == GENESIS and b["prev"] == a["hash"] and b["hash"] == chain_hash(a["hash"], b)
    L.anchor("x", "https://x.com/i/status/1")
    assert L.verify()["ok"] and CallLedger(tmp_path / "calls.jsonl").verify()["records"] == 3
    lines = (tmp_path / "calls.jsonl").read_text().splitlines()
    for bad in ([lines[0].replace("50000", "40000")] + lines[1:],      # a better entry after the fact
                lines[1:],                                             # the first call deleted
                [lines[1], lines[0], lines[2]]):                       # reordered
        (tmp_path / "t.jsonl").write_text("\n".join(bad) + "\n")
        v = CallLedger(tmp_path / "t.jsonl").verify()
        assert not v["ok"] and v["broken_at"] == 1
    with pytest.raises(LedgerError):
        L.call(MINT, "AAA", "you", 0, 0, 150)


def test_two_writers_keep_one_chain(tmp_path):
    """The bot and the command line both append (a call, a published proof): neither forks the chain."""
    bot, cli = CallLedger(tmp_path / "calls.jsonl"), CallLedger(tmp_path / "calls.jsonl")
    bot.call(MINT, "AAA", "bot", 50_000, 1e-7, 150)
    cli.anchor("telegram")                                     # the CLI's copy didn't know about the bot's call
    bot.call(MINT, "AAA", "you", 60_000, 1e-7, 150)
    v = CallLedger(tmp_path / "calls.jsonl").verify()
    assert v["ok"] and v["records"] == 3


def test_ledger_scores_fixed_horizons_after_costs_and_the_peak():
    L = CallLedger(None)
    t0 = 10_000.0
    c = L.call(MINT, "AAA", "you", 100_000, 1e-7, 150, ts=t0)
    for dt, mc in ((60, 180_000), (300, 150_000), (1800, 400_000), (3600, 120_000), (90_000, 50_000)):
        L.observe(c["id"], t0 + dt, mc)
    row = L.rows()[0]
    assert row["peak_x"] == pytest.approx(4.0) and row["5m"] == pytest.approx(50) and row["1h"] == pytest.approx(20)
    assert row["24h"] == pytest.approx(-50) and row["low_pct"] == pytest.approx(0)   # the 25 h sample is past tracking
    st = L.stats(now=t0 + 90_000)
    assert st["1h"]["median_pct"] == pytest.approx(20 - st["cost_pct"]) and st["peak"]["hit_rate"] == 1.0
    assert L.open_calls(t0 + 100) and not L.open_calls(t0 + 90_000)


# ---------------------------------------------------------------- engine: calls, orders, alerts, Pulse
def test_bot_entries_and_your_calls_go_on_the_record():
    e = market()
    s = live_coin(e)
    rec = asyncio.run(e.make_call(s.mint, "volume coming in"))
    assert rec["caller"] == "you" and rec["mcap_usd"] == pytest.approx(s.market_cap_sol * e.sol_price.usd)
    assert s.mint in e._pinned()                                # kept priced while the call is scored
    e._ledger_tick()
    assert e.ledger.rows()[0]["now_x"] == pytest.approx(1.0)
    e.feed.realtime = True                                      # only the live bot records its own entries
    other = live_coin(e, {s.mint})
    asyncio.run(e._buy(other, 60, 0.1, ["late play"], source="late"))
    assert [c["caller"] for c in e.ledger.calls()] == ["you", "bot"] and e.ledger.verify()["ok"]


def test_limit_orders_and_alerts():
    e = market()
    s = live_coin(e)
    usd = e.sol_price.usd
    mc = s.market_cap_sol * usd
    err, _ = asyncio.run(e.place_order(s.mint, "buy", "le", mc * 0.8, sol=50))
    assert "at most" in err
    assert "no open position" in asyncio.run(e.place_order(s.mint, "sell", "ge", mc * 2, frac=0.5))[0]
    err, dip = asyncio.run(e.place_order(s.mint, "buy", "le", mc * 0.8, sol=0.1))
    _, alert = asyncio.run(e.place_order(s.mint, "alert", "ge", mc * 1.5))
    assert not err and dip["mcap_at_place"] == pytest.approx(mc)
    asyncio.run(e._orders_tick())
    assert dip["status"] == alert["status"] == "open" and s.mint not in e.positions
    s.curve.v_sol *= 0.7                                        # price dips 30%: the limit buy fires
    asyncio.run(e._orders_tick())
    assert dip["status"] == "done" and s.mint in e.positions
    _, tp = asyncio.run(e.place_order(s.mint, "sell", "ge", mc * 1.4, frac=1))
    s.curve.v_sol *= 2.4                                        # up past both: the alert and the take profit
    asyncio.run(e._orders_tick())
    assert alert["status"] == "done" and tp["status"] == "done" and s.mint not in e.positions
    assert any("🔔" in line["text"] for line in e.log)
    _, late = asyncio.run(e.place_order(s.mint, "alert", "le", 1))
    assert "between" in late if isinstance(late, str) else late is None
    _, gone = asyncio.run(e.place_order(s.mint, "alert", "le", 200, ttl_h=0.1))
    e.now += 400
    asyncio.run(e._orders_tick())
    assert gone["status"] == "expired" and e.cancel_order(gone["id"]) == "no open order with that id"


def test_orders_survive_a_restart(tmp_path):
    e = market()
    e.persist = True
    s = live_coin(e)
    _, o = asyncio.run(e.place_order(s.mint, "alert", "ge", 10_000_000))
    e.save_state()
    p2 = copy.deepcopy(P)
    e2 = Engine(p2, Quiet(), PaperExecutor(p2.sniper.execution), mode="paper", log_to_journal=False, persist=True)
    asyncio.run(e2.restore_state())
    assert o["id"] in e2.orders and s.mint in e2.tokens and s.mint in e2._pinned()


def test_pulse_board_columns():
    e = market()
    v = e.pulse_view()
    assert v["new"] and all(r["progress"] < 0.5 for r in v["new"]) and all(r["progress"] >= 0.5 for r in v["stretch"])
    ages = [r["age"] for r in v["new"]]
    assert ages == sorted(ages)                                 # newest first
    row = v["new"][0]
    assert {"mcap_usd", "top10", "dev_pct", "bundle_pct", "flow30", "lk", "spark"} <= set(row)
    assert all("graduated_s" in g for g in v["graduated"])


# ---------------------------------------------------------------- the desk replies to every note
def test_every_persona_replies_to_a_note_and_answers_follow_ups(monkeypatch, tmp_path):
    from meme_trader.sniper import desk as deskmod
    from meme_trader.sniper.memory import Memory

    e = market()
    e.memory = Memory(tmp_path / "memory.json")
    it = asyncio.run(e.memory.add("Watch AI agent coins tonight", "my hunch"))
    seen = []

    briefs = []

    async def fake(client, p, persona, item, live=None, brief=None):
        seen.append((persona, [m["who"] for m in item.get("thread") or []]))
        briefs.append(brief)
        return {"reply": f"{persona} take", "stance": "neutral", "input_tokens": 10, "output_tokens": 5}
    monkeypatch.setattr(deskmod, "reply_note", fake)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "API key" in asyncio.run(e.discuss_note(it["id"]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert asyncio.run(e.discuss_note(it["id"])) == ""
    th = e.memory.items_[0]["thread"]
    assert [m["who"] for m in th] == list(e.p.desk.personas) and e.note_stats["calls"] == 4
    b = briefs[-1]                                       # they answer questions from the bot's real state
    assert {"edge_check_14d", "top_rejections", "settings", "recent_trades", "recent_notes", "entries_blocked"} <= set(b)
    assert "error" not in b
    e.memory.add_reply(it["id"], "you", "@skeptic what could go wrong?")
    asyncio.run(e.discuss_note(it["id"], ["skeptic"]))
    assert e.memory.items_[0]["thread"][-1]["who"] == "skeptic" and seen[-1][1][-1] == "you"
    assert json.loads((tmp_path / "memory.json").read_text())[0]["thread"][-2]["who"] == "you"


# ---------------------------------------------------------------- X and Telegram
def test_oauth1_signature_matches_xs_documented_example():
    """The worked example in X's 'Creating a signature' docs."""
    from meme_trader.ui.social import oauth1_header

    h = oauth1_header("POST", "https://api.twitter.com/1.1/statuses/update.json",
                      {"status": "Hello Ladies + Gentlemen, a signed OAuth request!", "include_entities": "true"},
                      "xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
                      "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
                      nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958")
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in h


def test_post_costs_lengths_and_guards(tmp_path, monkeypatch):
    from meme_trader.ui.social import LINK_POST_USD, POST_USD, Social, SocialError, x_cost, x_length

    assert x_cost("gm") == POST_USD and x_cost("CA: " + MINT) == POST_USD
    assert x_cost("chart https://dexscreener.com/x") == LINK_POST_USD and x_cost("see pump.fun/coin/abc") == LINK_POST_USD
    assert x_length("a https://example.com/very/long/link b") == 2 + 23 + 2 and x_length("🚀") == 2
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)
    soc = Social(tmp_path)
    st = soc.status()
    assert not st["x"]["connected"] and not st["telegram"]["configured"]
    res = asyncio.run(soc.post("gm", None, ("x", "telegram")))
    assert "isn't connected" in res["x"]["error"] and "isn't set up" in res["telegram"]["error"]
    with pytest.raises(SocialError):
        asyncio.run(soc.post_x("x" * 300))
    with pytest.raises(SocialError):
        soc.oauth2_start("http://127.0.0.1:8787/x/callback")       # no client id yet
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    url = soc.oauth2_start("http://127.0.0.1:8787/x/callback")
    assert url.startswith("https://x.com/i/oauth2/authorize?") and "code_challenge_method=S256" in url
    assert "tweet.write" in url and "offline.access" in url
    with pytest.raises(SocialError):
        asyncio.run(soc.oauth2_finish("code", "forged-state"))
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        monkeypatch.setenv(k, "k")
    assert soc.status()["x"]["method"] == "keys"
    with (tmp_path / "social_posts.jsonl").open("w") as f:          # today's cap reached
        for _ in range(25):
            f.write(json.dumps({"ts": time.time(), "to": "x", "ok": True, "cost_usd": 0.015}) + "\n")
    with pytest.raises(SocialError, match="daily X limit"):
        asyncio.run(soc.post_x("one more"))
    assert soc.status()["x"]["spent_today_usd"] == pytest.approx(0.375)


def test_cards_render_and_say_paper():
    from meme_trader.ui import cards

    png = cards.trade_card({"symbol": "AAA", "pnl": 0.2, "pnl_pct": 40, "peak_gain_pct": 60, "opened": 0,
                            "closed": 90, "exit": "trail", "source": "late"}, None, "paper")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
    L = CallLedger(None)
    c = L.call(MINT, "AAA", "you", 50_000, 1e-7, 150)
    assert cards.call_card(L.rows()[0])[:4] == b"\x89PNG" and cards.record_card(L.stats(), L.head)[:4] == b"\x89PNG"
    assert c["n"] == 1


def test_dashboard_call_order_and_post_actions(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui import social
    from meme_trader.ui.server import make_app

    e = market()
    s = live_coin(e)
    posted = []

    async def fake_post(self, text, png=None, to=("x",)):
        posted.append((text, bool(png), to))
        return {ch: {"ok": True, "url": f"https://example/{ch}", "cost_usd": 0.015 if ch == "x" else 0} for ch in to}
    monkeypatch.setattr(social.Social, "post", fake_post)

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            H = {"Host": host}
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", **H})
            await ws.receive_json()

            async def act(**cmd):
                await ws.send_json(cmd)
                while (m := await ws.receive_json())["type"] != "ack":
                    pass
                return m
            m = await act(action="call", mint=s.mint, thesis="runner", to=["x", "telegram"])
            assert m["ok"] and "posted on X" in m["text"] and "posted on Telegram" in m["text"]
            assert posted[-1][1] and "CA: " + s.mint in posted[-1][0] and posted[-1][2] == ("x", "telegram")
            m = await act(action="anchor", to=["x"])
            assert m["ok"] and e.ledger.records[-1]["type"] == "anchor"
            m = await act(action="order_place", mint=s.mint, side="alert", op="ge", mcap_usd=10_000_000)
            assert m["ok"] and "alert" in m["text"]
            oid = next(iter(e.orders))
            assert (await act(action="order_cancel", id=oid))["ok"]
            m = await act(action="post", text="gm", to=["telegram"], card={"kind": "record"})
            assert m["ok"] and posted[-1] == ("gm", True, ("telegram",))
            r = await (await c.get("/api/calls", headers=H)).json()
            assert r["verify"]["ok"] and r["rows"][0]["symbol"] == s.symbol and r["stats"]["calls"] == 1
            png = await c.get(f"/api/card.png?kind=call&id={r['rows'][0]['id']}", headers=H)
            assert png.status == 200 and (await png.read())[:4] == b"\x89PNG"
            assert (await c.get("/api/card.png?kind=trade&id=nope", headers=H)).status == 404
            pulse = await (await c.get("/api/pulse", headers=H)).json()
            assert "stretch" in pulse
            exp = await c.get("/api/calls/export", headers=H)
            assert len((await exp.text()).splitlines()) == 2 and "attachment" in exp.headers["Content-Disposition"]
            st = await (await c.get("/api/social", headers=H)).json()
            assert "x" in st and "telegram" in st
            await ws.close()
    asyncio.run(go())


def test_exit_lab_runs_other_exits_on_the_same_entries(tmp_path):
    """Shadow positions run the bot's exit code with other settings on the same live prices."""
    from meme_trader.sniper.exitlab import ExitLab

    e = market()
    s = live_coin(e)
    lab = ExitLab(tmp_path / "exit_lab.jsonl", e.fee)
    t0 = e.now
    lab.start(s.mint, s.symbol, "late", s.curve.price, t0, e.p)
    assert s.mint in lab.mints() and len(lab.open[s.mint]) == 9
    s.curve.v_sol *= 1.5                                      # +50%: "take profit at 2x" waits, nothing stops out
    lab.tick(e.tokens, t0 + 5, e.p)
    s.curve.v_sol *= 1.5                                      # ~+125%: the 2x take profit sells
    lab.tick(e.tokens, t0 + 10, e.p)
    tp = [r for r in lab.done if r["variant"] == "take profit at 2x"]
    assert tp and tp[0]["pnl_pct"] > 90
    s.curve.v_sol *= 0.3                                      # crash: the stops close the rest
    lab.tick(e.tokens, t0 + 15, e.p)
    lab.tick(e.tokens, t0 + 2000, e.p)                        # anything left closes at the 30-minute limit
    assert not lab.open and len(lab.done) == 9
    assert any(r["variant"] == "bank half at +30%" for r in lab.done)
    v = lab.view("late")
    assert v["entries"] == 1 and v["variants"][0]["variant"] == "take profit at 2x"
    assert len((tmp_path / "exit_lab.jsonl").read_text().splitlines()) == 9
    assert len(ExitLab(tmp_path / "exit_lab.jsonl", e.fee).done) == 9     # reloads its history


def test_bot_entries_feed_the_exit_lab():
    e = market()
    e.feed.realtime = True
    s = live_coin(e)
    asyncio.run(e._buy(s, 60, 0.1, ["late play"], source="late"))
    assert s.mint in e.lab.open and s.mint in e._pinned()
    asyncio.run(e.manual_buy(live_coin(e, {s.mint}).mint, 0.1))
    assert len(e.lab.open) == 1                                # your own buys aren't shadowed


def test_the_ai_desks_passes_are_scored_on_the_bots_own_exits(tmp_path):
    """Owner: 'the bots should know their goal is to raise the account'. A pass costs money too when the coin runs."""
    from meme_trader.sniper import desk as deskmod
    from meme_trader.sniper.exitlab import ExitLab

    assert "grow the account" in deskmod.RUBRIC and "vetoing everything costs" in deskmod.PERSONAS["skeptic"]
    e = market()
    s = live_coin(e)
    t = live_coin(e, {s.mint})
    lab = ExitLab(tmp_path / "exit_lab.jsonl", e.fee)
    lab.start(s.mint, s.symbol, "desk-pass", s.curve.price, e.now, e.p, only=("as now",))
    lab.start(t.mint, t.symbol, "sniper", t.curve.price, e.now, e.p)
    assert len(lab.open[s.mint]) == 1                          # just the bot's own exits
    lab.tick(e.tokens, e.now + 2000, e.p)                       # both close at the time limit
    passed, sniper = lab.view("desk-pass"), lab.view("sniper")
    assert passed["entries"] == 1 and passed["variants"][0]["variant"] == "as now"
    assert all(r["kind"] != "desk-pass" for r in lab.done if r["mint"] == t.mint)
    assert sniper["entries"] == 1                              # the sniper view doesn't count desk passes


def test_the_wallet_study_view_groups_wallets_that_buy_together():
    """Two listed wallets that buy the same coins in the same seconds are one trader (or copy bots), not two."""
    from meme_trader.wallets import report
    from meme_trader.wallets.study import Data

    d = Data()
    for k in range(4):                                       # a and b buy 4 coins within 2 s of each other
        for w, dt in (("a" * 32, 0), ("b" * 32, 2), ("c" * 32, 900)):   # c buys the same coins 15 min later
            d.by_pool[f"pool{k}"].append((1000.0 + k * 3600 + dt, w, True, 0.3, 1e-6))
    groups, pairs = report.clusters(d, ["a" * 32, "b" * 32, "c" * 32], 0.05)
    assert groups == [["a" * 32, "b" * 32]]                  # c shares the coins but never in the same moment
    assert {(p["a"][0], p["b"][0], p["together"]) for p in pairs} >= {("a", "b", 4), ("a", "c", 0)}
