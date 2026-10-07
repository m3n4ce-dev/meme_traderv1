"""A ninth external review (2026-10-07, revision 8e05c96): its contracts as this project's tests (the reviewer's own
file is also run as an acceptance check). The exit lab keeps one cash ledger; fork evidence keeps status facts,
decoded content and inferences apart, and needs complete logs; the wallet ledger survives pruning and refuses to
call out-of-order history exact; the T9 simulation books a recovered exit when its quote came, not when it was due."""
import asyncio
import base64
import copy
import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from meme_trader import config
from meme_trader.sniper.copytrade import LeaderBook
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Reconcile, Trade
from meme_trader.sniper.execution import PaperExecutor, SniperFill
from meme_trader.sniper.exitlab import ACCOUNTING, ExitLab
from meme_trader.sniper.feeds import PUMP_PROGRAM, TRADE_EVENT, Feed, SolanaTradeFeed
from meme_trader.sniper.t9_portfolio import DELAY_S, HOLD_S, RETRY_S, SIZE, Portfolio
from meme_trader.sniper.tracker import TokenState, _content

M = "R" * 40 + "pump"
K = ("S", 0)
P = config.load(config.EXAMPLE)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


class Live(Quiet):
    realtime = True
    t = 1000.0

    def now(self):
        return self.t


@pytest.fixture
def make(tmp_path, monkeypatch):
    import meme_trader.journal as jm
    import meme_trader.sniper.engine as em
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)

    def engine(persist=False, feed=None):
        p = copy.deepcopy(P)
        p.sniper.market["skip_mayhem"] = False
        return Engine(p, feed or Quiet(), PaperExecutor(p.sniper.execution), persist=persist, log_to_journal=False)
    return engine


def tr(slot, qty, side="buy", wallet="W", sig="S", ei=0, sol=1.0, mint=M):
    return Trade(mint, slot / 100, wallet, side, sol, qty, 30 + qty, 1000 - qty, signature=sig, slot=slot,
                 event_index=ei)


def coin(*trades):
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    for t in trades:
        s.on_trade(t, 10)
    return s


def mark(price):
    return SimpleNamespace(price_known=True, curve=SimpleNamespace(price=price, progress=0.5))


# --------------------------------------------------------------------------- R9-1 / R9-2: one cash ledger in the lab
def reconciles(row):
    return row["net"] == pytest.approx(sum(x["sol"] for x in row["cash"]) - 1, abs=1e-8) and \
        row["pnl_pct"] == pytest.approx(row["net"] * 100, abs=0.006)


def test_every_closed_row_reconciles_to_its_cash_lines_full_trim_dust_and_negative():
    lab = ExitLab(None, 1.75)
    lab.start(M, "T", "late", 1.0, 0, P.sniper, only=("trim 25% at +10% net", "all out at +10% net"), stake_sol=0.25)
    lab.tick({M: mark(1.16)}, 5, P.sniper)                  # full exit, and a trim
    trim = next(sh for sh in lab.open[M] if not sh.get("closed"))
    lab._close(trim, 1.20, 6, "fixture")
    lab.start("D" * 44, "D", "late", 1.0, 0, P.sniper, only=("all out at +10% net",), stake_sol=0.25)
    dust = lab.open["D" * 44][0]
    lab._close(dust, 1e-9, 7, "fixture: worthless")         # the sell's network cost exceeds what it fetches
    rows = lab.done
    assert len(rows) == 3 and all(r["accounting"] == ACCOUNTING and reconciles(r) for r in rows)
    assert rows[-1]["net"] < -1                              # dust: worse than losing the stake, as on chain
    assert [x["status"] for x in rows[1]["cash"]] == ["filled", "filled"] and rows[1]["cash"][0]["frac"] == 0.25


def test_a_bot_entry_uses_its_own_fill_and_an_observation_pays_entry_costs_once():
    fill = SniperFill(True, sol=0.251, tokens=0.25)          # 0.25 SOL stake + 0.001 network, fees inside the tokens
    lab = ExitLab(None, 1.75)
    lab.start(M, "T", "late", fill.price, 0, P.sniper, only=("as now",), entry={"cash_sol": fill.sol, "tokens": fill.tokens})
    sh = lab.open[M][0]
    tx = (P.sniper.execution.priority_fee_sol + 0.000005) / fill.sol
    assert sh["entry"]["price_kind"] == "all_in_fill" and sh["tx"] == pytest.approx(tx)
    assert lab.net_gain_pct(sh, 1.2) == pytest.approx(((0.25 / 0.251) * 1.2 * (1 - 0.0175) - tx - 1) * 100)
    lab2 = ExitLab(None, 1.75)
    lab2.start(M, "T", "desk-pass", 1.0, 0, P.sniper, only=("as now",), stake_sol=0.25, price_kind="raw_mark")
    e = lab2.open[M][0]["entry"]
    assert e["cash_sol"] == pytest.approx(0.25 + 0.001005) and e["tokens"] == pytest.approx(0.25 * 0.9825 / 0.251005)


def late_lab():
    p = copy.deepcopy(P)
    p.sniper.execution["paper_delay_s"] = 2.5
    return ExitLab(None, 1.75), p.sniper


def test_late_landing_variants_keep_a_partial_fraction_and_retry_failed_sells(monkeypatch):
    lab, sp = late_lab()
    lab.start(M, "T", "late", 1.0, 0, sp, only=("trim 25% at +10% net · lands 2.5 s late",), stake_sol=0.25)
    sh = lab.open[M][0]
    fails = iter([True, False])
    monkeypatch.setattr(ExitLab, "_fails", staticmethod(lambda sh: next(fails)))
    lab.tick({M: mark(1.16)}, 5, sp)                          # decided: a 25% trim, sent
    assert sh["pending"]["frac"] == 0.25 and not sh["cash"]
    lab.tick({M: mark(1.10)}, 7.5, sp)                        # lands: fails, pays its network cost, retried
    assert [x["status"] for x in sh["cash"]] == ["failed"] and sh["cash"][0]["sol"] == pytest.approx(-sh["tx"])
    lab.tick({M: mark(1.12)}, 10, sp)                         # lands at the price THEN, a quarter of the bag
    assert sh["cash"][-1]["status"] == "filled" and sh["cash"][-1]["frac"] == 0.25 and sh["cash"][-1]["price"] == 1.12
    assert sh["pos"].tokens == pytest.approx(0.75 * sh["pos"].initial_tokens) and not sh.get("closed")


def test_a_late_landing_bot_shadow_doesnt_buy_again_and_failed_exits_eventually_count_as_lost(monkeypatch):
    lab, sp = late_lab()
    lab.start(M, "T", "late", 1.0, 0, sp, only=("all out at +10% net · lands 2.5 s late",), stake_sol=0.25)
    sh = lab.open[M][0]
    assert sh["land_at"] == 0                                 # the bot's fill already landed
    monkeypatch.setattr(ExitLab, "_fails", staticmethod(lambda sh: True))
    t = 5.0
    lab.tick({M: mark(1.2)}, t, sp)
    for _ in range(30):
        t += 2.5
        lab.tick({M: mark(1.2)}, t, sp)
    row = lab.done[-1]
    assert row["why"] == "exit never filled" and row["sell_tries"] == 20 and reconciles(row)
    assert row["net"] == pytest.approx(-1 - 20 * sh["tx"])


def test_the_price_after_an_early_exit_is_followed_to_the_thirty_minute_mark(tmp_path):
    lab = ExitLab(tmp_path / "lab.jsonl", 1.75)
    lab.start(M, "T", "late", 1.0, 0, P.sniper, only=("all out at +10% net",), stake_sol=0.25)
    lab.tick({M: mark(1.2)}, 5, P.sniper)
    assert lab.done and M in lab.mints()                      # still followed after the exit
    lab.tick({M: mark(2.4)}, 600, P.sniper)
    lab.tick({M: mark(1.8)}, 1801, P.sniper)
    after = [json.loads(x) for x in (tmp_path / "lab.jsonl").read_text().splitlines() if "after_exit" in x]
    a = after[0]
    assert a["peak_after_pct"] == pytest.approx(100)                       # in the window
    assert a["end_after_pct"] is None and "stale" in a["missing_reason"]   # (10th review) last price 1200 s old
    assert a["after_horizon"]["pct"] == pytest.approx(50) and a["after_horizon"]["delay_s"] == 1
    assert ExitLab(tmp_path / "lab.jsonl", 1.75).done == [r for r in lab.done]     # reload: close rows only


# --------------------------------------------------------------------------- R9-3 / R9-4: status, content, inference
def test_status_facts_content_proofs_and_inferences_are_kept_apart():
    s = coin(tr(100, 10), tr(101, 12))
    s.resolve_conflict(K, 100, "finalized", "status")
    st = s.conflicts[K]["strongest"]
    assert st["method"] == "status" and st["content"] is None and st["inferred"] == list(_content(tr(100, 10)))
    s.on_trade(tr(100, 11), 10)
    assert s.resolve_conflict(K, 100, "finalized", "tx", content=_content(tr(100, 11))) == "replaced"
    assert s.holders["W"] == 11 and not s.unsafe and s.conflicts[K]["strongest"]["method"] == "tx"
    s.on_trade(tr(100, 13), 10)                                    # two PROVED finalized contents that differ
    assert s.resolve_conflict(K, 100, "finalized", "tx", content=_content(tr(100, 13))) == "unresolvable"
    assert "contents contradict" in s.conflicts[K]["fault"] and s.unsafe


def test_a_finalized_slot_contradiction_is_still_a_fault():
    s = coin(tr(100, 10), tr(101, 12))
    s.resolve_conflict(K, 100, "finalized", "status")
    assert s.resolve_conflict(K, 101, "finalized", "tx", content=_content(tr(101, 12))) == "unresolvable"
    assert "slot or failure" in s.conflicts[K]["fault"]


def test_the_registry_keeps_content_upgrades_status_answers_and_checks_identity(make):
    e = make()
    e._registry_evidence(Reconcile(1, M, "S", 0, 100, "finalized", "status"), K)
    assert e.fork_evidence[K]["content"] is None and e.fork_evidence[K]["method"] == "status"
    e._registry_evidence(Reconcile(2, M, "S", 0, 100, "finalized", "tx", content=json.dumps(list(_content(tr(100, 10))))), K)
    assert e.fork_evidence[K]["method"] == "tx" and e.fork_evidence[K]["content"] == list(_content(tr(100, 10)))
    e._registry_evidence(Reconcile(3, "OTHER" * 8, "S", 0, 100, "finalized", "x", err="boom"), K)
    assert not e.fork_evidence[K]["fault"] and e.stats["fork_registry_mismatch"] == 1   # another coin: not compared
    e._registry_evidence(Reconcile(4, M, "S", 0, 100, "finalized", "tx", content=json.dumps(list(_content(tr(100, 11))))), K)
    assert "contents contradict" in e.fork_evidence[K]["fault"] and "contradict" in e.fork_blocks[M]


def test_status_only_answers_keep_bot_buys_blocked_until_the_content_is_verified(make, monkeypatch):
    e = make(feed=Live())
    s = e.tokens[M] = coin(tr(100, 10), tr(101, 12))
    s.new_conflicts.clear()
    e.fork_pending[K] = {"mint": M, "since": e.now, "stage": "first"}
    asyncio.run(e.handle(Reconcile(1, M, "S", 0, 101, "finalized", "status")))   # status only: the slot, no bytes
    assert not s.unsafe and "isn't verified" in e._authorize(M, 0.01, "late")
    assert e._authorize(M, 0.01, "manual") == ""                                   # manual trading: never blocked
    assert e.fork_pending[K]["stage"] == "content"                                 # bytes still being fetched
    asyncio.run(e.handle(Reconcile(2, M, "S", 0, 101, "finalized", "tx", content=json.dumps(list(_content(tr(101, 12)))))))
    assert K not in e.fork_unproved and K not in e.fork_pending and e._authorize(M, 0.01, "late") == ""


def test_the_content_stage_retries_then_gives_up_visibly(make, monkeypatch):
    e = make(feed=Live())
    e.persist = True
    e.tokens[M] = coin(tr(100, 10), tr(101, 12))
    e.fork_pending[K] = {"mint": M, "since": 900.0, "stage": "content", "next": 0}
    e.fork_unproved[K] = {"mint": M, "since": 900.0, "given_up": False}
    monkeypatch.setattr(e, "_fork_rpc", lambda: ("https://x.invalid", "x.invalid"))
    monkeypatch.setattr(Engine, "_status_lookup", staticmethod(
        lambda url, sigs: [{"slot": 101, "confirmationStatus": "finalized", "err": None}]))
    e.now = e.feed.t = 1000.0
    asyncio.run(e._reconcile_forks())                       # the fetch fails (no network in tests): retried later
    assert e.fork_pending[K]["next"] == 1000 + 5 and not e.fork_unproved[K]["given_up"]
    e.now = e.feed.t = 2000.0                               # past FORK_GIVE_UP_S since the conflict
    e.fork_pending[K]["next"] = 0
    asyncio.run(e._reconcile_forks())
    assert K not in e.fork_pending and e.fork_unproved[K]["given_up"] and e.stats["fork_content_given_up"] == 1
    e.tokens[M].conflicts[K]["status"] = "final"            # even if the coin itself looked settled
    assert "isn't verified" in e._authorize(M, 0.01, "late")


# --------------------------------------------------------------------------- R9-5: complete logs, and the verifier
def event(user=bytes(range(32)), sol=10 ** 9, tokens=12 * 10 ** 6, quote=b""):
    raw = (TRADE_EVENT + bytes(range(100, 132)) + struct.pack("<QQ?", sol, tokens, True) + user
           + struct.pack("<qQQQQ", 0, 31 * 10 ** 9, 10 ** 12, 0, 0))
    raw = raw + bytes(250) if not quote else raw + bytes(258 - len(raw)) + struct.pack("<I", 0) + b"\x00" + bytes(32) \
        + struct.pack("<I", 0) + quote
    return "Program data: " + base64.b64encode(raw).decode()


PUMP_IN, PUMP_OK = f"Program {PUMP_PROGRAM} invoke [1]", f"Program {PUMP_PROGRAM} success"
OTHER = "Ev1L" + "1" * 39


def decode(logs, ei=0):
    return [t for t in SolanaTradeFeed.parse_logs({"err": None, "logs": logs}, 0.0, 100, complete=True)
            if t.event_index == ei]


def test_the_verifier_needs_a_closed_stack_and_handles_siblings_nesting_and_bad_data():
    assert decode([PUMP_IN, event()]) == []                                            # never closed
    assert decode([PUMP_IN, event(), "Log truncated"]) == []
    assert len(decode([PUMP_IN, event(), PUMP_OK])) == 1
    assert len(decode([f"Program {OTHER} invoke [1]", f"Program {PUMP_PROGRAM} invoke [2]", event(), PUMP_OK,
                       f"Program {OTHER} success", PUMP_IN, event(tokens=5 * 10 ** 6), PUMP_OK], ei=1)) == 1
    assert decode([PUMP_IN, "Program data: !!!not base64", event(), PUMP_OK])[0].tokens == 12   # junk skipped
    usdc = bytes(range(200, 232))
    two = [PUMP_IN, event(quote=usdc), event(tokens=7 * 10 ** 6), PUMP_OK]          # a non-SOL sibling keeps its index
    assert decode(two, 0) == [] and decode(two, 1)[0].tokens == 7
    feed_before = SolanaTradeFeed.unclosed_logs
    assert len(SolanaTradeFeed.parse_logs({"err": None, "logs": [PUMP_IN, event()]}, 0.0, 100)) == 1   # the live feed
    assert SolanaTradeFeed.unclosed_logs == feed_before + 1                                           # counts it


# --------------------------------------------------------------------------- R9-6 / R9-7: the wallet ledger
def test_a_pruned_tombstone_leaves_no_index_and_a_late_reinstatement_is_marked_unknown():
    b = LeaderBook([{"address": "W"}])
    old = tr(100, 10)
    b.on_trade(old)
    b.replace(old, None)
    b._newest_ts = 7200
    b._prune()
    assert K not in b.where and not b.pairs and K in b.folded_ids
    b.replace(None, old)
    assert b.stats["W"].unknown_bags == 1 and b.bag("W", M).tokens == 0                # not counted again


def test_an_arrival_older_than_the_fold_checkpoint_survives_restart_as_unknown(monkeypatch):
    import meme_trader.sniper.copytrade as cm
    monkeypatch.setattr(cm, "LEDGER_EVENTS", 1)
    b = LeaderBook([{"address": "W"}])
    b.on_trade(tr(100, 10, sig="BUY"))
    b.on_trade(tr(200, 5, "sell", sol=2, sig="SELL"))
    assert b.pairs[("W", M)].checkpoint == 100 and not b.stats["W"].unknown_bags
    again = LeaderBook([{"address": "W"}])
    again.load_state(json.loads(json.dumps(b.to_json())))
    again.on_trade(tr(50, 5, "sell", sol=2, sig="OLD"))
    assert again.stats["W"].unknown_bags == 1


def test_two_transactions_in_one_slot_with_a_sell_are_not_claimed_in_order():
    b = LeaderBook([{"address": "W"}])
    b.on_trade(tr(100, 10, sig="A"))
    b.on_trade(tr(100, 4, "sell", sol=2, sig="B"))
    assert b.stats["W"].unknown_bags == 1
    c = LeaderBook([{"address": "W"}])
    c.on_trade(tr(100, 10, sig="A"))
    c.on_trade(tr(100, 4, sig="B"))                                   # two buys: order doesn't change the books
    assert c.stats["W"].unknown_bags == 0


# --------------------------------------------------------------------------- R9-8: the T9 simulation's exit clock
def power():
    spec = importlib.util.spec_from_file_location("power", Path(__file__).resolve().parents[1] / "research/power_t9_e1.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class NoFailure:
    def random(self, shape):
        import numpy as np
        return np.full(shape, 0.5)


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None, reason="needs numpy")
def test_a_recovered_exit_is_booked_when_its_quote_came_with_risk_reserved_meanwhile():
    """The reviewer's contract on the fixed path: `_arm_quotes` returns the first valid quote's time, and the account
    is told it (their version of this test drives the old call sequence, which can't carry that time)."""
    import numpy as np
    m = power()
    m.AVAILABILITY["fixture"] = (48, 10, 0, 0, 0, 0)
    starts, ends = [3650], [4250]
    entry, first = m._arm_quotes(NoFailure(), starts, ends, np.array([60.0]), np.array([0.2]), "fixture")
    assert entry[0] and first[0] == 4260                     # the first 30 s retry after the outage
    port = m.Portfolio(2 * 86400, day_stop=0.5, trace=True)
    m._run_arm(port, np.array([0.0]), ["M"], np.array([0.2]), entry, first, 0.0, np.array([0.0]))
    assert port.risk_used(4000) == pytest.approx(SIZE * 1.012)    # a failed exit, being retried: risk reserved
    port.close()
    exit_t = next(r["t"] for r in port.trace if r["kind"] == "exit")
    assert exit_t == 4260 and m._up(starts, ends, np.array([exit_t]))[0]


def test_the_account_holds_cash_and_books_the_day_of_a_late_fill_and_rejects_impossible_ones():
    p = Portfolio(end_t=3 * 86400, trace=True)
    t0 = 86400 - DELAY_S - HOLD_S - 60                       # due 60 s before midnight; filled 5 min after it
    p.try_enter(t0, "A", 0.1, exit_at=86400 + 300 - 60 + 60)
    p.settle(86400 - 1)
    assert p.cash == pytest.approx(9.0 - SIZE)               # still out: the fill hasn't happened
    p.close()
    assert p.daily(2) == [0.0, pytest.approx(SIZE * 0.1)]
    with pytest.raises(ValueError):
        Portfolio(86400).try_enter(0, "B", 0.1, exit_at=DELAY_S + HOLD_S + RETRY_S + 1)


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None, reason="needs numpy")
def test_transient_failures_come_in_streaks_and_unquotable_pools_dont_see_returns():
    import numpy as np
    m = power()
    m.AVAILABILITY["streaky"] = (1e9, 1, 0, 0, 1.0, 0.0)     # every live attempt fails, each blocking a streak
    rng = np.random.default_rng(1)
    entry, first = m._arm_quotes(rng, [], [], np.zeros(200), np.zeros(200), "streaky")
    assert not first.any()
    m.AVAILABILITY["dead"] = (1e9, 1, 0, 0, 0.0, 0.5)
    r = np.where(np.arange(4000) % 2 == 0, -0.9, 0.5)
    _, first = m._arm_quotes(np.random.default_rng(2), [], [], np.zeros(4000), r, "dead")
    lost_losers, lost_winners = (first[r < 0] == 0).mean(), (first[r > 0] == 0).mean()
    assert abs(lost_losers - lost_winners) < 0.05            # base case: independent of the return
    m.AVAILABILITY["A3 informative"] = (1e9, 1, 0, 0, 0.0, 0.2)
    _, first = m._arm_quotes(np.random.default_rng(3), [], [], np.zeros(4000), r, "A3 informative")
    assert (first[r < 0] == 0).mean() > 2 * (first[r > 0] == 0).mean()   # the labelled stress


# --------------------------------------------------------------------------- the exit lab while entries are blocked
def test_blocked_graduation_entries_are_followed_with_every_exit_variant_and_change_nothing_else(make, monkeypatch):
    from meme_trader.sniper.exitlab import TP10
    e = make(feed=Live())
    e.book.halted = "drawdown 50%"
    s = coin()
    s.decided, s.price_known = "watching", True
    e.tokens[M] = s
    monkeypatch.setattr("meme_trader.sniper.engine.evaluate_late_entry", lambda *a: (True, "late play"))
    asyncio.run(e._maybe_late())
    names = {sh["variant"] for sh in e.lab.open[M]}
    assert set(TP10) <= names and "as now" in names and all(sh["kind"] == "late-blocked" for sh in e.lab.open[M])
    assert not s.late_tried and M not in e.positions and not e.book.reserved          # nothing bought or changed
    assert all(sh["entry"]["price_kind"] == "raw_mark" for sh in e.lab.open[M])
    n = len(e.lab.open[M])
    e._last_late_scan = 0
    asyncio.run(e._maybe_late())
    assert len(e.lab.open[M]) == n                                                      # once per coin
