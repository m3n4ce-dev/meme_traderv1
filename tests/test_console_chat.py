"""Dashboard chat (Claude Code headless), contract-address lookup, paper deposits, market pulse."""
import asyncio
import copy
import json
import stat
import struct
import sys

import pytest

from meme_trader import config
from meme_trader.sniper import lookup as lk
from meme_trader.sniper.agent_api import AgentAPI, AgentError
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Migration, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.ui.chat import ChatManager

P = config.load(config.EXAMPLE)
A = "A" * 40 + "pump"
DEV = "D" * 44
WHY = "test reason here"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine", "meme_trader.ui.chat"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine(mode="paper"):
    p = copy.deepcopy(P)
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode=mode, log_to_journal=False, persist=False)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ lookup
def test_is_mint():
    assert lk.is_mint(BONK) and lk.is_mint("  " + BONK + " ")
    assert not lk.is_mint("hello") and not lk.is_mint("0" * 44) and not lk.is_mint(BONK + "xx")


def test_decode_curve_reads_reserves_flags_and_creator():
    from solders.pubkey import Pubkey

    creator = Pubkey.from_string(BONK)
    raw = bytes(8) + struct.pack("<5Q", 953_841_678_518_919, 97_577_130_608, 673_941_678_518_919, 841_537_216,
                                 1_000_000_000_000_000) + b"\x00" + bytes(creator) + b"\x01"
    c = lk.decode_curve(raw)
    assert c["v_sol"] == pytest.approx(97.577130608) and c["v_tokens"] == pytest.approx(953_841_678.518919)
    assert c["price_sol"] == pytest.approx(97.577130608 / 953_841_678.518919)
    assert c["progress_pct"] == pytest.approx(15.02, abs=0.01)
    assert c["creator"] == BONK and c["mayhem"] is True and c["complete"] is False
    assert lk.decode_curve(raw[:40]) is None
    done = raw[:48] + b"\x01" + raw[49:]
    assert lk.decode_curve(done)["progress_pct"] == 100.0


def test_lookup_assembles_sources_and_flags(monkeypatch):
    from solders.keypair import Keypair

    w1, w2 = str(Keypair().pubkey()), str(Keypair().pubkey())          # real wallets (on the ed25519 curve)
    curve = bytes(8) + struct.pack("<5Q", 900_000_000_000_000, 80_000_000_000, 620_000_000_000_000,
                                   50_000_000_000, 1_000_000_000_000_000) + b"\x00" + bytes(32) + b"\x00"

    async def onchain(rpc, mint):
        return {"curve_address": "C" * 44, "curve": lk.decode_curve(curve),
                "mint": {"supply": 1e9, "decimals": 6, "mint_authority": "M" * 44, "freeze_authority": None,
                         "program": "spl-token"}}

    async def dex(http, mint):
        return None

    async def rug(http, mint):
        return {"score_normalised": 40, "rugged": False, "totalHolders": 321,
                "tokenMeta": {"symbol": "TST", "name": "Test"},
                "risks": [{"name": "Low liquidity", "level": "danger", "value": ""}],
                "topHolders": [{"address": "x" * 44, "owner": "C" * 44, "pct": 60.0},
                               {"address": "y" * 44, "owner": w1, "pct": 12.0, "insider": True},
                               {"address": "z" * 44, "owner": w2, "pct": 9.0}]}

    monkeypatch.setattr(lk, "_onchain", onchain)
    monkeypatch.setattr(lk, "_dexscreener", dex)
    monkeypatch.setattr(lk, "_rugcheck", rug)
    lk._cache.clear()
    d = run(lk.lookup(BONK, sol_usd=200.0))
    assert d["symbol"] == "TST" and d["stage"] == "bonding curve" and d["holder_count"] == 321
    assert d["mcap_usd"] == pytest.approx(80 / 900_000_000 * 1e9 * 200, rel=1e-6)
    assert d["holders"]["top"][0]["label"] == "bonding curve" and d["holders"]["top"][1]["label"] == "insider"
    assert d["holders"]["top10_pct"] == pytest.approx(21.0)   # the curve's own account is excluded
    texts = " | ".join(f["text"] for f in d["flags"])
    assert "Mint authority" in texts and "Low liquidity" in texts
    b = lk.brief(d)
    assert "candles" not in b and b["symbol"] == "TST"
    with pytest.raises(ValueError):
        run(lk.lookup("not-a-mint"))


# ------------------------------------------------------------------ deposits, pulse
def test_paper_deposit_is_capital_not_profit():
    e = engine()
    e.book.equity_hist.append((1.0, e.book.sol))
    out = e.deposit_paper(5)
    assert out["cash_sol"] == pytest.approx(P.sniper.capital.starting_sol + 5)
    assert e.book.start_sol == pytest.approx(P.sniper.capital.starting_sol + 5)
    assert e.book.equity_hist[-1][1] == pytest.approx(e.book.start_sol)
    assert e.summary()["realized_pnl_sol"] == 0
    with pytest.raises(ValueError):
        e.deposit_paper(0)
    with pytest.raises(ValueError):
        engine("live").deposit_paper(1)


def test_agent_deposit_limits_and_keep(tmp_path, monkeypatch):
    e = engine()
    saved = {}
    monkeypatch.setattr(e, "save_setting", lambda k, v, path=None: saved.update({k: v}))
    api = AgentAPI(e)

    async def go():
        with pytest.raises(AgentError, match="at most"):
            await api.call("deposit", {"sol": api.max_deposit_sol + 1, "reason": WHY})
        out = await api.call("deposit", {"sol": 3, "reason": WHY, "keep": True})
        assert out["ok"] and saved["capital.starting_sol"] == pytest.approx(P.sniper.capital.starting_sol + 3)
        assert any("paper deposit" in x["text"] for x in e.log if x["level"] == "agent")
        live = engine("live")
        with pytest.raises(AgentError, match="paper only"):
            await AgentAPI(live).call("deposit", {"sol": 1, "reason": WHY})
    run(go())


def test_save_setting_writes_one_key(tmp_path):
    path = tmp_path / "params.yaml"
    path.write_text("mode: paper\nsniper:\n  sizing: {max_usd: 50}\n")
    engine().save_setting("capital.starting_sol", 12.5, path)
    import yaml

    d = yaml.safe_load(path.read_text())
    assert d["sniper"]["capital"]["starting_sol"] == 12.5
    assert d["sniper"]["sizing"]["max_usd"] == 50 and d["mode"] == "paper"


def test_market_pulse_counts_per_minute():
    e = engine()

    async def go():
        await e.handle(Launch(A, 60.0, DEV, symbol="TOK"))
        s = e.tokens[A]
        k = s.curve.v_sol * s.curve.v_tokens
        await e.handle(Trade(A, 61.0, "w", "buy", 2.0, 1e7, 32.0, k / 32.0))
        await e.handle(Trade(A, 125.0, "w", "sell", 1.0, 1e7, 31.0, k / 31.0))
        await e.handle(Migration(A, 126.0))
    run(go())
    rows = e.snapshot()["pulse"]
    assert rows[0][:3] == [60, 1, 1] and rows[0][3] == pytest.approx(2.0)
    assert rows[1][0] == 120 and rows[1][2] == 1 and rows[1][4] == pytest.approx(1.0) and rows[1][5] == 1
    assert e.snapshot()["limits"]["kill_dd_pct"] == P.sniper.capital.max_drawdown_pct


# ------------------------------------------------------------------ chat (with a fake `claude`)
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "env": sorted(os.environ)}) + "\n")
def out(d): print(json.dumps(d), flush=True)
def se(ev): out({"type": "stream_event", "event": ev})
if "--resume" in sys.argv and sys.argv[sys.argv.index("--resume") + 1] == "gone":
    print("No conversation found with session ID: gone", file=sys.stderr); sys.exit(1)
TOOL = {"type": "tool_use", "id": "t1", "name": "mcp__meme-trader__get_status"}
out({"type": "system", "subtype": "init", "session_id": "s1",
     "mcp_servers": [{"name": "meme-trader", "status": "connected"}]})
out({"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.25, "resetsAt": 1}}}})
se({"type": "message_start", "message": {"id": "m1"}})
se({"type": "content_block_start", "index": 0, "content_block": TOOL})
out({"type": "assistant", "message": {"id": "m1", "content": [dict(TOOL, input={})]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                               "content": [{"type": "text", "text": "{}"}]}]}})
se({"type": "message_start", "message": {"id": "m2"}})
for w in ["All ", "good: ", "**paper** mode."]:
    se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w}})
out({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "All good: **paper** mode."}]}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": "s1", "duration_ms": 1234,
     "num_turns": 2, "result": "All good"})
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PUMPPORTAL_API_KEY", "should-not-leak")
    monkeypatch.setattr("meme_trader.ui.chat.ENV_KEEP", ("HOME", "PATH", "FAKE_LOG"))
    return path


def calls(tmp_path):
    return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]


async def wait_idle(chat):
    for _ in range(300):
        if not chat.busy:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("chat never finished")


def test_chat_streams_a_reply_and_resumes(fake_claude, tmp_path):
    async def go():
        chat = ChatManager(engine(), 8787, {"claude_bin": str(fake_claude)}, tmp_path / "chat.json")
        q = chat.subscribe()
        assert await chat.send("how are we doing?") == ""
        assert await chat.send("again") != ""                        # one answer at a time
        await wait_idle(chat)
        bot = chat.state["messages"][-1]
        assert bot["done"] and not bot.get("error")
        assert [p["type"] for p in bot["parts"]] == ["tool", "text"]
        assert bot["parts"][0]["name"] == "get_status" and bot["parts"][0]["status"] == "ok"
        assert bot["parts"][1]["text"] == "All good: **paper** mode."  # deltas, not doubled by the final message
        assert chat.state["session_id"] == "s1" and chat.usage["five_hour"]["used"] == 0.25
        evs = []
        while not q.empty():
            evs.append(q.get_nowait()["ev"])
        assert "delta" in evs and "msg" in evs and "status" in evs
        await chat.send("and now?")
        await wait_idle(chat)
        c = calls(tmp_path)
        a0, a1 = c[0]["argv"], c[1]["argv"]
        assert a0[a0.index("--tools") + 1] == "" and "--strict-mcp-config" in a0 and "--resume" not in a0
        assert a1[a1.index("--resume") + 1] == "s1"
        assert "mcp__meme-trader__approve_action" in a0 and "mcp__meme-trader__get_status" in a0
        assert all(k not in c[0]["env"] for k in ("ANTHROPIC_API_KEY", "PUMPPORTAL_API_KEY"))
        assert json.loads((tmp_path / "chat.json").read_text())["session_id"] == "s1"
    run(go())


def test_chat_restarts_a_lost_session(fake_claude, tmp_path):
    async def go():
        chat = ChatManager(engine(), 8787, {"claude_bin": str(fake_claude)}, tmp_path / "chat.json")
        chat.state["session_id"] = "gone"
        await chat.send("hi")
        await wait_idle(chat)
        assert not chat.state["messages"][-1].get("error") and chat.state["session_id"] == "s1"
        assert len(calls(tmp_path)) == 2
    run(go())


def test_chat_approval_waits_for_the_owner(tmp_path):
    async def go():
        chat = ChatManager(engine(), 8787, {"claude_bin": sys.executable}, tmp_path / "chat.json")
        q = chat.subscribe()
        chat.cur = {"id": "a1", "role": "assistant", "done": False,
                    "parts": [{"type": "tool", "tid": "t9", "name": "pause_entries", "input": {}, "status": "running"}]}
        task = asyncio.create_task(chat.request_approval("mcp__meme-trader__pause_entries", {"reason": WHY}, "t9"))
        await asyncio.sleep(0.01)
        part = chat.cur["parts"][0]
        assert part["approval"]["status"] == "pending" and part["input"] == {"reason": WHY}
        assert chat.decide(part["approval"]["aid"], True) == ""
        assert (await task) == {"behavior": "allow"} and part["approval"]["status"] == "approved"
        task = asyncio.create_task(chat.request_approval("mcp__meme-trader__pause_entries", {"reason": WHY}, "t9"))
        await asyncio.sleep(0.01)
        chat.decide(part["approval"]["aid"], False)
        res = await task
        assert res["behavior"] == "deny" and "declined" in res["message"]
        chat.set_options(ask_first=False)
        assert (await chat.request_approval("mcp__meme-trader__pause_entries", {}, "t9")) == {"behavior": "allow"}
        chat.cur = None
        assert (await chat.request_approval("x", {}, ""))["behavior"] == "deny"
        assert not q.empty()
    run(go())


def test_chat_shows_a_card_for_a_pasted_contract_address(fake_claude, tmp_path, monkeypatch):
    async def fake_lookup(mint, engine=None, sol_usd=None, watch=False):
        return {"mint": mint, "symbol": "TST", "stage": "listed", "candles": [[1, 1, 1, 1, 1, 1]], "flags": []}

    monkeypatch.setattr("meme_trader.ui.chat.lookup", fake_lookup)

    async def go():
        chat = ChatManager(engine(), 8787, {"claude_bin": str(fake_claude)}, tmp_path / "chat.json")
        await chat.send(BONK)
        await wait_idle(chat)
        bot = chat.state["messages"][-1]
        assert bot["parts"][0]["type"] == "card" and bot["parts"][0]["data"]["symbol"] == "TST"
        prompt = calls(tmp_path)[0]["prompt"]
        assert "Metrics for " + BONK in prompt and '"candles"' not in prompt
        chat.set_options(auto_read=False)
        await chat.send(BONK)
        await wait_idle(chat)
        assert len(calls(tmp_path)) == 1                              # card only: Claude not called
    run(go())


def test_chat_state_survives_a_restart(tmp_path):
    path = tmp_path / "chat.json"
    path.write_text(json.dumps({"session_id": "s1", "messages": [
        {"id": "x", "role": "assistant", "done": False,
         "parts": [{"type": "tool", "tid": "t", "name": "x", "status": "running",
                    "approval": {"status": "pending"}}]}]}))
    chat = ChatManager(engine(), 8787, {"claude_bin": sys.executable}, path)
    m = chat.state["messages"][0]
    assert m["done"] and "interrupted" in m["error"] and m["parts"][0]["approval"]["status"] == "expired"


# ------------------------------------------------------------------ server + MCP
def test_server_chat_routes_and_approval_endpoint(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    async def go():
        eng = engine()
        chat = ChatManager(eng, 8787, {"claude_bin": sys.executable}, tmp_path / "chat.json")
        async with TestClient(TestServer(make_app(eng, "tok", chat))) as c:
            host = f"127.0.0.1:{c.port}"
            r = await c.get("/api/chat", headers={"Host": host})
            assert r.status == 200 and (await r.json())["messages"] == []
            r = await c.post("/api/agent", json={"tool": "_approval", "args": {"tool_name": "x", "input": {}}},
                             headers={"Host": host, "X-Agent-Token": "tok"})
            assert (await r.json())["result"]["behavior"] == "deny"      # no chat run waiting
            r = await c.post("/api/agent", json={"tool": "_approval", "args": {}}, headers={"Host": host})
            assert r.status == 403
            ws = await c.ws_connect("/ws", headers={"Host": host, "Origin": f"http://{host}"})
            await ws.send_json({"action": "deposit", "sol": 2})
            for _ in range(5):
                m = await ws.receive_json()
                if m.get("type") == "ack":
                    break
            assert m["ok"] and "Added 2" in m["text"]
            await ws.send_json({"action": "chat_opts", "model": "haiku"})
            for _ in range(5):
                m = await ws.receive_json()
                if m.get("type") == "chat":
                    break
            assert m["ev"] == "status" and m["model"] == "haiku"
            await ws.close()
    run(go())


def test_mcp_server_tools(monkeypatch):
    pytest.importorskip("mcp")
    import importlib

    import meme_trader.sniper.mcp_server as ms

    async def names(mod):
        return {t.name for t in await mod.mcp.list_tools()}

    monkeypatch.delenv("MEME_TRADER_CHAT", raising=False)
    ms = importlib.reload(ms)
    got = run(names(ms))
    assert {"lookup_token", "add_paper_funds"} <= got and "approve_action" not in got
    monkeypatch.setenv("MEME_TRADER_CHAT", "1")
    ms = importlib.reload(ms)
    assert "approve_action" in run(names(ms))
    monkeypatch.delenv("MEME_TRADER_CHAT")
    importlib.reload(ms)


def test_example_config_validates_chat_section():
    p = copy.deepcopy(P)
    p.sniper.chat["model"] = "gpt"
    with pytest.raises(config.ConfigError):
        config.validate_sniper(p.sniper)
