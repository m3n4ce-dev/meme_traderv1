"""An eighth external review (2026-10-07, revision 59ae17a): its contracts as this project's tests (the reviewer's own
file is also run as an acceptance check). Fork evidence keeps its strongest answer apart from the resolution state;
followed wallets' accounting is replayed, not adjusted; the T9 account's primary P&L is its economic P&L; the
reviewer's +10%-net take-profit runs in the exit lab's shadow."""
import asyncio
import base64
import copy
import json
import struct
from dataclasses import asdict

import pytest
from solders.pubkey import Pubkey

from meme_trader import config
from meme_trader.sniper.copytrade import LEDGER_EVENTS, LeaderBook
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Reconcile, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.exitlab import TP10, ExitLab
from meme_trader.sniper.feeds import PUMP_PROGRAM, TRADE_EVENT, Feed
from meme_trader.sniper.t9_portfolio import DELAY_S, HOLD_S, RETRY_S, SIZE, Portfolio
from meme_trader.sniper.tracker import TokenState, _content

M = "R" * 40 + "pump"
P = config.load(config.EXAMPLE)
K = ("S", 0)


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

    def engine(persist=False):
        p = copy.deepcopy(P)
        p.sniper.market["skip_mayhem"] = False
        return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), persist=persist, log_to_journal=False)
    return engine


def tr(slot, qty, side="buy", wallet="W", sig="S", ei=0, sol=1.0):
    return Trade(M, slot / 100, wallet, side, sol, qty, 30 + qty, 1000 - qty, signature=sig, slot=slot, event_index=ei)


def forked():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    s.on_trade(tr(100, 10), 10)
    s.on_trade(tr(101, 12), 10)
    return s


# --------------------------------------------------------------------------- R8-A: evidence precedence
def test_weaker_evidence_never_changes_state_success_or_failure():
    s = forked()
    s.resolve_conflict(K, 100, "finalized")
    assert s.resolve_conflict(K, 101, "confirmed", err="failed") == "" and s.holders["W"] == 10   # weaker failure
    s2 = forked()
    s2.resolve_conflict(K, 101, "finalized", err="failed")
    assert "W" not in s2.holders
    assert s2.resolve_conflict(K, 100, "confirmed") == "" and "W" not in s2.holders                # weaker success
    assert s2.conflicts[K]["strongest"]["err"] and s2.unsafe


def test_new_content_after_final_evidence_blocks_but_keeps_the_evidence_until_reverified():
    s = forked()
    s.resolve_conflict(K, 100, "finalized")
    s.on_trade(tr(102, 14), 10)
    assert s.unsafe and s.holders["W"] == 10 and s.conflicts[K]["strongest"]["slot"] == 100
    assert s.resolve_conflict(K, 102, "confirmed") == "" and s.unsafe          # weaker: can't displace final
    assert s.resolve_conflict(K, 100, "finalized") == "kept" and not s.unsafe  # the same final answer, checked again
    s.on_trade(tr(103, 15), 10)
    assert s.resolve_conflict(K, 103, "finalized") == "unresolvable" and s.conflicts[K]["fault"] and s.unsafe


def test_new_content_after_a_finalized_failure_stays_retracted_and_blocked():
    s = forked()
    s.resolve_conflict(K, 101, "finalized", err="failed")
    s.on_trade(tr(102, 14), 10)
    assert "W" not in s.holders and s.unsafe
    assert s.resolve_conflict(K, 101, "finalized", err="failed") == "retracted" and s.unsafe


def test_contradictory_finalized_answers_are_an_integrity_fault_for_good():
    s = forked()
    s.resolve_conflict(K, 100, "finalized", "A")
    assert s.resolve_conflict(K, 101, "finalized", "B", "failed") == "unresolvable"
    assert s.conflicts[K]["fault"] and s.holders["W"] == 10
    assert s.resolve_conflict(K, 100, "finalized", "A") == "" and s.unsafe     # nothing settles it after that


def test_the_same_evidence_twice_has_no_second_effect():
    s = forked()
    s.resolve_conflict(K, 101, "confirmed", err="failed")
    s.replacements.clear()
    assert s.resolve_conflict(K, 101, "confirmed", err="failed") == "" and not s.replacements
    s.resolve_conflict(K, 101, "finalized", err="failed")
    assert not s.replacements                                                     # already retracted: no None -> None


def test_same_slot_copies_need_the_transactions_content():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    s.on_trade(tr(100, 10), 10)
    s.on_trade(tr(100, 11), 10)
    assert s.resolve_conflict(K, 100, "finalized") == "unresolvable"             # the slot can't tell them apart
    s2 = TokenState(M, Launch(M, 0, "DEV"), 0)
    s2.on_launch(s2.launch)
    s2.on_trade(tr(100, 10), 10)
    s2.on_trade(tr(100, 11), 10)
    assert s2.resolve_conflict(K, 100, "finalized", content=_content(tr(100, 11))) == "replaced"
    assert s2.holders["W"] == 11 and not s2.unsafe and s2.conflicts[K]["strongest"]["method"] == "tx"
    s3 = forked()                                                                 # content no delivered version has
    assert s3.resolve_conflict(K, 101, "confirmed", content=_content(tr(101, 99))) == "unresolvable" and s3.unsafe


def test_confirmed_answers_failed_or_not_are_rechecked_until_finalized(make):
    e = make()
    e.tokens[M] = forked()
    e.tokens[M].new_conflicts.clear()                       # (on_trade's fan-out already queued it, as here)
    e.fork_pending[K] = {"mint": M, "since": 0, "stage": "first"}
    asyncio.run(e.handle(Reconcile(1, M, "S", 0, 101, "confirmed", "t", err="failed")))
    assert e.fork_pending[K]["stage"] == "final" and e.tokens[M].unsafe
    asyncio.run(e.handle(Reconcile(2, M, "S", 0, 100, "finalized", "t")))
    assert K not in e.fork_pending and e.tokens[M].holders["W"] == 10 and not e.tokens[M].unsafe


def test_evidence_and_its_precedence_survive_a_restart(make):
    e = make(persist=True)
    e.tokens[M] = forked()
    asyncio.run(e.handle(Reconcile(1, M, "S", 0, 100, "finalized", "t")))
    e.fork_blocks[M] = "fork conflict unresolved"
    e.save_state()
    again = make(persist=True)
    asyncio.run(again.restore_state())
    assert again.fork_evidence[K]["level"] == "final" and again.fork_evidence[K]["slot"] == 100
    asyncio.run(again.handle(Reconcile(2, M, "S", 0, 101, "confirmed", "t", err="failed")))   # weaker: ignored
    assert again.fork_evidence[K]["slot"] == 100 and not again.fork_evidence[K]["err"]
    asyncio.run(again.handle(Reconcile(3, M, "S", 0, 101, "finalized", "t")))                  # contradicts final
    assert again.fork_evidence[K]["fault"] and "contradict" in again._authorize(M, 0.01, "late")
    assert again._authorize(M, 0.01, "manual") == ""                                          # manual: never blocked


# --------------------------------------------------------------------------- transaction-content verification
def event_log(user: bytes, sol: int, tokens: int, buy=True, v_sol=31_000_000_000, v_tokens=1_000_000_000_000):
    raw = (TRADE_EVENT + bytes(range(100, 132)) + struct.pack("<QQ?", sol, tokens, buy) + user
           + struct.pack("<qQQQQ", 0, v_sol, v_tokens, 0, 0))
    return "Program data: " + base64.b64encode(raw + bytes(250)).decode()


def test_the_transaction_itself_is_decoded_with_the_feeds_parser(monkeypatch):
    user = bytes(range(32))
    logs = [f"Program {PUMP_PROGRAM} invoke [1]", event_log(user, 10**9, 12 * 10**6), f"Program {PUMP_PROGRAM} success"]
    monkeypatch.setattr(Engine, "_tx_lookup", staticmethod(
        lambda url, sig, c: {"slot": 101, "meta": {"err": None, "logMessages": logs}}))
    content, how = Engine._tx_content("https://x.invalid", K, 101, "confirmed")
    assert how == "tx_decoded"
    got = json.loads(content)
    assert got[0] == str(Pubkey.from_bytes(user)) and got[1] == "buy" and got[2] == 1.0 and got[3] == 12.0
    assert Engine._tx_content("https://x.invalid", K, 102, "confirmed") == ("", "tx_unknown")        # another slot
    assert Engine._tx_content("https://x.invalid", ("S", 1), 101, "confirmed") == ("", "tx_unknown")  # no such event
    monkeypatch.setattr(Engine, "_tx_lookup", staticmethod(
        lambda url, sig, c: {"slot": 101, "meta": {"err": None, "logMessages": logs[:1] + ["Log truncated"]}}))
    assert Engine._tx_content("https://x.invalid", K, 101, "confirmed") == ("", "tx_unknown")        # truncated


def test_a_lookup_pass_records_the_decoded_content_and_matches_by_it(make, monkeypatch):
    e = make()
    s = e.tokens[M] = forked()
    s.new_conflicts.clear()
    e.fork_pending[K] = {"mint": M, "since": e.now, "stage": "first"}
    monkeypatch.setattr(Engine, "_status_lookup", staticmethod(
        lambda url, sigs: [{"slot": 100, "confirmationStatus": "finalized", "err": None}]))
    monkeypatch.setattr(e, "_fork_rpc", lambda: ("https://x.invalid", "x.invalid"))
    monkeypatch.setattr(Engine, "_tx_content", classmethod(
        lambda cls, url, k, slot, c: (json.dumps(list(_content(tr(100, 10)))), "tx_decoded")))
    asyncio.run(e._reconcile_forks())
    c = s.conflicts[K]
    assert c["status"] == "final" and c["strongest"]["method"] == "tx" and s.holders["W"] == 10
    assert "getTransaction" in c["strongest"]["source"] and e.stats["fork_tx_decoded"] == 1


# --------------------------------------------------------------------------- R8-B: followed wallets, replayed
def book(*events):
    b = LeaderBook([{"address": "W"}, {"address": "X"}])
    for t in events:
        b.on_trade(t)
    return b


def same(a: LeaderBook, b: LeaderBook, wallets=("W", "X")):
    for w in wallets:
        assert asdict(a.bag(w, M)) == pytest.approx(asdict(b.bag(w, M)))
        sa, sb = asdict(a.stats[w]), asdict(b.stats[w])
        assert sa == pytest.approx(sb), (w, sa, sb)


BUY, BUY12 = tr(100, 10), tr(101, 12)


@pytest.mark.parametrize("sell", [tr(103, 4, "sell", sol=2, sig="P"), tr(103, 10, "sell", sol=2, sig="F")])
def test_a_corrected_buy_before_a_later_sell_equals_a_fresh_replay(sell):
    got = book(BUY, sell)
    got.replace(BUY, BUY12)
    same(got, book(BUY12, sell))


def test_a_corrected_sell_retraction_reappearance_and_multiple_corrections():
    sell, sell2 = tr(103, 10, "sell", sol=2, sig="T"), tr(104, 8, "sell", sol=3, sig="T")
    got = book(BUY, sell)
    got.replace(sell, sell2)                                                       # the sell itself corrected
    same(got, book(BUY, sell2))
    got.replace(BUY, None)                                                         # retracted
    same(got, book(sell2))
    got.replace(None, BUY12)                                                       # reinstated, in another version
    same(got, book(BUY12, sell2))
    got.replace(BUY12, BUY)
    same(got, book(BUY, sell2))


def test_a_correction_that_changes_the_trader_moves_the_event():
    sell = tr(103, 5, "sell", sol=2, sig="T")
    got = book(BUY, sell)
    moved = tr(101, 10, wallet="X")
    got.replace(BUY, moved)
    same(got, book(sell, moved))
    assert got.stats["W"].unknown_bags == 1                     # W now sold what it was never seen buying


def test_an_unknown_opening_inventory_is_marked_not_counted_as_profit():
    b = book(tr(100, 5), tr(101, 10, "sell", sol=4, sig="T"))
    st = b.stats["W"]
    assert st.unknown_bags == 1 and st.realized_sol == pytest.approx(4 * 5 / 10 - 1)   # only the tokens seen bought
    assert book(tr(101, 10, "sell", sol=4, sig="T")).stats["W"].unknown_bags == 1


def test_late_arrivals_in_an_earlier_slot_are_replayed_in_chain_order():
    sell = tr(103, 10, "sell", sol=2, sig="T")
    got = book(sell, BUY)                                       # the buy's notification came late
    same(got, book(BUY, sell))


def test_folded_history_and_restart_keep_replay_equivalence(make):
    trades = [tr(100 + i, 1, sig=f"B{i}") for i in range(LEDGER_EVENTS + 5)]
    got = book(*trades)
    assert len(got.pairs[("W", M)].events) == LEDGER_EVENTS
    got.replace(trades[0], tr(100, 2, sig="B0"))                # folded into the snapshot: can't be replayed
    assert got.stats["W"].unknown_bags == 1
    e = make(persist=True)
    e.leaders = book(BUY, tr(103, 4, "sell", sol=2, sig="P"))
    e.save_state()
    again = make(persist=True)
    asyncio.run(again.restore_state())
    again.leaders.replace(BUY, BUY12)
    same(again.leaders, book(BUY12, tr(103, 4, "sell", sol=2, sig="P")))


def test_our_copied_results_are_never_touched_by_a_correction():
    b = book(BUY)
    b.on_copy_closed("W", 0.02, 5)
    b.replace(BUY, None)
    assert b.stats["W"].copied == 1 and b.stats["W"].copied_pnl == pytest.approx(0.02) and b.stats["W"].trades == 0


# --------------------------------------------------------------------------- R8-C: the T9 account's contract
def test_impairments_are_cash_primary_pnl_and_risk_on_the_day_they_are_recognized():
    p = Portfolio(end_t=3 * 86400, trace=True)
    t0 = 86400 - DELAY_S - HOLD_S - 100                     # its exit is due on day 0; the retry runs out on day 1
    assert p.try_enter(t0, "A", None) == ""
    p.close()
    assert p.daily(3)[0] == 0 and p.daily(3)[1] == pytest.approx(-SIZE * 1.012)
    assert p.cash - 9.0 == pytest.approx(sum(p.daily(3))) and p.measured_daily(3) == [0, 0, 0]
    assert p.lost[1] == pytest.approx(SIZE * 1.012) and [x["kind"] for x in p.trace][-1] == "impaired"


def test_a_failed_exit_reserves_risk_while_retried_and_the_coin_stays_quarantined():
    p = Portfolio(end_t=86400, day_stop=0.25)
    p.try_enter(0, "A", None)
    t = HOLD_S + 100                                        # A's exit failed; it's still being retried
    assert p.try_enter(t - DELAY_S, "B", 0.1) == "daily stop"
    p.settle(DELAY_S + HOLD_S + RETRY_S)
    p2 = Portfolio(end_t=86400)
    p2.try_enter(0, "A", None)
    p2.settle(DELAY_S + HOLD_S + RETRY_S)
    assert p2.try_enter(DELAY_S + HOLD_S + RETRY_S + 10, "A", 0.1) == "same coin"   # its tokens aren't disposed of


# --------------------------------------------------------------------------- the take-profit experiment (shadow)
def test_ten_percent_net_means_net_of_both_fees_and_both_transactions():
    from meme_trader.sniper.strategy import SniperPosition
    lab = ExitLab(None, 1.75)
    pos = SniperPosition(mint="m", symbol="s", opened_at=0, entry_price=1.0, tokens=1.0, initial_tokens=1.0,
                         cost_sol=1.0, initial_cost_sol=1.0, score=0, peak_price=1.0, exits=[], source="late")
    sh = {"proceeds": 0.0, "pos": pos, "tx": 0.001005 / 0.25}
    assert lab.net_gain_pct(sh, 1.10) == pytest.approx(5.4, abs=0.1)        # +10% on the price is ~+5% net
    assert lab.net_gain_pct(sh, 1.148) == pytest.approx(10.0, abs=0.1)      # +10% net needs ~+15%
    assert set(TP10) == {"all out at +10% net", "trim 25% at +10% net"}


def test_the_exit_lab_runs_both_tp10_variants_on_every_bot_entry():
    from meme_trader.sniper.curve import Curve
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    lab = ExitLab(None, 1.75)
    lab.start(M, "T", "late", s.curve.price, 0, P.sniper, stake_sol=0.25)
    names = {sh["variant"] for sh in lab.open[M]}
    assert set(TP10) <= names and "as now" in names
    s.curve = Curve(s.curve.v_sol * 1.25, s.curve.v_tokens)   # the price is up 25%: past +10% net
    lab.tick({M: s}, 5, P.sniper)
    done = {r["variant"]: r for r in lab.done}
    assert done["all out at +10% net"]["why"].startswith("take +") and done["all out at +10% net"]["pnl_pct"] > 10
    trim = next(sh for sh in lab.open.get(M, ()) if sh["variant"] == "trim 25% at +10% net")
    assert trim.get("trim_done") and trim["pos"].tokens == pytest.approx(0.75 * trim["pos"].initial_tokens)
