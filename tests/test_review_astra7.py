"""A seventh external review (2026-10-07, revision 0f9ab0a): its contracts as this project's tests (the reviewer's own
file is also run as an acceptance check). Fork evidence has levels and can be revised; failed transactions are
retracted; known blocks survive restarts; only logical events reach wallet-following; the T9 account is one
event-driven model; every cash change is a typed event."""
import asyncio
import copy
import json

import pytest

from meme_trader import config
from meme_trader.sniper.copytrade import LeaderBook
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Reconcile, Trade
from meme_trader.sniper.execution import PaperExecutor, SniperFill
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.t9_portfolio import DELAY_S, HOLD_S, RETRY_S, Portfolio
from meme_trader.sniper.tracker import TokenState

M = "R" * 40 + "pump"
P = config.load(config.EXAMPLE)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


@pytest.fixture
def make(tmp_path, monkeypatch):
    import meme_trader.journal as jm
    import meme_trader.sniper.engine as em
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)

    def engine(persist=False, journal=False):
        p = copy.deepcopy(P)
        p.sniper.market["skip_mayhem"] = False
        p.sniper.execution["paper_delay_s"] = 0
        e = Engine(p, Quiet(), PaperExecutor(p.sniper.execution), persist=persist, log_to_journal=False)
        e.journal = journal
        return e
    return engine


def tr(slot, qty, side="buy", wallet="W", sig="S", ei=0, sol=1.0):
    return Trade(M, slot / 100, wallet, side, sol, qty, 30 + qty, 1000 - qty, signature=sig, slot=slot, event_index=ei)


def coin():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    return s


def forked():
    s = coin()
    s.on_trade(tr(100, 10), 10)
    s.on_trade(tr(101, 12), 10)
    return s


# --------------------------------------------------------------------------- fork evidence
def test_confirmed_is_usable_but_a_finalized_answer_revises_it_and_weaker_never_overrides_final():
    s = forked()
    assert s.resolve_conflict(("S", 0), 101, "confirmed") == "kept" and not s.unsafe and s.holders["W"] == 12
    assert s.resolve_conflict(("S", 0), 100, "finalized") == "replaced" and s.holders["W"] == 10
    assert s.conflicts[("S", 0)]["status"] == "final"
    assert s.resolve_conflict(("S", 0), 101, "confirmed") == "" and s.holders["W"] == 10      # weaker: ignored
    assert s.resolve_conflict(("S", 0), 101, "finalized") == "unresolvable" and s.unsafe     # final vs final


def test_new_content_after_resolution_reopens_and_asks_again():
    s = forked()
    s.resolve_conflict(("S", 0), 101, "finalized")
    s.new_conflicts.clear()
    assert s.on_trade(tr(102, 14), 10) == "conflict"
    assert s.unsafe and s.new_conflicts == [("S", 0)] and s.holders["W"] == 14     # provisional: the latest slot


def test_a_failed_transaction_is_retracted_and_the_coin_stays_flagged():
    s = forked()
    s.on_trade(tr(103, 5, wallet="OTHER", sig="T"), 10)
    assert s.resolve_conflict(("S", 0), 101, "finalized", err='{"InstructionError": [1, "x"]}') == "retracted"
    assert "W" not in s.holders and s.holders["OTHER"] == 5 and s.buys == 1 and s.unsafe
    assert s.replacements[-1][1] is None                                           # downstream: retract it


def test_two_differing_copies_from_one_slot_cant_be_told_apart():
    s = coin()
    s.on_trade(tr(100, 10), 10)
    s.on_trade(tr(100, 11), 10)
    assert s.resolve_conflict(("S", 0), 100, "finalized") == "unresolvable" and s.unsafe


def test_the_engine_rechecks_confirmed_until_finalized_and_rejects_failed_or_malformed(make, monkeypatch):
    e = make()
    e.tokens[M] = forked()
    e.tokens[M].new_conflicts.clear()                     # (on_trade's fan-out already queued it, as below)
    e.fork_pending[("S", 0)] = {"mint": M, "since": e.now, "stage": "first"}
    answers = iter([[{"slot": 101, "confirmationStatus": "confirmed"}],                       # no "err": malformed
                    [{"slot": 101, "confirmationStatus": "confirmed", "err": None}],
                    [{"slot": 101, "confirmationStatus": "finalized", "err": None}]])
    monkeypatch.setattr(Engine, "_status_lookup", staticmethod(lambda *a: next(answers)))
    monkeypatch.setattr(e, "_fork_rpc", lambda: ("https://x.invalid", "x.invalid"))

    async def ask():
        e.fork_pending[("S", 0)]["next"] = 0
        await e._reconcile_forks()
    asyncio.run(ask())
    assert e.tokens[M].conflicts[("S", 0)]["status"] == "unresolved"            # malformed: not evidence
    asyncio.run(ask())
    assert e.tokens[M].conflicts[("S", 0)]["status"] == "confirmed" and e.fork_pending[("S", 0)]["stage"] == "final"
    asyncio.run(ask())
    assert e.tokens[M].conflicts[("S", 0)]["status"] == "final" and ("S", 0) not in e.fork_pending


def test_known_blocks_and_pending_lookups_survive_a_restart_for_held_and_unheld_coins(make):
    e = make(persist=True)
    s = e.tokens[M] = forked()
    e._apply_buy(s, SniperFill(True, sol=0.05, tokens=1e6), 70, [], "manual", "")
    e.fork_blocks["UNHELD" + "x" * 34] = "fork conflict unresolved"
    e.fork_pending[("S", 0)] = {"mint": M, "since": e.now, "stage": "final"}
    e.save_state()
    again = make(persist=True)
    asyncio.run(again.restore_state())
    assert again._authorize(M, 0.01, "late") and again.tokens[M].unsafe
    assert again._authorize("UNHELD" + "x" * 34, 0.01, "late") == "fork conflict unresolved"
    assert again.fork_pending[("S", 0)]["stage"] == "final"
    assert again._authorize(M, 0.01, "manual") == ""                              # manual trading is never blocked


# --------------------------------------------------------------------------- wallet-following sees logical events
def follower(make, monkeypatch):
    e = make()
    e.leaders = LeaderBook([{"address": "W", "label": "w", "mode": "signal"}])
    e.tokens[M] = coin()

    async def nothing(*a):
        return None
    monkeypatch.setattr(e, "_evaluate", nothing)
    monkeypatch.setattr(e, "_known_wallets", lambda: {})
    return e


def test_duplicate_buys_and_sells_count_once_and_distinct_events_twice(make, monkeypatch):
    e = follower(make, monkeypatch)

    async def go():
        for t in (tr(100, 10), tr(100, 10), tr(100, 10, ei=1), tr(101, 4, side="sell", sig="X"),
                  tr(101, 4, side="sell", sig="X")):
            await e.handle(t)
    asyncio.run(go())
    assert e.leaders.bag("W", M).tokens == pytest.approx(16) and e.leaders.stats["W"].trades == 3
    assert e.tokens[M].holders["W"] == pytest.approx(16)
    assert sum(r[2] for r in e.pulse) == 3                                          # market stats: events, not deliveries


def test_a_proven_replacement_and_a_retraction_correct_the_followed_wallets_bag(make, monkeypatch):
    e = follower(make, monkeypatch)

    async def go():
        await e.handle(tr(100, 10))
        await e.handle(tr(101, 12))                                                 # the later version, provisionally
        assert e.leaders.bag("W", M).tokens == pytest.approx(12)
        await e.handle(Reconcile(2, M, "S", 0, 100, "finalized", "test"))
        assert e.leaders.bag("W", M).tokens == pytest.approx(10)
        await e.handle(Reconcile(3, M, "S", 0, 100, "finalized", "test", err="failed"))
    asyncio.run(go())
    assert e.leaders.bag("W", M).tokens == pytest.approx(0) and e.leaders.stats["W"].trades == 0


# --------------------------------------------------------------------------- the T9 account
def test_the_account_runs_out_of_cash_instead_of_borrowing():
    p = Portfolio(end_t=30 * 86400, trace=True)
    for d in range(30):
        for c in range(4):
            p.try_enter(d * 86400 + 10, (d, c), -1.0)
    p.close()
    assert p.cash >= -1e-9 and sum(p.daily(30)) >= -9.0 - 1e-9
    assert any(x["kind"] == "skip" and x["why"] == "cash" for x in p.trace)


def test_exits_land_on_their_own_utc_day_and_count_against_that_days_stop():
    p = Portfolio(end_t=3 * 86400)
    t0 = 86400 - DELAY_S - 1800                                                    # enters 23:30, exits 00:30
    p.try_enter(t0, "a", -0.9)
    p.try_enter(86400 + 3700, "b", 0.1)                                            # after a's exit: day 1's stop?
    p.close()
    assert p.daily(3)[0] == 0 and p.daily(3)[1] == pytest.approx(0.25 * -0.9 + 0.25 * 0.1)
    p2 = Portfolio(end_t=3 * 86400)
    for i in range(3):
        p2.try_enter(86400 + i * 4000, ("x", i), -0.9)                             # 0.225 lost each on day 1
    assert p2.try_enter(86400 + 3 * 4000, "y", 0.1) == "daily stop"


def test_unmeasured_exits_hold_capacity_and_capital_then_are_written_off():
    p = Portfolio(end_t=86400)
    for i in range(6):
        p.try_enter(10, i, None)
    assert p.attempted == 4 and p.cash == pytest.approx(9.0 - 1.0)
    assert p.try_enter(10 + HOLD_S + 10, "late", 0.1) == "max open"                # still retrying the exits
    p.settle(10 + DELAY_S + HOLD_S + RETRY_S)
    assert p.trapped == 4 and p.cash == pytest.approx(8.0) and p.conservative == pytest.approx(4 * 0.25 * -1.012)


def test_nothing_enters_that_couldnt_close_inside_the_window():
    p = Portfolio(end_t=86400)
    assert p.try_enter(86400 - HOLD_S, "z", 0.1) == "window"


# --------------------------------------------------------------------------- typed economic events
def test_the_account_journal_rebuilds_cash_exactly(make, tmp_path):
    e = make(journal=True)
    s = e.tokens[M] = coin()
    s.price_known = True
    start = e.book.sol

    async def go():
        e._apply_buy(s, SniperFill(False, fees_lost=0.002), 70, [], "late", "")          # a failed buy: no position
        e._apply_buy(s, SniperFill(True, sol=0.05, tokens=2e6), 70, [], "late", "")
        e._apply_sell(s, e.positions[M], SniperFill(False, fees_lost=0.001), "retry")
        e._apply_sell(s, e.positions[M], SniperFill(True, sol=0.07, tokens=2e6), "exit")
        e.deposit_paper(1.0)
    asyncio.run(go())
    rows = [json.loads(x) for x in (tmp_path / "account-paper.jsonl").read_text().splitlines()]
    assert {r["account"] for r in rows} == {e.book.account_id}
    assert start + sum(r["sol"] for r in rows) == pytest.approx(e.book.sol)
    kinds = [r["kind"] for r in rows]
    assert kinds.count("failed_fee") == 2 and "close" in kinds and kinds[-1] == "deposit"
    unattached = [r for r in rows if r["kind"] == "failed_fee" and not r.get("attached")]
    assert len(unattached) == 1 and unattached[0]["sol"] == pytest.approx(-0.002)
    old = e.book.account_id
    assert e.reset_paper() == "" and e.book.account_id != old
    reset = json.loads((tmp_path / "account-paper.jsonl").read_text().splitlines()[-1])
    assert reset["kind"] == "reset" and reset["previous"] == old


def test_the_account_id_survives_a_restart_and_an_old_book_is_adopted(make):
    e = make(persist=True, journal=True)
    e.save_state()
    again = make(persist=True, journal=True)
    asyncio.run(again.restore_state())
    assert again.book.account_id == e.book.account_id
