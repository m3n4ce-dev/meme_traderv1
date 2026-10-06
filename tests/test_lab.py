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


def test_a_run_in_its_own_service_is_picked_up_after_a_restart(tmp_path, monkeypatch):
    e = market(launches=3)
    e.persist = True
    lab = Lab(tmp_path / "lab")
    x, _ = lab.add("late.stop_loss_pct", 20, "wider stop", "team", 1000.0, {"late.stop_loss_pct": 15})
    x.update(status="running", started=__import__("time").time(), unit="meme-lab-" + x["id"])
    lab.save()
    (tmp_path / "lab" / f"{x['id']}.result.json").write_text(json.dumps(compare([tr(10, -0.01)], [tr(10, 0.02)], (0.0, 3 * 21600))))
    e.xlab = Lab(tmp_path / "lab")                                       # the bot restarts: the run stays "running"
    assert e.xlab.running() and e.xlab.running()["id"] == x["id"]
    monkeypatch.setattr(labmod, "free_mb", lambda: None)

    async def go():
        e._lab_step()
        await e._lab_task
    asyncio.run(go())
    assert e.xlab.items[0]["status"] == "done" and e.xlab.items[0]["result"]["verdict"]
    lab2 = Lab(tmp_path / "lab2")                                        # without its own service: queued again
    y, _ = lab2.add("late.stall_s", 60, "", "team", 1.0, {"late.stall_s": 45})
    y["status"] = "running"; lab2.save()
    assert Lab(tmp_path / "lab2").queued()[0]["id"] == y["id"]


def test_lab_replays_land_orders_like_the_live_bot():
    """A test of an exit setting is only fair with the bot's own landing delay and sell retries."""
    e = market(launches=2)
    e.p.execution["paper_delay_s"] = 2.5
    b = e.lab_baseline()
    assert b["execution.paper_delay_s"] == 2.5 and b["execution.sell_slippage_steps"] == list(e.p.execution.sell_slippage_steps)
    assert "late.stop_loss_pct" in b
    from meme_trader.sniper.lab import EXECUTION
    assert set(EXECUTION) & set(b) and not any(k.startswith("execution.") for k in labmod.TESTABLE)   # copied, never "tested"


def test_tests_judged_with_instant_fills_are_marked_and_tried_again(tmp_path):
    lab = Lab(tmp_path / "lab")
    old, _ = lab.add("late.stall_s", 60, "", "team", 1000.0, {"late.stall_s": 45})                     # before the fix
    new, _ = lab.add("late.stall_s", 30, "", "team", 1000.0, {"late.stall_s": 45, "execution.paper_delay_s": 2.5})
    for x in (old, new):
        x.update(status="done", result={"verdict": "no clear difference"})
    v = {x["value"]: x for x in lab.view()["done"]}
    assert v[60]["instant_fills"] and not v[30]["instant_fills"]
    picks = {lab.auto_candidate({"late.stall_s": 45}, 2000.0 + h * 3600) for h in range(40)}
    assert ("late.stall_s", 60) in picks and ("late.stall_s", 30) not in picks      # the old one is fair game again


def test_dump_profile_filters_skip_young_and_one_sided_entries():
    from meme_trader.sniper.strategy import evaluate_late_entry
    e = market(launches=40)
    L = e.p.late
    s = max((s for s in e.tokens.values() if not s.migrated and s.price_known and not s.dev_sold), key=lambda s: s.curve.progress)
    red = {**e._late_red(s), "max_bundle_pct": 100, "max_early_sold_ratio": 99.0, "max_creator_launches_24h": 10**6,
           "max_cluster_pct": 100}
    L.update(min_curve_pct=0, max_curve_pct=100, max_age_s=10**9, min_net_flow_sol=-1e9, min_buyers=0,
             min_buy_sell_ratio=0, min_near_high=0)                                  # (it qualifies on momentum)
    now = e.now
    base_ok, _ = evaluate_late_entry(s, now, L, red)
    L.update(min_age_s=s.age(now) + 5)
    assert evaluate_late_entry(s, now, L, red) == (False, "too young")
    L.update(min_age_s=0, min_recent_sells=10**6)
    assert evaluate_late_entry(s, now, L, red) == (False, "one-sided buying")
    L.update(min_recent_sells=0)
    assert evaluate_late_entry(s, now, L, red)[0] == base_ok                     # off by default: nothing changes
    assert {"late.min_age_s", "late.min_recent_sells", "late.runner_after_pct", "entry.max_bundle_pct"} <= set(labmod.TESTABLE)
    assert e.lab_baseline()["entry.max_early_sold_ratio"] == e.p.entry.max_early_sold_ratio
    assert e.lab_baseline()["late.runner_after_pct"] == 0 and "late.min_recent_sells" in e.lab_baseline()



def test_a_coin_matching_the_dump_profile_is_passed_over_for_good():
    """Waiting for a first sell / an older coin tested worse (replays, 2026-10-05): it's skipped once, for good."""
    from meme_trader.sniper.strategy import SKIP_FOR_GOOD
    assert set(SKIP_FOR_GOOD) == {"too young", "one-sided buying", "Mayhem mode"}
    e = market(launches=40)
    e.p.late.update(enabled=True, min_curve_pct=0, max_curve_pct=100, max_age_s=10**9, min_net_flow_sol=-1e9, min_buyers=0,
                    min_buy_sell_ratio=0, min_near_high=0, min_recent_sells=10**6)
    e.p.entry.update(max_bundle_pct=100, max_early_sold_ratio=99.0)
    from meme_trader.sniper.strategy import evaluate_late_entry
    for s in e.tokens.values():
        s.late_tried = False
    cand = [s for s in e.tokens.values() if s.decided and s.price_known and s.mint not in e.positions
            and evaluate_late_entry(s, e.now, e.p.late, e._late_red(s))[1] == "one-sided buying"]
    asyncio.run(e._maybe_late())
    assert cand and all(s.late_tried for s in cand) and not any(p.source == "late" for p in e.positions.values())



def test_a_queued_test_is_run_against_the_settings_of_the_moment(tmp_path, monkeypatch):
    e = market(launches=2)
    e.persist = True
    e.xlab = Lab(tmp_path / "lab")
    x, _ = e.xlab.add("late.stall_s", 60, "", "team", 1000.0, {"late.stall_s": 45})        # queued with an old baseline
    e.p.lab = {**(e.p.get("lab") or {}), "detach": False}

    async def fake_exec(*a, **k):
        raise OSError("no subprocess in this test")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(e._lab_run(x))
    assert "execution.paper_delay_s" in x["baseline"] and x["now"] == e.p.late.stall_s


def test_lab_replays_compare_rules_without_the_accounts_stops(tmp_path, monkeypatch):
    from meme_trader.sniper import research
    lab = Lab(tmp_path / "lab")
    x, _ = lab.add("late.min_age_s", 30, "", "team", 1000.0, {"late.min_age_s": 0, "execution.paper_delay_s": 2.5})
    seen = {}
    monkeypatch.setattr(research, "load_policy", lambda name: {"name": name})
    monkeypatch.setattr(research, "load_events", lambda files, **k: ([], {"first": 0.0, "last": 86400.0}))

    def fake_run(pol, events, jobs_list, jobs=0):
        seen.update({j[0]: j[2] for j in jobs_list})
        return {j[0]: {"trades": []} for j in jobs_list}
    monkeypatch.setattr(research, "run_variants", fake_run)
    f = tmp_path / "feed-2026-10-05.jsonl"
    f.write_text('{"ts": 86400}\n')
    out = labmod.run(x["id"], tmp_path, files=[f])
    for sets in seen.values():
        assert "capital.daily_loss_limit_sol=1000000" in sets and "capital.max_drawdown_pct=100" in sets
        assert "risk_adapt.enabled=false" in sets and "execution.paper_delay_s=2.5" in sets
    assert "late.min_age_s=30" in seen["change"] and out["account_stops"].startswith("off")


def test_tests_replay_each_recorded_day_and_reuse_the_baseline(tmp_path, monkeypatch):
    from pathlib import Path

    from meme_trader.sniper import research
    files = [Path(f"feed-2026-10-0{d}.jsonl.gz") for d in (1, 2, 3, 4, 5)] + [Path("feed-2026-10-06.jsonl")]
    assert [f.name[5:15] for f in labmod.day_files(files, 3, today="2026-10-06")] == ["2026-10-03", "2026-10-04", "2026-10-05"]

    lab = Lab(tmp_path / "lab")
    x, _ = lab.add("late.min_age_s", 30, "", "team", 1000.0, {"late.min_age_s": 0, "execution.paper_delay_s": 2.5})
    D = 86400.0
    day0 = {"feed-2026-10-03.jsonl.gz": 0.0, "feed-2026-10-04.jsonl.gz": D, "feed-2026-10-05.jsonl.gz": 2 * D}
    calls = []

    def tr(ts, pnl, n=0):
        return {"mint": f"m{ts}{n}", "opened": ts, "pnl": pnl, "pnl_pct": pnl * 100}

    def fake_events(fs, **k):
        t0 = day0[fs[0].name]
        return [t0], {"first": t0, "last": t0 + D - 1}

    def fake_run(pol, events, jobs_list, jobs=0):
        t0 = events[0]
        calls.append([j[0] for j in jobs_list])
        out = {"now": {"trades": [tr(t0 + 3600 * h, 0.0) for h in range(0, 24, 6)]}}
        if t0 == 0.0:                                          # day 1: the change wins every block, by a lot
            out["change"] = {"trades": [tr(t0 + 3600 * h, 1.0, 1) for h in range(0, 24, 6)]}
        else:                                                   # days 2 and 3: a little worse
            out["change"] = {"trades": [tr(t0 + 3600, -0.01, 1)]}
        return out
    monkeypatch.setattr(research, "load_policy", lambda name: {"name": name})
    monkeypatch.setattr(research, "load_events", fake_events)
    monkeypatch.setattr(research, "run_variants", fake_run)
    days = [Path(n) for n in day0]
    out = labmod.run(x["id"], tmp_path, files=days)
    assert out["days"] == 3 and out["days_better"] == 1 and len(out["per_day"]) == 3
    assert out["p_better"] >= 0.9 and out["better_blocks"] > out["worse_blocks"]   # pooled alone says "better" ...
    assert out["verdict"] == "no clear difference"             # ... but one day carried it
    assert all("now" in c for c in calls)                       # first run: each day's baseline replayed ...
    calls.clear()
    labmod.run(x["id"], tmp_path, files=days)
    assert calls and all(c == ["change"] for c in calls)        # ... and then reused
    assert len(list((tmp_path / "lab" / "base").glob("*.json"))) == 3
