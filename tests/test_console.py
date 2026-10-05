"""The chat knows the console: the manual covers every tab, key and terminal command; console_help finds the
right section; ui_action changes only how open dashboards look; the corkboard hides and deletes notes."""
import asyncio
import re

import pytest

from meme_trader.sniper.agent_api import AgentAPI, AgentError
from meme_trader.ui import manual
from tests.test_manual import live_coin, market  # noqa: F401  (the paper-market helpers)

PAGE = manual.PAGE.read_text()
DOC = manual.DOC.read_text()


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


def test_the_manual_covers_every_tab_key_and_terminal_command():
    tabs = re.findall(r'\["(\w+)", "[^"]+"\]', PAGE[PAGE.index("const TABS = ["): PAGE.index("];", PAGE.index("const TABS = ["))])
    assert "charts" in tabs and len(tabs) == 9
    for t in tabs:
        assert t.lower() in DOC.lower(), f"CONSOLE.md never mentions the {t} tab"
    block = PAGE[PAGE.index("const TCMD = {"): PAGE.index("\n};", PAGE.index("const TCMD = {"))]
    cmds = re.findall(r'(\w+): \["', block)
    assert len(cmds) >= 30
    for c in cmds:
        assert f"`{c}" in DOC, f"CONSOLE.md doesn't list the terminal command {c}"
    for k in ("`t`", "`u`", "`m`", "`p`", "`a`", "`n`", "`c`", "`/`", "`?`", "Ctrl"):
        assert k in DOC


@pytest.mark.parametrize("q, want", [("how do I change the theme to white", "theme"), ("turn their dialog off", "speech"),
                                     ("move the characters around", "characters"), ("delete a note on the bulletin board", "corkboard"),
                                     ("pin a coin's chart", "charts"), ("show dollars instead of sol", "dollars"),
                                     ("what's the OG badge", "og")])
def test_console_help_finds_the_section(q, want):
    top = manual.search(q)["found"][0]["section"].lower()
    assert want in top, (q, top)


def test_guide_sections_are_searchable_too():
    assert any(t.startswith("Guide tab: ") for t, _ in manual.sections())


def test_ui_actions_change_only_the_screen_and_need_an_open_dashboard():
    api = AgentAPI(market(launches=2))
    with pytest.raises(AgentError, match="no dashboard"):
        api.read_ui("theme", "white")
    sent = []
    api.ui_sink = lambda m: sent.append(m) or 1
    assert api.read_ui("theme", "white")["ok"] and sent[-1] == {"type": "ui", "do": "theme", "value": "light", "bot": ""}
    api.read_ui("character", "talk=quiet", bot="Skeptic")
    assert sent[-1]["do"] == "character" and sent[-1]["bot"] == "Skeptic"
    for bad in [("buy", "x"), ("theme", "purple"), ("tab", "secret"), ("open_coin", "not-a-mint"), ("character", "talk=loud")]:
        with pytest.raises(AgentError):
            api.read_ui(*bad, bot="skeptic")
    assert "t key" in api.read_console_help("how do I switch the theme")["found"][0]["text"].lower().replace("`", "")


def test_the_chat_may_use_the_console_tools_without_approval():
    from meme_trader.sniper import mcp_server
    from meme_trader.ui.chat import CHAT_PROMPT, READ_TOOLS
    assert {"console_help", "ui_action"} <= set(READ_TOOLS)
    assert hasattr(mcp_server, "console_help") and hasattr(mcp_server, "ui_action")
    assert "console_help" in CHAT_PROMPT and "ui_action" in CHAT_PROMPT


def test_ui_actions_reach_open_dashboards_and_the_corkboard_hides_notes(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app
    e = market(launches=2)
    it = {"id": "n1", "ts": 1.0, "by": "you", "kind": "note", "title": "watch this", "note": "", "url": "", "mint": ""}
    e.memory.items_ = [it]

    async def go():
        async with TestClient(TestServer(make_app(e, "tok", data_dir=tmp_path))) as c:
            host = f"127.0.0.1:{c.port}"
            ws = await c.ws_connect("/ws", headers={"Origin": f"http://{host}", "Host": host})
            await asyncio.sleep(0.05)
            r = await c.post("/api/agent", json={"tool": "ui", "args": {"action": "theme", "value": "white"}},
                             headers={"Host": host, "X-Agent-Token": "tok"})
            assert r.status == 200 and (await r.json())["result"]["dashboards"] == 1
            got = None
            for _ in range(30):
                m = await ws.receive_json(timeout=5)
                if m.get("type") == "ui":
                    got = m
                    break
            assert got == {"type": "ui", "do": "theme", "value": "light", "bot": ""}
            await ws.send_json({"action": "mem_board", "id": "n1", "on": False})
            for _ in range(30):
                m = await ws.receive_json(timeout=5)
                if m.get("type") == "ack":
                    return m
    ack = asyncio.run(go())
    assert ack["ok"] and e.memory.items_[0]["off_board"] is True             # hidden, still remembered
    assert e.memory.set_board("n1", True) and "off_board" not in e.memory.items_[0]
    assert not e.memory.set_board("gone", False)


def test_howto_questions_carry_the_manual_and_others_dont():
    from meme_trader.ui.chat import with_manual
    p = with_manual("How do I hide a note on the bulletin board?", "How do I hide a note on the bulletin board?")
    assert "[Console manual" in p and "Hide from board" in p
    assert with_manual("what's my P&L today", "what's my P&L today") == "what's my P&L today"


@pytest.mark.parametrize("text, attach", [("How do I hide a note on the bulletin board?", True), ("make it white", True),
                                          ("turn off the speech bubbles", True), ("where is the kill switch", True),
                                          ("show me my P&L today", False), ("what's the market doing", False),
                                          ("add 0.5 SOL to BONK", False), ("change risk to bold", False)])
def test_only_howto_questions_get_the_manual(text, attach):
    from meme_trader.ui.chat import with_manual
    assert ("[Console manual" in with_manual(text, text)) is attach


def test_other_chains_rows_and_cache(monkeypatch):
    import time as _t

    from meme_trader.ui import chains
    p = {"attributes": {"address": "0xabc", "name": "PEPE / WBNB", "market_cap_usd": "123456.7", "fdv_usd": None, "reserve_in_usd": "5000",
                        "volume_usd": {"h1": "9000", "h24": "50000"}, "price_change_percentage": {"m5": "1.5", "h1": "-3", "h24": "40"},
                        "transactions": {"h1": {"buys": 120, "sells": 80, "buyers": 60}},
                        "pool_created_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - 3600))},
         "relationships": {"base_token": {"data": {"id": "bsc_0xtoken"}}}}
    r = chains.pool_row("bsc", p)
    assert r["symbol"] == "PEPE" and r["chain_name"] == "BNB Chain" and r["token"] == "0xtoken" and r["mcap_usd"] == 123456.7
    assert r["buys_h1"] == 120 and abs(r["age_s"] - 3600) < 120 and r["dex"].startswith("https://dexscreener.com/bsc/")
    assert chains.pool_row("bsc", {"attributes": {}}) is None
    c = chains.Chains()
    calls = []

    async def fake_refresh():
        calls.append(1)
        c.data = {"trending": {"bsc": [r]}, "new": {}, "fetched": _t.time(), "error": "", "chains": {"bsc": "BNB Chain"}}
    c._refresh = fake_refresh
    asyncio.run(c.get()); asyncio.run(c.get())
    assert len(calls) == 1 and c.names() == {"BNB Chain": ["PEPE"]}     # cached: one fetch per 90 s
