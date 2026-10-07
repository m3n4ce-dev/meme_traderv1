"""Always practicing (the owner, 2026-10-07): entries the bot can't make - the sniper is off, the kill switch, a
pause - are followed in the exit lab with every exit variant. Measurement only: nothing is bought."""
import asyncio
import copy
from types import SimpleNamespace

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState

M = "P" * 40 + "pump"
P = config.load(config.EXAMPLE)


class Live(Feed):
    realtime = True

    async def events(self):
        return
        yield


class Replay(Live):
    realtime = False


def engine(tmp_path, monkeypatch, feed):
    import meme_trader.journal as jm
    import meme_trader.sniper.engine as em
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)
    p = copy.deepcopy(P)
    p.sniper.market["skip_mayhem"] = False
    p.sniper.entry["enabled"] = False                       # the sniper is off, as the owner has it
    e = Engine(p, feed, PaperExecutor(p.sniper.execution), persist=False, log_to_journal=False)
    monkeypatch.setattr("meme_trader.sniper.engine.evaluate_entry",
                        lambda *a: SimpleNamespace(action="enter", score=80.0, notes=["would enter"]))
    s = e.tokens[M] = TokenState(M, Launch(M, 0, "DEV", symbol="PR"), 0)
    s.on_launch(s.launch)
    return e, s


def test_the_sniper_keeps_practicing_while_off(tmp_path, monkeypatch):
    e, s = engine(tmp_path, monkeypatch, Live())
    asyncio.run(e._check_entry(s))
    kinds = {sh["kind"] for sh in e.lab.open[M]}
    assert kinds == {"sniper-blocked"} and len(e.lab.open[M]) > 5          # every sniper exit variant
    assert s.decided.startswith("followed while blocked") and M not in e.positions and not e.book.reserved
    assert all(sh["entry"]["price_kind"] == "raw_mark" for sh in e.lab.open[M])
    assert e.lab.view("sniper")["entries"] == 0                             # never mixed with real sniper trades


def test_a_kill_switch_halt_is_practiced_too(tmp_path, monkeypatch):
    e, s = engine(tmp_path, monkeypatch, Live())
    e.p.entry["enabled"] = True
    e.book.halted = "drawdown 50%"
    asyncio.run(e._check_entry(s))
    assert {sh["kind"] for sh in e.lab.open[M]} == {"sniper-blocked"} and "halted" in s.decided


def test_replays_and_a_full_book_dont_follow(tmp_path, monkeypatch):
    e, s = engine(tmp_path, monkeypatch, Replay())
    asyncio.run(e._check_entry(s))
    assert s.decided == "sniper off" and M not in e.lab.open                 # replays: unchanged
    e2, s2 = engine(tmp_path, monkeypatch, Live())
    e2.p.entry["enabled"] = True
    monkeypatch.setattr(e2, "entries_blocked", lambda *a: "max positions (3)")
    asyncio.run(e2._check_entry(s2))
    assert M not in e2.lab.open and not s2.decided                           # a moment's block: may still be bought


def test_practice_and_pass_follows_run_every_exit_variant_but_score_on_as_now(tmp_path, monkeypatch):
    from meme_trader.sniper.exitlab import TP10
    e, s = engine(tmp_path, monkeypatch, Live())
    e.lab.start(M, "PR", "practice-buy", s.curve.price, e.now, e.p, price_kind="raw_mark")
    names = {sh["variant"] for sh in e.lab.open[M]}
    assert set(TP10) <= names and "as now" in names
    e._calls_open[M] = {"ts": e.now, "mint": M, "kind": "late"}
    e._score_call({"variant": "all out at +10% net", "kind": "practice-buy", "mint": M, "opened": e.now, "pnl_pct": 9})
    assert M in e._calls_open                                   # another variant doesn't score the team's call
