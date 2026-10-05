"""The Desk tab and its neighbours: the scanner checklist (must agree with the real entry rule), exit gauges,
API keys (never echoed), the wallet recorder's on/off control, portfolio, X feed, logos, and the server routes."""
import asyncio
import copy
import io
import json
import os
import stat
import time

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed, SyntheticFeed
from meme_trader.sniper.strategy import evaluate_late_entry, exit_watch, late_checklist

P = config.load(config.EXAMPLE)


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


def engine(realtime=False, **late):
    p = copy.deepcopy(P)
    p.sniper.late.update(late)
    return Engine(p, Quiet(realtime), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)


@pytest.mark.parametrize("seed,mode", [(3, "rule"), (8, "rule"), (5, "window")])
def test_scanner_checklist_agrees_with_the_entry_rule(seed, mode):
    seen = {"buy": 0, "wait": 0, "other": 0}

    async def go():
        e = engine(entry_mode=mode)
        L = e.p.late
        n = 0
        async for ev in SyntheticFeed(seed=seed, speed=0, launches=160, start_ts=1_780_000_000).events():
            await e.handle(ev)
            n += 1
            if n % 40:
                continue
            for s in list(e.tokens.values()):
                if not s.price_known:
                    continue
                red = e._late_red(s)
                ok, _ = evaluate_late_entry(s, e.now, L, red)
                c = late_checklist(s, e.now, L, red)
                assert ok == all(r["ok"] for r in c["checks"]) == (c["verdict"] == "buy"), (s.symbol, c)
                seen["buy" if ok else "wait" if c["verdict"] == "wait" else "other"] += 1
    asyncio.run(go())
    assert seen["buy"] and seen["other"]


def test_scanner_thoughts_and_desk_view():
    async def go():
        e = engine(realtime=True)
        async for ev in SyntheticFeed(seed=3, speed=0, launches=120, start_ts=1_780_000_000).events():
            await e.handle(ev)
            if ev.ts - getattr(e, "_t_think", 0) >= 2:
                e._t_think = ev.ts
                e._late_think()
        return e
    e = asyncio.run(go())
    assert e.thoughts and all(t["agent"] == "scanner" for t in e.thoughts)
    assert any(t["mood"] in ("act", "wait", "pass", "spot") for t in e.thoughts)
    d = e.desk_view()
    assert {"late", "holding", "thoughts", "desk", "risk", "feed", "limits"} <= set(d)
    for c in d["late"]["candidates"]:
        assert c["verdict"] in ("buy", "blocked", "wait", "early", "pass", "out", "holding", "done") and c["why"]


def test_exit_gauges_are_bounded():
    from meme_trader.sniper.strategy import SniperPosition
    from meme_trader.sniper.tracker import TokenState

    e = engine()

    async def go():
        async for ev in SyntheticFeed(seed=3, speed=0, launches=40, start_ts=1_780_000_000).events():
            await e.handle(ev)
    asyncio.run(go())
    s = next(s for s in e.tokens.values() if s.price_known)
    for src in ("late", "sniper"):
        pos = SniperPosition(mint=s.mint, symbol=s.symbol, opened_at=e.now - 100, entry_price=s.curve.price * 1.3,
                             tokens=1000.0, initial_tokens=1000.0, cost_sol=0.1, initial_cost_sol=0.1, score=50.0,
                             source=src)
        g = exit_watch(pos, s, e.now, e.p.late, e.p.exit)
        assert g and all(x["frac"] is None or 0 <= x["frac"] <= 1 for x in g)
    assert isinstance(TokenState, type)


def test_desk_wakes_only_with_a_key(monkeypatch):
    e = engine()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "API key" in e.set_desk(True) and not e.p.desk.enabled

    class FakeDesk:
        def __init__(self, p):
            self.p, self.client, self.enabled, self.calls = p, object(), True, 0

        def cost_usd(self):
            return 0.0
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("meme_trader.sniper.desk.Desk", FakeDesk)
    assert e.set_desk(True) == "" and e.desk.enabled and e.p.desk.enabled
    assert e.set_desk(False) == "" and not e.desk.enabled and not e.p.desk.enabled


# ---------------------------------------------------------------- API keys
def test_keys_are_written_privately_and_never_echoed(tmp_path, monkeypatch):
    from meme_trader.ui import keys

    env = tmp_path / ".env"
    env.write_text("# my notes\nSOLANA_WS_URL=wss://a.example\nANTHROPIC_API_KEY=old\nANTHROPIC_API_KEY=dupe\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    k = keys.set_key("ANTHROPIC_API_KEY", secret, env)
    text = env.read_text()
    assert text.count("ANTHROPIC_API_KEY=") == 1 and f"ANTHROPIC_API_KEY={secret}" in text and "# my notes" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600 and os.environ["ANTHROPIC_API_KEY"] == secret
    assert k["set"] and k["hint"] == "…wxyz" and secret not in json.dumps(keys.status(env))
    rpc = keys.set_key("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=SECRET123", env)
    assert rpc["hint"] == "mainnet.helius-rpc.com" and "SECRET123" not in json.dumps(keys.status(env))
    for name, bad in [("ANTHROPIC_API_KEY", "a b"), ("ANTHROPIC_API_KEY", "x\nMEME_TRADER_CONFIRM_LIVE=yes"),
                      ("SOLANA_RPC_URL", "wss://x"), ("SOLANA_WS_URL", "https://x"), ("MEME_TRADER_CONFIRM_LIVE", "yes"),
                      ("SOLANA_KEYPAIR_PATH", "/tmp/k.json"), ("ANTHROPIC_API_KEY", "")]:
        with pytest.raises(keys.KeyError_):
            keys.set_key(name, bad, env)
    assert "MEME_TRADER_CONFIRM_LIVE" not in env.read_text()
    keys.clear_key("ANTHROPIC_API_KEY", env)
    assert "ANTHROPIC_API_KEY" not in os.environ and not next(s for s in keys.status(env) if s["name"] == "ANTHROPIC_API_KEY")["set"]
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)


# ---------------------------------------------------------------- wallet recorder control
def test_recorder_pause_schedule_and_log(tmp_path):
    from datetime import datetime

    from meme_trader.ui import sidecars
    from meme_trader.wallets.recorder import Recorder, control_state

    noon = datetime(2026, 10, 4, 12, 0).timestamp()
    assert control_state({}, noon) == (True, "")
    assert control_state({"paused": True}, noon) == (False, "paused")
    assert control_state({"pause_until": noon + 60}, noon)[0] is False and control_state({"pause_until": noon - 1}, noon)[0]
    assert control_state({"quiet": {"start": "23:00", "end": "06:00"}}, datetime(2026, 10, 4, 2, 0).timestamp())[0] is False
    assert control_state({"quiet": {"start": "23:00", "end": "06:00"}}, noon)[0] is True
    assert control_state({"quiet": {"start": "11:00", "end": "13:00"}}, noon) == (False, "quiet hours 11:00-13:00")
    for bad in ({"op": "quiet", "start": "25:00", "end": "06:00"}, {"op": "pause_for", "hours": 0}, {"op": "nope"}):
        with pytest.raises(ValueError):
            sidecars.set_recorder(bad, tmp_path)
    cfg = config.load(config.EXAMPLE).wallets
    r = Recorder(cfg, tmp_path)
    assert r.active
    info = sidecars.set_recorder({"op": "pause_for", "hours": 2}, tmp_path)
    assert not info["active"] and info["pause_reason"].startswith("paused until") and not info["running"]
    r.apply_control()
    assert not r.active
    sidecars.set_recorder({"op": "resume"}, tmp_path)
    r.apply_control()
    assert r.active
    log = [json.loads(x) for x in (tmp_path / "pauses.jsonl").read_text().splitlines()]
    assert len(log) == 1 and log[0]["why"].startswith("paused until") and log[0]["end"] >= log[0]["start"]
    assert r.status()["active"] is True


# ---------------------------------------------------------------- portfolio + X feed against fake servers
def test_portfolio_prices_holdings_from_rpc_and_dexscreener(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from meme_trader.ui import portfolio as pfm

    WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
    MINT_A, MINT_B = "A" * 40 + "pump", "B" * 40 + "pump"

    async def rpc(request):
        body = await request.json()
        m = body["method"]
        if m == "getBalance":
            return web.json_response({"result": {"value": 2_500_000_000}})
        if m == "getTokenAccountsByOwner":
            if body["params"][1]["programId"] != pfm.TOKEN_PROGRAMS[0]:
                return web.json_response({"result": {"value": []}})
            acct = lambda mint, ui: {"account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"uiAmount": ui}}}}}}
            return web.json_response({"result": {"value": [acct(MINT_A, 1000), acct(MINT_B, 5), acct("C" * 43, 0)]}})
        return web.json_response({"result": [{"signature": "sig1", "blockTime": 1_790_000_000, "err": None}]})

    async def ds(request):
        return web.json_response([
            {"baseToken": {"address": MINT_A, "symbol": "AAA"}, "priceUsd": "0.01", "liquidity": {"usd": 10},
             "url": "https://dexscreener.com/solana/x"},
            {"baseToken": {"address": MINT_A, "symbol": "AAA"}, "priceUsd": "0.02", "liquidity": {"usd": 9000},
             "url": "https://evil.example/"}])

    async def go():
        app = web.Application()
        app.add_routes([web.post("/", rpc), web.get("/ds/{mints}", ds)])
        async with TestServer(app) as srv:
            monkeypatch.setenv("SOLANA_RPC_URL", str(srv.make_url("/")))
            monkeypatch.setattr(pfm, "DS_TOKENS", str(srv.make_url("/ds/")))
            pf = pfm.Portfolio(tmp_path / "pf.json", sol_usd=lambda: 100.0)
            with pytest.raises(pfm.PortfolioError):
                pf.add("not-an-address")
            pf.add(WALLET, "whale", mine=False)
            await pf.refresh()
            return pf
    pf = asyncio.run(go())
    w = pf.view()["wallets"][0]
    assert w["label"] == "whale" and w["sol"] == 2.5 and w["sol_value_usd"] == 250.0 and not w["error"]
    a = w["tokens"][0]
    assert a["symbol"] == "AAA" and a["price_usd"] == 0.02 and a["value_usd"] == 20.0 and a["url"] == ""   # most liquid pair
    assert w["unpriced"] == 1 and w["total_usd"] == 270.0 and w["activity"][0]["sig"] == "sig1"
    assert json.loads((tmp_path / "pf.json").read_text())[0]["address"] == WALLET
    pf.remove(WALLET)
    assert pf.view()["wallets"] == []


def test_xfeed_sources_and_posts(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from meme_trader.ui import xfeed as xm

    post = {"id": "1", "url": "https://x.com/a/status/1", "text": "$WIF looks hot CA: " + "D" * 40 + "pump",
            "created_timestamp": 1_790_000_000, "likes": 3,
            "author": {"screen_name": "a", "name": "A", "avatar_url": "https://pbs.twimg.com/a.jpg"},
            "media": {"photos": [{"url": "https://pbs.twimg.com/m.jpg"}, {"url": "https://evil.example/x.jpg"}]}}

    async def h(request):
        return web.json_response({"code": 200, "results": [post, {"bad": True}]})

    async def go():
        app = web.Application()
        app.add_routes([web.get("/2/{tail:.*}", h)])
        async with TestServer(app) as srv:
            monkeypatch.setattr(xm, "API", str(srv.make_url("/2")))
            xf = xm.XFeed(tmp_path / "x.json", held=lambda: [("WIF", "D" * 40 + "pump")])
            for kind, bad in (("account", "has space"), ("search", ""), ("thing", "x")):
                with pytest.raises(xm.XFeedError):
                    xf.add(kind, bad)
            xf.add("account", "@solana")
            with pytest.raises(xm.XFeedError):
                xf.add("search", "pump.fun")                                 # already a default search
            xf.add("search", "$WIF")
            assert xf.sources() == ["@solana", "q:pump.fun", "q:$WIF", "held:WIF:" + "D" * 40 + "pump"]
            import aiohttp

            async with aiohttp.ClientSession() as s:
                assert await xf.fetch(s, "@solana") == 1
                assert await xf.fetch(s, "q:pump.fun") == 0                      # same post: deduped
            return xf
    xf = asyncio.run(go())
    v = xf.view()
    p = v["posts"][0]
    assert p["mints"] == ["D" * 40 + "pump"] and p["cashtags"] == ["WIF"] and p["photos"] == ["https://pbs.twimg.com/m.jpg"]
    assert any(s["source"].startswith("held:WIF") for s in v["sources"]) and v["auto"]
    xf.remove("@solana")
    assert "solana" not in json.loads((tmp_path / "x.json").read_text())["accounts"]


# ---------------------------------------------------------------- logos
def test_logo_thumbnails_only_real_images():
    from PIL import Image

    from meme_trader.ui.logos import _ipfs, thumbnail
    from meme_trader.sniper.feeds import _allowed_url

    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 30, 30)).save(buf, "PNG")
    out = thumbnail(buf.getvalue())
    assert out[:4] == b"RIFF" and Image.open(io.BytesIO(out)).size[0] <= 96
    assert thumbnail(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>') is None
    assert thumbnail(b"GIF89a-not-really") is None
    assert _ipfs("ipfs://Qm123") == "https://ipfs.io/ipfs/Qm123"
    assert not _allowed_url("http://127.0.0.1/x.png") and not _allowed_url("file:///etc/passwd")


def test_logo_cache_and_misses(tmp_path):
    from meme_trader.ui.logos import Logos

    lg = Logos(None, tmp_path)

    async def none(_mint):
        return []
    lg._candidates = none
    mint = "E" * 40 + "pump"

    async def go():
        assert await lg.get(mint) is None and lg.miss_until[mint] > time.time()
        assert await lg.get("../../etc/passwd") is None
        (tmp_path / f"{mint}.webp").write_bytes(b"RIFFxxxxWEBP")
        return await lg.get(mint)
    assert asyncio.run(go()) == b"RIFFxxxxWEBP"


# ---------------------------------------------------------------- server routes
def test_server_desk_keys_and_actions(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def go():
        e = engine()
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            H = {"Host": host}
            assert (await c.get("/api/keys", headers={"Host": "evil.example"})).status == 403
            d = await (await c.get("/api/desk", headers=H)).json()
            assert "late" in d and "recorder" in d and "research" in d
            assert (await c.get("/api/logo/not-a-mint", headers=H)).status == 404
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            assert (await ws.receive_json())["type"] == "hello"

            async def ack(cmd):
                await ws.send_json(cmd)
                while True:
                    m = await ws.receive_json()
                    if m["type"] == "ack":
                        return m
            r = await ack({"action": "key_set", "name": "TELEGRAM_BOT_TOKEN", "value": "123456:ABCDEFGHIJKLMNOP"})
            assert r["ok"] and "123456:ABCDEFGHIJKLMNOP" not in json.dumps(r)
            assert "TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJKLMNOP" in (tmp_path / ".env").read_text()
            keys = await (await c.get("/api/keys", headers=H)).json()
            assert "ABCDEFGHIJKLMNOP" not in json.dumps(keys)
            assert not (await ack({"action": "key_set", "name": "MEME_TRADER_CONFIRM_LIVE", "value": "yes"}))["ok"]
            assert not (await ack({"action": "desk_wake", "on": True}))["ok"]
            r = await ack({"action": "x_add", "kind": "account", "value": "@solana"})
            assert r["ok"] and r["xfeed"]["sources"][0]["source"] == "@solana", r
            r = await ack({"action": "pf_add", "address": "nope"})
            assert not r["ok"]
            await ws.close()
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    asyncio.run(go())


def test_capped_reads_get_the_whole_body_across_chunks():
    """Regression: content.read(n) returned only the first chunk, truncating logos and token metadata."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from meme_trader.sniper.feeds import read_capped

    big = bytes(range(256)) * 2000                                    # 512 KB, sent in slow pieces

    async def h(request):
        r = web.StreamResponse()
        await r.prepare(request)
        for i in range(0, len(big), 7000):
            await r.write(big[i:i + 7000])
            await asyncio.sleep(0)
        await r.write_eof()
        return r

    async def go():
        import aiohttp

        app = web.Application()
        app.add_routes([web.get("/", h)])
        async with TestServer(app) as srv, aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/")) as r:
                whole = await read_capped(r, 1 << 20)
            async with s.get(srv.make_url("/")) as r:
                capped = await read_capped(r, 100_000)
        return whole, capped
    whole, capped = asyncio.run(go())
    assert whole == big and capped is None


def test_ipfs_links_fall_back_to_other_gateways():
    from meme_trader.ui.logos import GATEWAYS, ipfs_urls

    cid = "bafkreihe7cin2hdj354mdnnxbsusujc6o5v7e7fbvznpgjlsud4bp2xbui"
    urls = ipfs_urls(f"https://ipfs.io/ipfs/{cid}")
    assert urls[0] == f"https://ipfs.io/ipfs/{cid}" and urls[1:] == [g + cid for g in GATEWAYS]
    assert ipfs_urls("https://cdn.dexscreener.com/cms/images/x?w=1") == ["https://cdn.dexscreener.com/cms/images/x?w=1"]
    assert ipfs_urls(f"ipfs://{cid}")[0] == f"https://ipfs.io/ipfs/{cid}"


# ---------------------------------------------------------------- the AI desk's key problems
def test_workspace_header_and_friendly_errors(monkeypatch):
    from meme_trader.sniper.desk import client_kwargs, friendly_error

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert client_kwargs() == {}
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_01ABCdef")
    assert client_kwargs() == {"default_headers": {"anthropic-workspace-id": "wrkspc_01ABCdef"}}
    real = ("BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
            "'This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header'}}")
    assert "workspace ID" in friendly_error(real)
    assert "out of credits" in friendly_error("Your credit balance is too low")
    assert "rejected the API key" in friendly_error("AuthenticationError: 401 invalid x-api-key")


def test_user_key_warning_and_workspace_validation(tmp_path, monkeypatch):
    from meme_trader.ui import keys

    env = tmp_path / ".env"
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"):
        monkeypatch.delenv(k, raising=False)
    keys.set_key("ANTHROPIC_API_KEY", "sk-ant-usr-" + "x" * 90, env)
    st = {s["name"]: s for s in keys.status(env)}
    assert st["ANTHROPIC_API_KEY"]["warn"] and st["ANTHROPIC_API_KEY"]["testable"]
    with pytest.raises(keys.KeyError_, match="wrkspc_"):
        keys.set_key("ANTHROPIC_WORKSPACE_ID", "my-workspace", env)
    keys.set_key("ANTHROPIC_WORKSPACE_ID", "wrkspc_01ABCdefGHI", env)
    st = {s["name"]: s for s in keys.status(env)}
    assert not st["ANTHROPIC_API_KEY"]["warn"] and st["ANTHROPIC_WORKSPACE_ID"]["hint"] == "wrkspc_01ABCdefGHI"
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"):
        os.environ.pop(k, None)


def test_key_test_without_a_key(monkeypatch):
    from meme_trader.ui import keys

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert asyncio.run(keys.test_anthropic()) == (False, "no Anthropic API key saved")


def test_a_broken_desk_rests_itself_after_three_failed_reviews(monkeypatch):
    """Every persona erroring means the desk passes every trade: after 3 such reviews it steps aside."""
    from meme_trader.sniper.desk import Vote, aggregate

    e = engine()
    err = "BadRequestError: This API key is not scoped to a workspace, so this request must include ..."

    class Broken:
        enabled, client, calls = True, object(), 0

        async def review(self, snap):
            return aggregate([Vote(p, "pass", 0, error=err) for p in ("veteran", "skeptic")], {}, 0.45, 75)

        def cost_usd(self):
            return 0.0
    e.desk = Broken()
    e.p.desk["enabled"] = True

    async def go():
        async for ev in SyntheticFeed(seed=3, speed=0, launches=40, start_ts=1_780_000_000).events():
            await e.handle(ev)
        s = next(s for s in e.tokens.values() if s.price_known)
        for _ in range(3):
            e.reviewing.add(s.mint)
            await e._desk_then_buy(s, "late", 60.0, 0.05, [], "late", "", 0.0)
        return s
    asyncio.run(go())
    assert not e.desk.enabled and not e.p.desk.enabled and "workspace" in e.desk_error
    assert any("put to rest" in l["text"] for l in e.log)
    assert e.desk_view()["desk"]["error"]


def test_waking_the_desk_clears_stale_votes_and_a_new_key_rebuilds_it(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    built = []

    class FakeDesk:
        def __init__(self, p):
            built.append(os.environ.get("ANTHROPIC_API_KEY"))
            self.p, self.client, self.enabled, self.calls = p, object(), True, 0

        def cost_usd(self):
            return 0.0
    monkeypatch.setattr("meme_trader.sniper.desk.Desk", FakeDesk)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "a" * 90)
    e = engine()
    e.desk_reviews.append({"ts": 1, "votes": [{"persona": "veteran", "error": "AuthenticationError: invalid x-api-key"}]})
    assert e.set_desk(True) == "" and not e.desk_reviews

    async def go():
        async with TestClient(TestServer(make_app(e, data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.receive_json()
            await ws.send_json({"action": "key_set", "name": "ANTHROPIC_API_KEY", "value": "sk-ant-api03-" + "b" * 90})
            while (m := await ws.receive_json())["type"] != "ack":
                pass
            await ws.close()
            return m
    m = asyncio.run(go())
    assert m["ok"] and "restarted with it" in m["text"] and built[-1].endswith("b" * 90) and e.desk.enabled
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_page_layouts_and_bot_looks_are_saved_on_the_bot(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    async def go():
        async with TestClient(TestServer(make_app(engine(), data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await ws.receive_json()
            await ws.send_json({"action": "ui_set", "key": "bots", "value": {"scanner": {"name": "Hawk", "color": "#ff7a00", "acc": "crown"}}})
            await ws.send_json({"action": "ui_set", "key": "layout", "value": {"live": {"order": {"live:0": ["live:recent-trades"]}, "hidden": []}}})
            await ws.send_json({"action": "ui_set", "key": "secrets", "value": {"x": 1}})               # not a UI key
            await ws.send_json({"action": "ui_set", "key": "layout", "value": {"x": "y" * 70_000}})    # too big
            while (m := await ws.receive_json())["type"] != "ack":
                pass
            assert not m["ok"]
            await ws.close()
            return await (await c.get("/api/ui", headers={"Host": host})).json()
    ui = asyncio.run(go())
    assert ui["bots"]["scanner"]["name"] == "Hawk" and ui["layout"]["live"]["order"]["live:0"] == ["live:recent-trades"]
    assert "secrets" not in ui


def test_paper_account_survives_a_restart_and_can_start_over(tmp_path, monkeypatch):
    """The real-feed paper bot used to start a fresh 5 SOL account on every restart: today's P&L, the daily
    loss limit and the drawdown all reset (seen 2026-10-04 as 'today -1.09' on a +3 SOL day)."""
    from meme_trader.sniper.strategy import SniperPosition

    monkeypatch.setattr("meme_trader.sniper.engine.DATA", tmp_path)
    p = copy.deepcopy(P)

    def make():
        return Engine(p, Quiet(False), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False,
                      persist=True)
    e = make()
    e.book.sol, e.book.day, e.book.day_pnl, e.book.peak_equity = 6.2, "2026-10-04", -0.4, 6.9
    e.book.closed.append({"symbol": "WIN", "pnl": 1.2})
    e.deposit_paper(2.0)
    e.book.equity_hist.append((1.0, 8.2))
    m = "P" * 40 + "pump"
    e.positions[m] = SniperPosition(m, "OPEN", 0, 1e-7, 1000.0, 1000.0, 0.05, 0.05, 70, peak_price=1e-7, exits=[])
    e.save_state()

    e2 = make()
    asyncio.run(e2.restore_state())
    b = e2.book
    assert (b.sol, b.day, b.day_pnl, b.peak_equity, b.start_sol) == (8.2, "2026-10-04", -0.4, 8.9, p.sniper.capital.starting_sol + 2.0)
    assert b.closed[-1]["symbol"] == "WIN" and b.deposits[0][1] == 2.0 and (1.0, 8.2) in b.equity_hist
    assert m in e2.positions
    assert e2.reset_paper() == "close the open positions first"
    e2.positions.clear()
    assert e2.reset_paper() == ""
    assert e2.book.sol == p.sniper.capital.starting_sol and e2.book.day_pnl == 0 and not e2.book.closed
    e3 = make()
    asyncio.run(e3.restore_state())
    assert e3.book.sol == p.sniper.capital.starting_sol
    live = Engine(p, Quiet(False), PaperExecutor(p.sniper.execution), mode="live", log_to_journal=False)
    assert live.reset_paper() == "paper only"


def test_the_desk_stays_on_or_off_after_a_restart(monkeypatch):
    saved = []
    e = engine()
    e.persist = True                                                     # the real bot (save_setting faked)
    monkeypatch.setattr(e, "save_setting", lambda k, v, path=None: saved.append((k, v)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert e.set_desk(True) == "" and e.desk.enabled
    assert e.set_desk(False) == ""
    assert e.set_desk(False, who="bot") == "" and e.set_desk(True, who="key") == ""
    assert saved == [("desk.enabled", True), ("desk.enabled", False)]     # only the owner's choice is saved
    demo = engine()                                                      # a demo or test: persist off
    monkeypatch.setattr(demo, "save_setting", lambda k, v, path=None: saved.append((k, v)))
    demo.set_desk(False)
    assert len(saved) == 2                                               # never writes the config


def test_a_desk_approval_that_cannot_be_sized_says_why(monkeypatch):
    from meme_trader.sniper import desk as deskmod
    from meme_trader.sniper.tracker import TokenState

    e = engine()

    class Verdict:
        approve, votes, summary, size_mult = True, [], "APPROVED", 1.0

    class FakeDesk:
        enabled = True

        async def review(self, snap):
            return Verdict()
    e.desk = FakeDesk()
    monkeypatch.setattr(deskmod, "snapshot_for", lambda *a, **k: {})
    monkeypatch.setattr(e, "_size", lambda *a, **k: 0.0)
    s = TokenState("T" * 40 + "pump", None, e.now)
    asyncio.run(e._desk_then_buy(s, "late", 60, 0.1, [], "late", "", 0.0))
    assert any("desk approved" in l["text"] and "too thin" in l["text"] for l in e.log)


def test_coins_the_desk_passes_are_followed_by_the_gate_audit(monkeypatch):
    from types import SimpleNamespace as NS

    from meme_trader.sniper import desk as deskmod
    from meme_trader.sniper.tracker import TokenState

    e = engine()
    votes = [NS(persona=p, vote=v, conviction=60, error="", reasons=[], red_flags=[])
             for p, v in (("veteran", "buy"), ("narrative", "buy"), ("skeptic", "pass"), ("quant", "buy"))]

    class FakeDesk:
        enabled = True

        async def review(self, snap):
            return NS(approve=False, votes=votes, summary="PASSED", size_mult=1.0)
    e.desk = FakeDesk()
    monkeypatch.setattr(deskmod, "snapshot_for", lambda *a, **k: {})
    s = TokenState("A" * 40 + "pump", None, e.now)
    s.price_known = True
    asyncio.run(e._desk_then_buy(s, "late", 60, 0.1, [], "late", "", 0.0))
    assert e.audit[s.mint][0] == "AI desk passed (graduation): 3 of 4 said buy"


def test_the_desk_can_think_with_cheaper_or_local_models(monkeypatch):
    """Owner: 'an option to add local models or something cheaper' (and GitHub Models / Hugging Face)."""
    from meme_trader.config import Params
    from meme_trader.sniper import desk as deskmod

    for k in ("ANTHROPIC_API_KEY", "GITHUB_MODELS_TOKEN", "HF_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    base = dict(P.sniper.desk)
    assert "Anthropic" in deskmod.provider_ready(Params(base))
    assert "GITHUB_MODELS_TOKEN" in deskmod.provider_ready(Params({**base, "provider": "github"}))
    assert deskmod.provider_ready(Params({**base, "provider": "local"})) == ""          # no key for a local server
    assert deskmod.model_of(Params({**base, "provider": "github"})) == "openai/gpt-4.1-mini"   # not a Claude id
    assert deskmod.parse_json('Sure! ```json\n{"vote": "buy", "conviction": 70}\n```') == {"vote": "buy", "conviction": 70}

    calls = []

    async def fake_chat_json(self, model, system, user, max_tokens=700):
        calls.append((self.base, model, "JSON object" in system))
        return {"vote": "BUY", "conviction": 140, "reasons": ["flow"], "red_flags": []}, 900, 80
    monkeypatch.setattr(deskmod.OpenAICompat, "chat_json", fake_chat_json)
    d = deskmod.Desk(Params({**base, "enabled": True, "provider": "local", "model": "qwen2.5:7b"}))
    assert d.enabled and d.compat is not None and d.client is None
    v = asyncio.run(d._ask("veteran", {"symbol": "X"}))
    assert (v.vote, v.conviction, v.error) == ("buy", 100, "") and calls[0] == ("http://127.0.0.1:11434/v1", "qwen2.5:7b", True)
    assert d.cost_usd() == 0.0 and d.calls == 1

    e = engine()
    saved = []
    e.persist = True
    monkeypatch.setattr(e, "save_setting", lambda k, v, path=None: saved.append((k, v)))
    assert e.set_desk_brain("github") == "" and e.p.desk.model == "openai/gpt-4.1-mini"
    assert ("desk.provider", "github") in saved
    assert "http" in e.set_desk_brain("local", "llama3.2", "ftp://nope")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert e.set_desk_brain("anthropic", "claude-haiku-4-5") == ""
    b = e.desk_brain()
    assert b["model"] == "claude-haiku-4-5" and b["per_review_usd"] < 0.02 and b["ready"] == ""


def test_thinking_models_get_room_and_prices_come_from_the_provider_list():
    """Qwen3.5/3.8 on Hugging Face reply with 'reasoning' and, when it fills max_tokens, no 'content' at all."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from meme_trader.sniper.desk import OpenAICompat, model_price

    calls = []

    async def chat(request):
        body = await request.json()
        calls.append(body["max_tokens"])
        if len(calls) == 1:                               # thought until it ran out of room: no content key
            return web.json_response({"choices": [{"finish_reason": "length", "message": {"role": "assistant", "reasoning": "hmm " * 50}}],
                                      "usage": {"prompt_tokens": 10, "completion_tokens": 700}})
        return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": '\n{"vote": "pass", "conviction": 60}', "reasoning": "ok"}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 900}})

    async def models(_):
        return web.json_response({"data": [{"id": "Qwen/Big", "providers": [{"status": "live", "pricing": {"input": 2, "output": 6}},
                                                                             {"status": "live", "pricing": {"input": 1.5, "output": 7}}]}]})

    async def go():
        app = web.Application()
        app.add_routes([web.post("/v1/chat/completions", chat), web.get("/v1/models", models)])
        async with TestServer(app) as srv:
            base = str(srv.make_url("/v1"))
            d, tin, tout = await OpenAICompat(base).chat_json("Qwen/Big:fastest", "s", "u", 700)
            price = await model_price(base, "", "Qwen/Big:fastest")
            missing = await model_price(base, "", "Qwen/Other")
            return d, tin, tout, price, missing
    d, tin, tout, price, missing = asyncio.run(go())
    assert d["vote"] == "pass" and calls == [700, 2100] and (tin, tout) == (20, 1600)   # a retry with room, both counted
    assert price == (2.0, 7.0) and missing is None                                       # the highest live price: never low


def test_model_errors_say_which_side_failed():
    from meme_trader.sniper.desk import friendly_error

    assert "credits are used up" in friendly_error("RuntimeError: HTTP 402: You have depleted your monthly included credits.")
    assert "provider rejected the token" in friendly_error("RuntimeError: HTTP 401: invalid token")
    assert friendly_error("AuthenticationError: Error code: 401 - invalid x-api-key") == "Anthropic rejected the API key"


def test_the_team_huddles_from_the_bots_real_state(monkeypatch):
    """Owner: 'do the bots talk to each other? It doesn't seem like it. They should be close to AGI.'"""
    from meme_trader.sniper import desk as deskmod

    e = engine()
    seen = {}

    async def fake_chat(self, system, user, schema, effort=None, max_tokens=700):
        seen["system"], seen["user"] = system, json.loads(user)
        self.input_tokens += 2000; self.output_tokens += 500
        return {"lines": [{"who": "scanner", "say": "Nothing in the window, Quant."},
                          {"who": "quant", "say": "Then the sniper is the leak: -13.8% over 55 trades."},
                          {"who": "nobody", "say": "dropped: not a team member"}],
                "takeaway": "Graduation plays carry the account.", "suggestion": "Turn the sniper off."}, ""
    monkeypatch.setattr(deskmod.Desk, "chat", fake_chat)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert asyncio.run(e.huddle("test")) == ""
    h = e.huddles[-1]
    assert [x["who"] for x in h["lines"]] == ["scanner", "quant"] and h["suggestion"] == "Turn the sniper off."
    assert "grow the account" in seen["system"] and {"edge_check_14d", "settings"} <= set(seen["user"]["desk_brief"])
    assert h["cost_usd"] > 0 and e.desk_view()["huddles"][0]["id"] == h["id"]
    asyncio.run(e.huddle("again"))
    assert seen["user"]["previous_takeaways"] == ["Graduation plays carry the account."]   # they don't repeat themselves
