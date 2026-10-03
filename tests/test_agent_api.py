"""AI operator tools: the guardrails in agent_api.py, the token-protected endpoint, and the MCP server."""
import asyncio
import copy
import json

import pytest

from meme_trader import config
from meme_trader.sniper.agent_api import AgentAPI, AgentError
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
A = "A" * 40 + "pump"
DEV = "D" * 44
WHY = "test reason here"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine(**overrides):
    p = copy.deepcopy(P)
    for key, value in overrides.items():
        section, name = key.split("__")
        p.sniper[section][name] = value
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False,
                  persist=False)


def token(eng, mint=A):
    s = eng.tokens[mint] = TokenState(mint, Launch(mint, 0, DEV, symbol="TOK"), 0)
    s.on_launch(s.launch)
    k = s.curve.v_sol * s.curve.v_tokens
    s.on_trade(Trade(mint, 1, "w", "buy", 5.0, 1e7, 35.0, k / 35.0), 2)   # ~5% up the curve, priced
    return s


def run(coro):
    return asyncio.run(coro)


def test_reads_are_json_ready():
    async def go():
        eng = engine()
        token(eng)
        api = AgentAPI(eng)
        for tool in ("status", "positions", "radar", "settings", "trades", "log", "analytics"):
            json.dumps(await api.call(tool), default=str)
        assert (await api.call("token", {"mint": A}))["symbol"] == "TOK"
        with pytest.raises(AgentError):
            await api.call("token", {"mint": "B" * 44})
        with pytest.raises(AgentError):
            await api.call("format_disk")
    run(go())


def test_agent_may_lower_risk_but_never_exceed_the_owners_limits():
    async def go():
        eng = engine(entry__enabled=False)
        api = AgentAPI(eng)
        cap = eng.p.sizing.max_usd
        with pytest.raises(AgentError, match="can't go above"):
            await api.call("set_setting", {"key": "sizing.max_usd", "value": str(cap + 1), "reason": WHY})
        await api.call("set_setting", {"key": "sizing.max_usd", "value": str(cap - 5), "reason": WHY})
        assert eng.p.sizing.max_usd == cap - 5
        await api.call("set_setting", {"key": "sizing.max_usd", "value": str(cap), "reason": WHY})   # back to cap
        with pytest.raises(AgentError, match="can't go below"):
            await api.call("set_setting", {"key": "entry.min_score", "value": "10", "reason": WHY})
        with pytest.raises(AgentError, match="off in the owner's config"):
            await api.call("set_setting", {"key": "entry.enabled", "value": "true", "reason": WHY})
        await api.call("set_setting", {"key": "late.enabled", "value": "false", "reason": WHY})
        await api.call("set_setting", {"key": "late.enabled", "value": "true", "reason": WHY})   # was on: may restore
        with pytest.raises(AgentError, match="safety feature"):
            await api.call("set_setting", {"key": "risk_adapt.enabled", "value": "false", "reason": WHY})
        with pytest.raises(AgentError, match="isn't an adjustable setting"):
            await api.call("set_setting", {"key": "capital.starting_sol", "value": "100", "reason": WHY})
        with pytest.raises(AgentError, match="reason"):
            await api.call("pause", {"reason": ""})
        assert any("set sizing.max_usd" in x["text"] for x in eng.log if x["level"] == "agent")
    run(go())


def test_agent_actions_are_rate_limited():
    async def go():
        eng = engine(agent__max_actions_per_min=3)
        api = AgentAPI(eng)
        for _ in range(3):
            await api.call("note", {"text": "hello"})
        with pytest.raises(AgentError, match="rate limit"):
            await api.call("note", {"text": "one too many"})
    run(go())


def test_agent_buy_goes_through_the_engine_and_its_limits():
    async def go():
        eng = engine(agent__max_buys_per_hour=1)
        token(eng)
        api = AgentAPI(eng)
        with pytest.raises(AgentError, match="not tracked"):
            await api.call("buy", {"mint": "B" * 44, "usd": 5, "reason": WHY})
        with pytest.raises(AgentError, match="usd must be"):
            await api.call("buy", {"mint": A, "usd": eng.p.sizing.max_usd + 1, "reason": WHY})
        out = await api.call("buy", {"mint": A, "usd": 5, "reason": WHY})
        assert out["ok"] and eng.positions[A].source == "agent"
        token(eng, "C" * 40 + "pump")
        with pytest.raises(AgentError, match="per hour"):
            await api.call("buy", {"mint": "C" * 40 + "pump", "usd": 5, "reason": WHY})
        await api.call("sell", {"mint": A, "reason": WHY})
        assert A not in eng.positions
    run(go())


def test_agent_buy_refuses_rugs_near_graduation_and_engine_blocks():
    async def go():
        eng = engine()
        s = token(eng)
        api = AgentAPI(eng)
        s.dev_sold = 1.0
        with pytest.raises(AgentError, match="creator has sold"):
            await api.call("buy", {"mint": A, "usd": 5, "reason": WHY})
        s.dev_sold = 0.0
        s.migrated = True
        with pytest.raises(AgentError, match="graduation"):
            await api.call("buy", {"mint": A, "usd": 5, "reason": WHY})
        s.migrated = False
        eng.book.day_pnl = -eng.p.capital.daily_loss_limit_sol
        with pytest.raises(AgentError, match="daily loss limit"):
            await api.call("buy", {"mint": A, "usd": 5, "reason": WHY})
        eng.book.day_pnl = 0.0
        eng.p.agent["can_buy"] = False
        with pytest.raises(AgentError, match="disabled"):
            await AgentAPI(eng).call("buy", {"mint": A, "usd": 5, "reason": WHY})
    run(go())


def test_agent_watch_pause_resume():
    async def go():
        eng = engine()
        api = AgentAPI(eng)
        await api.call("watch", {"mint": "W" * 43 + "p", "reason": WHY})
        assert "W" * 43 + "p" in eng.tokens
        with pytest.raises(AgentError, match="mint"):
            await api.call("watch", {"mint": "short", "reason": WHY})
        await api.call("pause", {"reason": WHY})
        assert eng.paused and (await api.call("status"))["entries_blocked"] == "paused"
        await api.call("resume", {"reason": WHY})
        assert not eng.paused
    run(go())


def test_agent_endpoint_requires_the_token():
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    async def go():
        eng = engine()
        async with TestClient(TestServer(make_app(eng, "secret-token"))) as c:
            host = f"127.0.0.1:{c.port}"
            r = await c.post("/api/agent", json={"tool": "status"}, headers={"Host": host})
            assert r.status == 403
            r = await c.post("/api/agent", json={"tool": "status"}, headers={"Host": host, "X-Agent-Token": "nope"})
            assert r.status == 403
            r = await c.post("/api/agent", json={"tool": "status"},
                             headers={"Host": host, "X-Agent-Token": "secret-token"})
            assert r.status == 200 and "equity_sol" in (await r.json())["result"]
            r = await c.post("/api/agent", json={"tool": "set_setting", "args": {"key": "sizing.max_usd",
                                                                                   "value": "999", "reason": WHY}},
                             headers={"Host": host, "X-Agent-Token": "secret-token"})
            assert r.status == 400 and "can't go above" in (await r.json())["error"]
        async with TestClient(TestServer(make_app(eng))) as c:                  # agent API off: no route
            r = await c.post("/api/agent", json={"tool": "status"},
                             headers={"Host": f"127.0.0.1:{c.port}", "X-Agent-Token": "x"})
            assert r.status in (404, 405)
    run(go())


def test_agent_token_file_is_private(tmp_path):
    import stat

    from meme_trader.ui.server import write_agent_token

    tok = write_agent_token(tmp_path / "agent.token")
    assert len(tok) >= 32 and (tmp_path / "agent.token").read_text() == tok
    assert stat.S_IMODE((tmp_path / "agent.token").stat().st_mode) == 0o600


def test_mcp_server_round_trip(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from aiohttp.test_utils import TestServer

    from meme_trader.sniper import mcp_server
    from meme_trader.ui.server import make_app

    async def go():
        tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
        assert {"get_status", "get_token", "set_setting", "buy_token", "sell_position", "pause_entries"} <= set(tools)
        assert tools["get_status"].annotations.read_only_hint and tools["buy_token"].annotations.destructive_hint

        (tmp_path / "agent.token").write_text("tok")
        monkeypatch.setattr(mcp_server, "TOKEN_FILE", tmp_path / "missing.token")
        res = await mcp_server.mcp.call_tool("get_status", {})
        assert "isn't running" in res.content[0].text

        eng = engine()
        server = TestServer(make_app(eng, "tok"), host="127.0.0.1")
        await server.start_server()
        try:
            monkeypatch.setattr(mcp_server, "TOKEN_FILE", tmp_path / "agent.token")
            monkeypatch.setattr(mcp_server, "URL", f"http://127.0.0.1:{server.port}")
            res = await mcp_server.mcp.call_tool("get_status", {})
            assert json.loads(res.content[0].text)["mode"] == "paper"
            res = await mcp_server.mcp.call_tool("set_setting", {"key": "sizing.max_usd", "value": "999",
                                                                 "reason": WHY})
            assert res.content[0].text.startswith("REFUSED:")
        finally:
            await server.close()
    run(go())
