"""Risk dial (owner-controlled exposure), Claude's access to it, the page's self-reload, and frozen policies
replaying with their locked settings."""
import asyncio
import copy
import json
import sys

import pytest

from meme_trader import config
from meme_trader.sniper import research as R
from meme_trader.sniper.agent_api import AgentAPI, AgentError
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.ui.chat import ChatManager

P = config.load(config.EXAMPLE)
WHY = "owner asked for it"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine", "meme_trader.ui.chat",
                "meme_trader.sniper.research"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine(level=2, max_level=5):
    p = copy.deepcopy(P)
    p.sniper.risk["level"], p.sniper.risk["max_level"] = level, max_level
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=False)


def test_dial_scales_exposure_around_the_configured_settings(monkeypatch):
    e = engine()
    base = P.sniper.sizing.base_usd, P.sniper.sizing.max_usd, P.sniper.capital.max_open_positions
    saved = {}
    monkeypatch.setattr(e, "save_setting", lambda k, v, path=None: saved.update({k: v}))
    assert e.set_risk(4) == "" and e.risk_level == 4 and saved == {"risk.level": 4}
    assert e.p.sizing.base_usd == base[0] * 2 and e.p.sizing.max_usd == base[1] * 2
    assert e.p.capital.max_open_positions == base[2] * 2
    assert e.p.capital.daily_loss_limit_sol == pytest.approx(P.sniper.capital.daily_loss_limit_sol * 2)
    assert e.p.capital.max_drawdown_pct == P.sniper.capital.max_drawdown_pct          # kill switch never moves
    assert e.p.exit.stop_loss_pct == P.sniper.exit.stop_loss_pct
    assert e.set_risk(2) == "" and e.p.sizing.max_usd == base[1]
    assert "max_level" in engine(max_level=3).set_risk(4)
    assert e.set_risk(9) and e.set_risk("x")
    assert e.snapshot()["risk"]["level"] == 2 and len(e.snapshot()["risk"]["levels"]) == 5


def test_dial_level_from_config_and_saved_controls_dont_scale_twice(tmp_path):
    e = engine(level=3)
    assert e.p.sizing.max_usd == P.sniper.sizing.max_usd * 1.5
    path = tmp_path / "params.yaml"
    path.write_text("sniper: {}\n")
    e.save_controls(path)
    import yaml

    d = yaml.safe_load(path.read_text())["sniper"]
    assert d["sizing"]["max_usd"] == P.sniper.sizing.max_usd and d["risk"]["level"] == 3


def test_owner_edit_moves_the_baseline_agent_edit_doesnt():
    e = engine(level=3)
    assert e.set_control("sizing.max_usd", 60) == ""                 # owner, at x1.5
    assert e.risk_base["sizing.max_usd"] == pytest.approx(40)
    assert e.set_control("sizing.max_usd", 30, owner=False) == ""
    assert e.risk_base["sizing.max_usd"] == pytest.approx(40)


def test_agent_follows_the_dial():
    async def go():
        e = engine()
        api = AgentAPI(e)
        cap = e.p.sizing.max_usd
        with pytest.raises(AgentError, match="can't go above"):
            await api.call("set_setting", {"key": "sizing.max_usd", "value": str(cap + 1), "reason": WHY})
        out = await api.call("risk", {"level": 3, "reason": WHY})       # the chat asked the owner first
        assert out["risk_level"] == 3 and e.p.sizing.max_usd == cap * 1.5
        await api.call("set_setting", {"key": "sizing.max_usd", "value": str(cap + 1), "reason": WHY})   # now allowed
        await api.call("risk", {"level": 1, "reason": WHY})
        assert e.risk_level == 1
        with pytest.raises(AgentError, match="max_level"):
            await AgentAPI(engine(max_level=2)).call("risk", {"level": 3, "reason": WHY})
        assert (await api.call("status"))["risk_dial"]["level"] == 1
    asyncio.run(go())


def test_chat_always_asks_before_raising_risk(tmp_path):
    async def go():
        e = engine()
        chat = ChatManager(e, 8787, {"claude_bin": sys.executable}, tmp_path / "chat.json")
        chat.set_options(ask_first=False)
        chat.cur = {"id": "a", "role": "assistant", "done": False, "parts": []}
        lower = await chat.request_approval("mcp__meme-trader__set_risk_level", {"level": 1}, "t1")
        assert lower == {"behavior": "allow"}
        task = asyncio.create_task(chat.request_approval("mcp__meme-trader__set_risk_level", {"level": 4}, "t2"))
        await asyncio.sleep(0.01)
        part = next(p for p in chat.cur["parts"] if p["tid"] == "t2")
        assert part["approval"]["status"] == "pending"                  # even with "ask before actions" off
        chat.decide(part["approval"]["aid"], True)
        assert (await task)["behavior"] == "allow"
    asyncio.run(go())


def test_page_reloads_itself_after_an_update(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui import server

    async def go():
        e = engine()
        async with TestClient(TestServer(server.make_app(e))) as c:
            host = f"127.0.0.1:{c.port}"
            html = await (await c.get("/", headers={"Host": host})).text()
            version = server.ui_version()[0]
            assert f'<meta name="ui-version" content="{version}">' in html
            assert "__UI_VERSION__" not in html.split("<body")[0]
            ws = await c.ws_connect("/ws", headers={"Host": host, "Origin": f"http://{host}"})
            assert (await ws.receive_json()) == {"type": "hello", "ui": version}
            monkeypatch.setattr(e, "save_setting", lambda *a, **k: None)
            await ws.send_json({"action": "risk", "level": 3})
            for _ in range(5):
                m = await ws.receive_json()
                if m.get("type") == "ack":
                    break
            assert m["ok"] and "Bold" in m["text"] and e.risk_level == 3
            await ws.close()
    asyncio.run(go())


POLICY = """name: lock-v1
overrides:
  late.enabled: true
  sizing.base_usd: 20
  sizing.max_usd: 20
gates: {min_days: 14}
frozen_at: null
frozen_hash: null
"""


def test_frozen_policy_replays_its_locked_settings(tmp_path, monkeypatch):
    path = tmp_path / "lock-v1.yaml"
    path.write_text(POLICY)
    f = R.freeze(str(path), now=1_780_000_000)
    pol = R.load_policy(str(path))
    lock = json.loads((tmp_path / "lock-v1.lock.json").read_text())
    assert lock["signature"] == f["hash"] and lock["sniper"]["sizing"]["max_usd"] == 20
    assert R._check_frozen_hash(pol) == f["hash"]
    # a setting added to the bot later (or a changed default) doesn't touch the frozen policy
    real_load = R.load

    def load_with_new_default(path=None):
        p = real_load(path)
        p["sniper"]["late"]["stop_loss_pct"] = 99
        p["sniper"]["brand_new_section"] = {"x": 1}
        return p
    monkeypatch.setattr(R, "load", load_with_new_default)
    assert R._check_frozen_hash(pol) == f["hash"]
    params = R.policy_params(pol)
    assert params["sniper"]["late"]["stop_loss_pct"] == lock["sniper"]["late"]["stop_loss_pct"]
    # ...but editing the policy file still is refused
    path.write_text(path.read_text().replace("sizing.max_usd: 20", "sizing.max_usd: 25"))
    with pytest.raises(config.ConfigError, match="changed after it was frozen"):
        R._check_frozen_hash(R.load_policy(str(path)))


def test_repo_policy_is_locked_and_intact():
    pol = R.load_policy("graduation-v1")
    assert R.lock_path(pol).exists() and R._check_frozen_hash(pol) == pol["frozen_hash"]
