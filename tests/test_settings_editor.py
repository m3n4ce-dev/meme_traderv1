"""'All settings': every live-adjustable setting with its help text, owner-only, saved at Normal dial values."""
import asyncio
import copy

import yaml

from meme_trader import config
from meme_trader.sniper.agent_api import AgentAPI, AgentError
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed

P = config.load(config.EXAMPLE)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine():
    p = copy.deepcopy(P)
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=False)


def test_help_text_comes_from_the_example_file():
    h = config.example_help()
    assert "unique buyers" in h["sniper.late.min_buyers"]
    assert h["sniper.late.stop_loss_pct"] and h["sniper.execution.paper_delay_s"].startswith("paper:")
    assert "dashboard Copy/Open buttons" in h["sniper.callouts.auto_post"]          # continuation lines joined


def test_advanced_controls_cover_the_rest_and_only_scalars():
    e = engine()
    adv = {a["key"]: a for a in e.advanced_controls()}
    assert len(adv) > 80 and "late.min_buyers" in adv and "execution.paper_delay_s" in adv
    assert adv["late.entry_mode"]["type"] == "enum:rule,window"
    assert "sizing.max_usd" not in adv and "late.enabled" not in adv              # curated / dial-managed elsewhere
    assert "capital.starting_sol" not in adv and not any(k.startswith(("feed.", "agent.", "chat.")) for k in adv)
    assert all(a["help"] for a in adv.values())                                 # every one explained


def test_owner_can_change_any_agent_only_the_curated_ones(tmp_path):
    e = engine()
    assert e.set_control("late.min_buyers", "8") == "" and e.p.late.min_buyers == 8
    assert e.set_control("late.entry_mode", "window") == "" and e.p.late.entry_mode == "window"
    assert "between" in e.set_control("late.stop_loss_pct", -5)
    assert e.set_control("late.min_buyers", 3, owner=False).startswith("unknown setting")
    adv = {a["key"]: a for a in e.advanced_controls()}
    assert adv["late.min_buyers"]["changed"] and adv["late.min_buyers"]["default"] == P.sniper.late.min_buyers

    async def go():
        api = AgentAPI(e)
        try:
            await api.call("set_setting", {"key": "late.min_buyers", "value": "1", "reason": "agent wants more trades"})
        except AgentError as err:
            return str(err)
    assert "isn't an adjustable setting" in asyncio.run(go())
    path = tmp_path / "params.yaml"
    path.write_text("sniper: {}\n")
    e.save_controls(path)
    saved = yaml.safe_load(path.read_text())["sniper"]
    assert saved["late"]["min_buyers"] == 8 and saved["late"]["entry_mode"] == "window"
    assert "stop_loss_pct" not in saved["late"]                                  # untouched settings aren't written


def test_settings_endpoint():
    from aiohttp.test_utils import TestClient, TestServer

    from meme_trader.ui.server import make_app

    async def go():
        async with TestClient(TestServer(make_app(engine()))) as c:
            r = await c.get("/api/controls/advanced", headers={"Host": f"127.0.0.1:{c.port}"})
            rows = await r.json()
            assert r.status == 200 and any(x["key"] == "late.min_buyers" for x in rows)
    asyncio.run(go())
