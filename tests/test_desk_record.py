"""The AI team's track record (desk_record.py): scoring votes, the record from the journal, practice with the
graduation play off, and side votes when the rules decide."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper import desk_record as dr
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState

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


def engine(realtime=True, persist=False, **desk):
    p = copy.deepcopy(P)
    p.sniper.desk.update(desk)
    return Engine(p, Quiet(realtime), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=persist)


def votes(*pairs):
    return [NS(persona=p, vote=v, conviction=60, error="", reasons=[f"{p} thinks so"], red_flags=[]) for p, v in pairs]


class FakeDesk:
    enabled = True

    def __init__(self, approve, vs):
        self.approve, self.vs, self.seen = approve, vs, []

    async def review(self, snap, only=None):
        self.seen.append(only or {})
        return NS(approve=self.approve, votes=self.vs, summary="APPROVED" if self.approve else "PASSED", size_mult=1.0)


def test_scorecard_and_what_a_persona_reads():
    calls = [{"symbol": f"C{i}", "approve": i % 2 == 0, "pnl_pct": float(i - 5), "mode": "live",
              "votes": [["veteran", "buy" if i % 2 == 0 else "pass", 60, "flow"], ["skeptic", "pass", 70, "bundles"]]}
             for i in range(10)]
    sc = dr.scorecard(calls, ["veteran", "skeptic", "quant"])
    assert sc["calls"] == 10 and sc["buy"]["n"] == 5 and sc["pass"]["n"] == 5 and sc["by_mode"] == {"live": 10}
    assert sc["personas"]["veteran"]["buy"]["avg_pct"] == -1.0 and sc["personas"]["skeptic"]["pass"]["n"] == 10
    assert sc["personas"]["quant"] == {"buy": {"n": 0}, "pass": {"n": 0}}
    r = dr.persona_record(calls, "veteran")
    assert r["when_you_said_buy"]["n"] == 5 and len(r["latest"]) == 6 and r["latest"][-1] == {
        "coin": "C9", "you": "pass 60", "your_reason": "flow", "then": "+4%"}
    assert dr.persona_record(calls[:5], "veteran") is None                # too few calls to read anything into


def test_the_record_starts_from_the_journal_and_the_exit_lab(tmp_path):
    (tmp_path / "exit_lab.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"mint": "M1", "kind": "late", "variant": "as now", "opened": 1003.0, "pnl_pct": -12.0, "why": "stop", "held_s": 30},
        {"mint": "M1", "kind": "late", "variant": "stop 25%", "opened": 1003.0, "pnl_pct": -20.0},
        {"mint": "M2", "kind": "desk-pass", "variant": "as now", "opened": 2000.5, "pnl_pct": 40.0, "why": "graduation exit"},
        {"mint": "M3", "kind": "sniper", "variant": "as now", "opened": 3000.0, "pnl_pct": 5.0}]) + "\n")
    v = [{"persona": "veteran", "vote": "buy", "conviction": 62, "reasons": ["flow"], "error": ""},
         {"persona": "quant", "vote": "pass", "conviction": 55, "reasons": [], "error": "timeout"}]
    (tmp_path / "journal-2026-10-05.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": 1000.0, "event": "desk", "mint": "M1", "text": "AAA: APPROVED veteran:buy62", "votes": v},
        {"ts": 2000.0, "event": "desk", "mint": "M2", "text": "BBB: PASSED veteran:pass60", "votes": v},
        {"ts": 3000.0, "event": "desk", "mint": "M3", "text": "CCC: APPROVED", "votes": v},      # a sniper vote: not scored
        {"ts": 3100.0, "event": "info", "mint": "M1", "text": "x"}]) + "\n")
    calls = dr.backfill(tmp_path)
    assert [(c["symbol"], c["approve"], c["pnl_pct"]) for c in calls] == [("AAA", True, -12.0), ("BBB", False, 40.0)]
    assert calls[0]["votes"] == [["veteran", "buy", 62, "flow"]]           # the failed vote doesn't count
    e = engine(persist=True)
    assert [c["symbol"] for c in e.desk_calls] == ["AAA", "BBB"] and (tmp_path / "desk_calls.jsonl").exists()


def test_practice_with_the_graduation_play_off_scores_every_call(monkeypatch):
    e = engine(practice=True)
    e.p.late["enabled"] = False
    e.desk = FakeDesk(True, votes(("veteran", "buy"), ("skeptic", "pass")))
    monkeypatch.setattr("meme_trader.sniper.engine.evaluate_late_entry", lambda *a: (True, "late play"))
    s = TokenState("P" * 40 + "pump", None, e.now)
    s.price_known, s.decided = True, "watching"
    e.tokens[s.mint] = s

    async def go():
        await e._maybe_late()
        for _ in range(5):
            await asyncio.sleep(0)
    asyncio.run(go())
    assert not e.positions and not e.pending                              # nothing bought
    assert e._calls_open[s.mint]["mode"] == "practice" and e.lab.open[s.mint][0]["kind"] == "practice-buy"
    assert e.desk_reviews[-1]["kind"] == "practice" and s.late_tried
    e.lab._close(e.lab.open[s.mint][0], s.curve.price * 1.2, e.now + 60, "graduation exit")
    c = e.desk_calls[-1]
    assert c["mode"] == "practice" and c["approve"] and c["pnl_pct"] > 15 and s.mint not in e._calls_open
    assert e.desk_scorecard()["late"]["personas"]["skeptic"]["pass"]["n"] == 1 and e.desk_scorecard()["sniper"]["calls"] == 0
    e.p.desk["practice"] = False                                          # the switch
    assert not e._practicing()


def test_each_persona_reads_its_own_record_at_a_vote():
    e = engine()
    e.desk_calls = [{"symbol": f"C{i}", "approve": True, "pnl_pct": -10.0, "mode": "live",
                     "votes": [["quant", "buy", 58, "EV looks fine"]]} for i in range(9)]
    s = TokenState("R" * 40 + "pump", None, e.now)
    snap, only = e._desk_inputs(s, "late")
    assert only["quant"]["your_record"]["when_you_said_buy"]["avg_pct"] == -10.0
    assert "your_record" not in only.get("veteran", {}) and "narrative_context" in only["narrative"]
    assert "your_record" not in e._desk_inputs(s, "sniper")[1].get("quant", {})


def test_a_side_vote_when_the_rules_decide_graduation_buys(monkeypatch):
    e = engine(graduation_vote=False)
    e.desk = FakeDesk(False, votes(("veteran", "pass")))
    monkeypatch.setattr(e, "_size", lambda *a, **k: 0.05)
    s = TokenState("S" * 40 + "pump", None, e.now)
    s.price_known = True
    e.tokens[s.mint] = s

    async def go():
        await e._enter(s, "late", 60.0, 0.1, ["late play"], source="late")
        for _ in range(5):
            await asyncio.sleep(0)
    asyncio.run(go())
    assert s.mint in e.pending or s.mint in e.positions                   # bought on the rules, though the team passed
    assert e._calls_open[s.mint]["mode"] == "shadow" and not e._calls_open[s.mint]["approve"]


def test_sniper_votes_are_scored_apart_from_graduation_votes(monkeypatch):
    from meme_trader.sniper import desk as deskmod
    e = engine()
    e.desk = FakeDesk(False, votes(("veteran", "pass"), ("quant", "buy")))
    monkeypatch.setattr(deskmod, "snapshot_for", lambda *a, **k: {})
    s = TokenState("N" * 40 + "pump", None, e.now)
    s.price_known = True
    e.tokens[s.mint] = s
    asyncio.run(e._desk_then_buy(s, "sniper", 60, 0.1, [], "sniper", "", 0.0))
    assert e._calls_open[s.mint]["strategy"] == "sniper" and e.lab.open[s.mint][0]["kind"] == "sniper-pass"
    sh = e.lab.open[s.mint][0]
    e.lab._close(sh, s.curve.price * .5, e.now + 30, "stop")
    sc = e.desk_scorecard()
    assert sc["sniper"]["pass"]["n"] == 1 and sc["late"]["calls"] == 0
    assert e.lab.view("sniper")["entries"] == 0                         # a vote's follow isn't a sniper trade
    e.desk_calls += [{"symbol": f"C{i}", "approve": True, "pnl_pct": 5.0, "mode": "live", "strategy": "late",
                      "votes": [["quant", "buy", 58, "late reason"]]} for i in range(9)]
    assert dr.persona_record(e.desk_calls, "quant", strategy="sniper") is None      # 1 sniper call: too few
    assert dr.persona_record(e.desk_calls, "quant", strategy="late")["when_you_said_buy"]["n"] == 9


def test_the_exit_lab_tries_a_curve_ladder_on_sniper_entries():
    from meme_trader.sniper.curve import CURVE_TOKENS, INITIAL_V_TOKENS, Curve
    e = engine(realtime=False)
    s = TokenState("L" * 40 + "pump", None, e.now)
    s.price_known = True
    e.tokens[s.mint] = s
    e.lab.start(s.mint, "LAD", "sniper", s.curve.price, e.now, e.p)
    sh = next(x for x in e.lab.open[s.mint] if x["variant"] == "curve ladder 25/50/75%")
    for prog in (.26, .51, .76):                                       # the curve fills past each rung
        vt = INITIAL_V_TOKENS - prog * CURVE_TOKENS
        s.curve = Curve(30.0 * INITIAL_V_TOKENS / vt, vt)
        e.lab.tick(e.tokens, e.now + 1, e.p)
    assert sh["rung"] == 3 and abs(sh["pos"].tokens / sh["pos"].initial_tokens - .25) < 1e-6 and sh["proceeds"] > 0


def _settle(coro):
    async def go():
        await coro
        for _ in range(5):
            await asyncio.sleep(0)
    asyncio.run(go())


def test_the_team_practices_while_the_kill_switch_is_on(monkeypatch):
    e = engine()
    e.book.halted = "drawdown 50%"
    e.desk = FakeDesk(True, votes(("veteran", "buy"), ("quant", "pass")))
    monkeypatch.setattr("meme_trader.sniper.engine.evaluate_late_entry", lambda *a: (True, "late play"))
    s = TokenState("H" * 40 + "pump", None, e.now)
    s.price_known, s.decided = True, "watching"
    e.tokens[s.mint] = s
    _settle(e._maybe_late())
    assert not e.positions and not e.pending and e._calls_open[s.mint]["mode"] == "practice"
    assert e.lab.open[s.mint][0]["kind"] == "practice-buy" and not e.practicing and not e.reviewing


def test_the_sniper_practices_when_stopped_or_off_and_never_buys(monkeypatch):
    e = engine()
    e.desk = FakeDesk(False, votes(("veteran", "pass"), ("skeptic", "pass")))
    monkeypatch.setattr("meme_trader.sniper.engine.evaluate_entry", lambda *a: NS(action="enter", score=70.0, notes=["ok"]))

    async def no_gate(s):
        return ""
    monkeypatch.setattr(e, "_funding_gate", no_gate)
    for i, (how, off) in enumerate([("halted", False), ("sniper off", True)]):
        e.book.halted, e.p.entry["enabled"] = ("drawdown 50%" if how == "halted" else ""), not off
        s = TokenState(chr(65 + i) * 40 + "pump", None, e.now)
        s.price_known = True
        e.tokens[s.mint] = s
        _settle(e._check_entry(s))
        assert s.decided.startswith("practice vote (") and how in s.decided
        c = e._calls_open[s.mint]
        assert (c["mode"], c["strategy"], c["approve"]) == ("practice", "sniper", False)
        assert e.lab.open[s.mint][0]["kind"] == "sniper-pass"
    assert not e.positions and not e.pending and e.stats.get("skipped_sniper_off", 0) == 0
    assert e.desk_reviews[-1]["kind"] == "sniper practice"


def test_no_practice_for_a_moments_block_and_practice_takes_no_seat(monkeypatch):
    e = engine()
    e.desk = FakeDesk(True, votes(("veteran", "buy")))
    assert not e._practicing("max positions") and not e._practicing("no trades for 90s - entries paused")
    assert e._practicing("halted: drawdown 50%") and e._practicing("daily loss limit")
    e.p.capital["max_open_positions"] = 1
    e.reviewing.add("X"), e.practicing.add("X")
    assert e.entries_blocked() == ""                                    # a practice vote isn't a seat
    e.practicing.clear()
    assert e.entries_blocked() == "max positions"
