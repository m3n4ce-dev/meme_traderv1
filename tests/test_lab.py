"""The team's lab: one setting at a time, replayed on recorded days, judged block by block, every try counted."""
import asyncio
import json

import pytest

from meme_trader.sniper import lab as labmod
from meme_trader.sniper.lab import Lab, compare
from tests.test_manual import market  # noqa: F401


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


def tr(t, pnl, mint="m"):
    return {"mint": mint, "opened": t, "pnl": pnl, "pnl_pct": pnl * 100}


def test_queue_rules(tmp_path):
    lab = Lab(tmp_path / "lab")
    base = {"late.min_net_flow_sol": 3.0, "late.min_buyers": 12}
    x, err = lab.add("late.min_net_flow_sol", 2, "lower bar", "team", 1000.0, base)
    assert x and not err and x["now"] == 3.0 and x["value"] == 2.0
    assert lab.add("late.min_net_flow_sol", 2, "again", "team", 1001.0, base)[1] == "that test is already queued"
    assert "isn't one the lab can test" in lab.add("capital.max_open_positions", 9, "", "team", 1, base)[1]
    assert "between" in lab.add("late.min_buyers", 999, "", "team", 1, base)[1]
    assert lab.add("late.min_buyers", 12, "", "team", 1, base)[1] == "that's already the current setting"
    lab.items[0]["status"] = "running"
    lab.save()
    assert Lab(tmp_path / "lab").queued()[0]["id"] == x["id"]       # a run cut short by a restart is queued again
    c = lab.auto_candidate(base, 5000.0)
    assert c and c[0] in base and c[1] != base[c[0]]


def test_compare_verdicts():
    span = (0.0, 6 * 6 * 3600)                                        # six 6-hour blocks
    base = [tr(i * 21600 + 10, -0.01, f"m{i}") for i in range(6)]
    good = [tr(i * 21600 + 10, 0.02, f"m{i}") for i in range(6)]
    assert compare(base, good, span)["verdict"] == "better"
    assert compare(good, base, span)["verdict"] == "worse"
    assert compare(base, list(base), span)["verdict"] == "no effect"
    mixed = [tr(i * 21600 + 10, 0.02 if i % 2 else -0.03, f"m{i}") for i in range(6)]
    r = compare(base, mixed, span)
    assert r["verdict"] == "no clear difference" and r["blocks"] == 6 and r["change"]["trades"] == 6


def test_the_bot_runs_queued_tests_and_tells_the_team(tmp_path, monkeypatch):
    e = market(launches=3)
    e.persist = True
    e.xlab = Lab(tmp_path / "lab")
    x, err = e.lab_add("late.min_net_flow_sol", 2, "more trades", "team")
    assert x and not err and e.lab_brief()["queued"][0]["test"].startswith("late.min_net_flow_sol")

    async def fake_exec(*args, **kw):                                  # stands in for the low-priority process
        out = tmp_path / "lab" / f"{x['id']}.result.json"
        out.write_text(json.dumps(compare([tr(10, -0.01)], [tr(10, 0.02)], (0.0, 3 * 21600))))

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(labmod, "free_mb", lambda: None)               # (whatever this machine has free right now)

    async def go():
        e._lab_step()
        await e._lab_task
    asyncio.run(go())
    done = e.lab_brief()["results"][0]
    assert done["status"] == "done" and done["verdict"] in ("better", "not enough data", "no clear difference")
    assert e.snapshot()["lab"]["last"]["id"] == x["id"] and e.xlab.tries() == 1


def test_meetings_send_their_tests_to_the_lab(tmp_path, monkeypatch):
    from meme_trader.sniper import desk as deskmod
    e = market(launches=3)
    e.xlab = Lab(tmp_path / "lab")
    seen = {}

    async def fake_chat(self, system, user, schema, effort=None, max_tokens=700):
        seen["brief"] = json.loads(user)["desk_brief"]
        self.input_tokens += 100; self.output_tokens += 50
        return {"lines": [{"who": "quant", "say": "Test a lower flow bar."}], "takeaway": "t", "suggestion": "",
                "plan": {"goal": "g", "strategy": "s", "next": "n", "experiments": [
                    {"name": "lower flow bar", "change": "2 SOL", "success_if": "more trades, same edge", "status": "proposed",
                     "test": {"key": "late.min_net_flow_sol", "value": 2}},
                    {"name": "bogus", "change": "x", "success_if": "y", "status": "proposed", "test": {"key": "capital.buy_sol", "value": 9}}]},
                "actions": []}, ""
    monkeypatch.setattr(deskmod.Desk, "chat", fake_chat)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert asyncio.run(e.huddle("test")) == ""
    assert "testable" in seen["brief"]["lab"] and "late.min_net_flow_sol" in seen["brief"]["lab"]["testable"]
    assert [x["key"] for x in e.xlab.queued()] == ["late.min_net_flow_sol"]   # only real, testable settings
    assert e.plan["experiments"][0]["test"] == {"key": "late.min_net_flow_sol", "value": 2}


def test_the_ai_sets_exits_and_places_orders_for_the_owner():
    from meme_trader.sniper.agent_api import AgentAPI, AgentError
    from tests.test_manual import live_coin
    e = market()
    s = live_coin(e)
    api = AgentAPI(e)
    WHY = "the owner asked for a stop"
    with pytest.raises(AgentError, match="no open position"):
        asyncio.run(api.call("set_exits", {"mint": s.mint, "reason": WHY, "stop_loss_pct": 25}))
    assert asyncio.run(e.manual_buy(s.mint, 0.1)) == ""
    r = asyncio.run(api.call("set_exits", {"mint": s.mint, "reason": WHY, "stop_loss_pct": 25, "take_profit_pct": 100, "take_profit_fraction": 0.5}))
    assert r["ok"] and e.positions[s.mint].manual["sl"] == 25 and e.positions[s.mint].manual["tp"] == 100
    with pytest.raises(AgentError, match="at least one"):
        asyncio.run(api.call("set_exits", {"mint": s.mint, "reason": WHY}))
    e.sol_price.usd = 150.0
    r = asyncio.run(api.call("order", {"mint": s.mint, "side": "sell", "mcap_usd": 50000, "fraction": 0.5, "reason": "sell half into strength"}))
    assert r["ok"] and "sell 50%" in r["label"] and "≥" in r["label"]
    oid = r["order"]
    with pytest.raises(AgentError, match="capped"):
        asyncio.run(api.call("order", {"mint": s.mint, "side": "buy", "mcap_usd": 5000, "sol": 50, "reason": "a huge dip buy"}))
    assert asyncio.run(api.call("cancel_order", {"order_id": oid, "reason": "the owner changed their mind"}))["ok"]
    e.positions[s.mint].source = "late"                                  # the bot's own position: its strategy's exits
    with pytest.raises(AgentError, match="bot's own position"):
        asyncio.run(api.call("set_exits", {"mint": s.mint, "reason": WHY, "stop_loss_pct": 10}))
