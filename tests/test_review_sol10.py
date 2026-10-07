"""A tenth external review (2026-10-07, revision a1544b7): its contracts as this project's tests (the reviewer's own
file is also run as an acceptance check), plus the acceptance cases it listed. Wallet order is judged per slot and
never forgotten by a fold; fork evidence is validated and ranked before any gate moves; quotes have a raw SDK layer
and a checked one; after-exit marks have a strict horizon; the T9 simulation's late price move adds no drift."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meme_trader import config
from meme_trader.sniper import pumpswap as ps
from meme_trader.sniper.copytrade import LeaderBook
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Reconcile, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.exitlab import END_FRESH_S, ExitLab
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState, _content

M = "R" * 40 + "pump"
K = ("S", 0)
P = config.load(config.EXAMPLE)
numpy = importlib.util.find_spec("numpy") is not None


class Live(Feed):
    realtime = True

    async def events(self):
        return
        yield


@pytest.fixture
def make(tmp_path, monkeypatch):
    import meme_trader.journal as jm
    import meme_trader.sniper.engine as em
    monkeypatch.setattr(em, "DATA", tmp_path)
    monkeypatch.setattr(jm, "DATA", tmp_path)

    def engine():
        p = copy.deepcopy(P)
        p.sniper.market["skip_mayhem"] = False
        return Engine(p, Live(), PaperExecutor(p.sniper.execution), persist=False, log_to_journal=False)
    return engine


def tr(slot, qty, side="buy", sig="S", ei=0, sol=1.0, wallet="W"):
    return Trade(M, slot / 100, wallet, side, sol, qty, 30 + qty, 1000 - qty, signature=sig, slot=slot, event_index=ei)


def book(*trades):
    b = LeaderBook([{"address": "W"}])
    for t in trades:
        b.on_trade(t)
    return b


# --------------------------------------------------------------------------- R10-1/2: wallet order, per slot
OPEN = tr(90, 10, sig="OPEN")


@pytest.mark.parametrize("order", [("XS", "XB", "Y"), ("Y", "XS", "XB"), ("XS", "Y", "XB")])
def test_a_slot_with_a_sell_and_several_transactions_is_unknown_in_any_arrival_order(order):
    ev = {"XS": tr(100, 5, "sell", "X", 0), "XB": tr(100, 5, "buy", "X", 1), "Y": tr(100, 5, "buy", "Y", 0, sol=3)}
    assert book(OPEN, *(ev[k] for k in order)).stats["W"].unknown_bags == 1


def test_three_transactions_and_buys_only_and_one_transaction_alone():
    assert book(OPEN, tr(100, 1, sig="A"), tr(100, 1, sig="B"), tr(100, 1, "sell", "C")).stats["W"].unknown_bags == 1
    assert book(OPEN, tr(100, 1, sig="A"), tr(100, 1, sig="B"), tr(100, 1, sig="C")).stats["W"].unknown_bags == 0
    assert book(OPEN, tr(100, 5, "sell", "X", 0), tr(100, 5, "buy", "X", 1)).stats["W"].unknown_bags == 0


def test_a_correction_that_creates_the_same_slot_sell_is_caught():
    buy = tr(100, 5, sig="Y")
    b = book(OPEN, tr(101, 5, "sell", "X"), buy)
    assert b.stats["W"].unknown_bags == 0
    b.replace(tr(101, 5, "sell", "X"), tr(100, 5, "sell", "X"))           # the chain says X was in slot 100
    assert b.stats["W"].unknown_bags == 1


def test_ambiguity_survives_a_fold_at_the_real_threshold_and_a_restart():
    from meme_trader.sniper.copytrade import LEDGER_EVENTS
    b = book(OPEN, tr(100, 10, sig="A"), tr(100, 5, "sell", "B"))
    assert b.stats["W"].unknown_bags == 1
    for i in range(LEDGER_EVENTS + 3):                                       # push the ambiguous slot into the snapshot
        b.on_trade(tr(200 + i, 1, sig=f"F{i}"))
    p = b.pairs[("W", M)]
    assert p.checkpoint >= 100 and p.start_unknown and b.stats["W"].unknown_bags == 1
    again = LeaderBook([{"address": "W"}])
    again.load_state(json.loads(json.dumps(b.to_json())))
    again._rebuild(("W", M))
    again._restat("W")
    assert again.stats["W"].unknown_bags == 1                                # never upgraded to known


def test_the_checkpoint_slots_context_is_kept_for_its_boundary(monkeypatch):
    import meme_trader.sniper.copytrade as cm
    monkeypatch.setattr(cm, "LEDGER_EVENTS", 2)
    b = book(tr(100, 10, sig="A"), tr(101, 1, sig="B"), tr(102, 1, sig="C"))     # slot 100 folded alone
    p = b.pairs[("W", M)]
    assert p.checkpoint == 100 and [t.signature for t in p.boundary] == ["A"]
    assert b.stats["W"].unknown_bags == 0


# --------------------------------------------------------------------------- R10-3: accept, then gate
def proof(qty):
    return json.dumps(list(_content(tr(100, qty))))


def registry(e, method="status", content=None, level="final"):
    e.fork_evidence[K] = {"mint": M, "slot": 100, "level": level, "status": "finalized" if level == "final" else
                          "confirmed", "err": "", "content": content, "method": method, "fault": ""}


@pytest.mark.parametrize("bad", ["not-json", json.dumps({"a": 1}), json.dumps(["W", "buy", 1]),
                                 json.dumps(["W", "hold", 1.0, 10.0, 40.0, 990.0, -1.0, "pump", 0.0]),
                                 json.dumps(["W", "buy", True, 10.0, 40.0, 990.0, -1.0, "pump", 0.0])])
def test_malformed_or_wrong_shape_content_is_no_proof_and_keeps_the_block(make, bad):
    e = make()
    e.fork_unproved[K] = {"mint": M, "since": 0.0, "given_up": False}
    e.fork_pending[K] = {"mint": M, "since": 0.0, "stage": "content", "next": 0.0}
    registry(e)
    e._on_reconcile(Reconcile(2, M, "S", 0, 100, "finalized", "fixture", content=bad))
    assert K in e.fork_unproved and e.fork_pending[K]["stage"] == "content"
    assert e.fork_evidence[K]["content"] is None and e.stats["fork_content_invalid"] == 1


def test_a_wrong_identity_changes_nothing(make):
    e = make()
    registry(e, "tx", list(_content(tr(100, 10))))
    e._on_reconcile(Reconcile(2, "OTHER" * 8, "S", 0, 100, "finalized", "x", err="boom"))
    assert K not in e.fork_unproved and not e.fork_evidence[K]["fault"] and e.stats["fork_registry_mismatch"] == 1


def test_weaker_status_keeps_the_final_proof_and_adds_no_block(make):
    e = make()
    registry(e, "tx", list(_content(tr(100, 10))))
    e._on_reconcile(Reconcile(3, M, "S", 0, 100, "confirmed", "late"))
    assert K not in e.fork_unproved and e.fork_evidence[K]["method"] == "tx"


def test_a_final_status_after_a_confirmed_proof_keeps_that_proof_but_still_needs_final_bytes(make):
    e = make()
    registry(e, "tx", list(_content(tr(100, 10))), level="confirmed")
    e._on_reconcile(Reconcile(3, M, "S", 0, 100, "finalized", "status only"))
    rec = e.fork_evidence[K]
    assert rec["level"] == "final" and rec["method"] == "status" and rec["lower_proof"]["level"] == "confirmed"
    assert K in e.fork_unproved                                              # final bytes still to come
    e._on_reconcile(Reconcile(4, M, "S", 0, 100, "finalized", "tx", content=proof(10)))
    assert K not in e.fork_unproved and e.fork_evidence[K]["method"] == "tx"


def test_different_final_contents_stay_blocked_and_the_lifecycle_waits_for_acceptance(make, tmp_path):
    e = make()
    e.persist = True
    s = e.tokens[M] = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    s.on_trade(tr(100, 10), 10)
    s.on_trade(tr(101, 12), 10)
    s.new_conflicts.clear()
    e.fork_pending[K] = {"mint": M, "since": e.now, "stage": "first"}
    e._on_reconcile(Reconcile(1, M, "S", 0, 100, "finalized", "tx", content="not-json"))   # status only, in effect
    log = tmp_path / "fork_conflicts.jsonl"
    assert not log.exists() or "resolved" not in log.read_text()            # not resolved: content still owed
    assert K in e.fork_unproved and e.fork_pending[K]["stage"] == "content"
    e._on_reconcile(Reconcile(2, M, "S", 0, 100, "finalized", "tx", content=proof(10)))
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert rows[-1]["kind"] == "resolved" and rows[-1]["proof"] == "tx" and K not in e.fork_unproved
    s.on_trade(tr(100, 13), 10)
    e._on_reconcile(Reconcile(3, M, "S", 0, 100, "finalized", "tx", content=json.dumps(list(_content(tr(100, 13))))))
    assert s.conflicts[K]["fault"] and "contradict" in e._authorize(M, 0.01, "late")
    assert e._authorize(M, 0.01, "manual") == ""                              # the owner's trading: never blocked


# --------------------------------------------------------------------------- R10-4: raw vs checked quotes
FEES = {"lp": 20, "protocol": 5, "creator": 30}
TINY = json.loads((Path(__file__).parent / "fixtures" / "pumpswap" / "sdk-tiny-quotes.json").read_text())


@pytest.mark.parametrize("row", TINY["rows"], ids=[str(r["quote"]) for r in TINY["rows"]])
def test_tiny_buys_keep_raw_sdk_parity_and_are_refused_when_checked(row):
    raw = ps.raw_buy_quote_input(row["quote"], 1, 10 ** 12, 50 * 10 ** 9, FEES)
    assert {k: str(v) for k, v in raw.items()} == row["rawSdkOutput"]
    with pytest.raises(ps.QuoteError) as ex:
        ps.buy_quote_input(row["quote"], 1, 10 ** 12, 50 * 10 ** 9, FEES)
    assert ex.value.code == "output"


@pytest.mark.parametrize("call,code", [
    (lambda: ps.buy_base_input(10 ** 6, 1, 10 ** 12, 10 ** 9, FEES, 1 << 80), "state"),       # E beyond u64
    (lambda: ps.sell_base_input(10 ** 6, 1, 10 ** 12, 10 ** 9, FEES, -(10 ** 9)), "state"),   # E = 0
    (lambda: ps.sell_base_input(True, 1, 10 ** 12, 10 ** 9, FEES), "input"),                  # a bool isn't atoms
    (lambda: ps.sell_base_input(10 ** 6, 1, 10 ** 12, 10 ** 9, {"lp": 9000, "protocol": 900, "creator": 100}), "input"),
    (lambda: ps.sell_base_input(10 ** 6, 1, 10 ** 12, 10 ** 9, {"lp": -1, "protocol": 5, "creator": 30}), "input"),
    (lambda: ps.sell_base_input(1, 1, 10 ** 12, 10 ** 9, FEES), "output"),                    # receives nothing
])
def test_the_checked_layer_refuses_unqualified_state_inputs_and_outputs(call, code):
    with pytest.raises(ps.QuoteError) as ex:
        call()
    assert ex.value.code == code


def test_negative_virtual_reserves_and_inverse_sells_through_the_checked_layer():
    out = ps.sell_base_input(10 ** 9, 1, 10 ** 12, 50 * 10 ** 9, FEES, -(10 ** 9))
    assert out == ps.raw_sell_base_input(10 ** 9, 1, 10 ** 12, 50 * 10 ** 9, FEES, -(10 ** 9))
    golden = json.loads((Path(__file__).parent / "fixtures" / "pumpswap" / "pumpswap-sdk-1.20.0-golden.json").read_text())
    short = next(c for c in golden["cases"] if c.get("forwardSellCheck") and not c["forwardSellCheck"]["meetsTarget"])
    i = short["inputs"]
    with pytest.raises(ps.QuoteError, match="short"):
        ps.sell_quote_input(int(i["quote"]), i["slippagePercent"], int(i["baseReserve"]), int(i["quoteReserve"]),
                            ps.parse_fees(short["selectedFeesBps"]), int(i["virtualQuoteReserves"]), i["coinCreator"])


# --------------------------------------------------------------------------- R10-5: the after-exit horizon
def mark(price, known=True, at=None):
    """A coin whose accepted price is `price`; `at` = when it was received (default: the tick that reads it - a new
    observation each tick). An eleventh review: freshness is the price's source time, not the tick's."""
    return SimpleNamespace(price_known=known, curve=SimpleNamespace(price=price), _at=at)


def after(ticks, tmp_path):
    lab = ExitLab(tmp_path / "lab.jsonl", 1.75)
    lab.start(M, "T", "late", 1.0, 0, P.sniper, only=("all out at +10% net",), stake_sol=0.25)
    for i, (t, m) in enumerate([(5, mark(1.2))] + ticks):
        if m is not None:
            at = t if m._at is None else m._at
            m.mark = {"at": at, "seq": at, "chain_ts": at - 1} if m.price_known else {}
        lab.tick({M: m} if m is not None else {}, t, P.sniper)
    return next(json.loads(x) for x in (tmp_path / "lab.jsonl").read_text().splitlines() if "after_exit" in x)


def test_a_fresh_price_at_the_horizon_is_the_end_and_a_later_jump_stays_outside(tmp_path):
    r = after([(900, mark(1.8)), (1800, mark(1.5)), (1801, mark(9))], tmp_path)
    assert r["followed_to_end"] and r["end_after_pct"] == pytest.approx(25) and r["peak_after_pct"] == pytest.approx(50)
    assert r["after_horizon"] is None and r["until"] == 1800


def test_stale_unknown_and_evicted_ends_are_unmeasured_with_reasons(tmp_path):
    stale = after([(1700, mark(1.3)), (1801, mark(2.0))], tmp_path / "a")
    assert stale["end_after_pct"] is None and "stale" in stale["missing_reason"] and stale["end_price_age_s"] == 100
    assert stale["after_horizon"]["delay_s"] == 1 and stale["after_horizon"]["pct"] == pytest.approx(66.67, abs=0.01)
    gone = after([(600, None)], tmp_path / "b")
    assert not gone["followed_to_end"] and "no longer tracked" in gone["missing_reason"]
    unknown_before = after([(1790, mark(1.3)), (1795, mark(0, known=False)), (1801, mark(1.4))], tmp_path / "c")
    assert "unknown at the last observation" in unknown_before["missing_reason"] and unknown_before["unknown_observations"] == 1
    assert END_FRESH_S == 60


# --------------------------------------------------------------------------- R10-6/7: the power simulation
def power():
    spec = importlib.util.spec_from_file_location("power", Path(__file__).resolve().parents[1] / "research/power_t9_e1.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.skipif(not numpy, reason="needs numpy")
def test_the_late_move_is_centered_with_no_drift_and_the_sensitivities_are_labelled():
    import numpy as np
    m = power()
    x, w = np.polynomial.hermite.hermgauss(40)
    z = x * np.sqrt(2)

    def mean(drift, delay=900.0):
        return float(np.dot(w, m._late_return(np.zeros_like(z), 0.0, np.full_like(z, delay), z, drift)) / np.sqrt(np.pi))
    sig2 = m.LATE_SD ** 2 * 900 / 3600
    assert mean("centered") == pytest.approx(0, abs=1e-12)
    assert mean("positive") == pytest.approx((1 + m.FEE) * (np.exp(sig2 / 2) - 1), rel=1e-9)        # v4's +2.2%
    assert mean("negative") == pytest.approx((1 + m.FEE) * (np.exp(-sig2 / 2) - 1), rel=1e-9)
    assert m._late_return(np.array([0.2]), 0.0, np.array([600.0]), np.array([0.0]))[0] == pytest.approx(0.2)   # Z=0
    assert m._late_return(np.array([0.0]), 0.0, np.array([900.0]), np.array([-99.0]))[0] > -1 - m.FEE        # positive


@pytest.mark.skipif(not numpy, reason="needs numpy")
def test_the_late_share_is_of_executed_exits_and_both_arms_are_counted():
    import numpy as np
    m = power()
    out = m.one_test_v5(np.random.default_rng(7), 5, 0.0, 1.0, "flat", "A3", 0.0, 1.0, 40)
    a = out["arms"]["signal"]
    assert out["late_fill_share"] == pytest.approx(a["late_exits"] / a["executed_exits"])
    assert set(out["arms"]) == {"signal", "control"} and a["attempted"] == a["measured"] + a["impaired"]
    assert a["signals"] == a["no_entry_quote"] + a["attempted"] + sum(a["skipped"].values())


def test_an_eleventh_review_a_carried_price_reread_is_not_fresh_and_not_a_new_observation(tmp_path):
    """The price received at 5 and re-read at 1799 and 1800: its age at the horizon is 1795 s, not 0 - the end is
    unmeasured (stale), and it's one observation, not three. A new price received at 1790 is fresh."""
    old = mark(1.3, at=5)
    stale = after([(1799, old), (1800, old)], tmp_path / "a")
    assert stale["end_after_pct"] is None and "stale" in stale["missing_reason"] and stale["end_price_age_s"] == 1795
    assert stale["observations"] == 1 and stale["freshness_rule"].startswith("receipt age")
    fresh = after([(1799, mark(1.3, at=1790)), (1800, mark(1.3, at=1790))], tmp_path / "b")
    assert fresh["followed_to_end"] and fresh["end_price_age_s"] == 10 and fresh["end_price_chain_age_s"] == 11
    no_provenance = SimpleNamespace(price_known=True, curve=SimpleNamespace(price=1.4))      # no mark at all
    lab = ExitLab(tmp_path / "c.jsonl", 1.75)
    lab.start(M, "T", "late", 1.0, 0, P.sniper, only=("all out at +10% net",), stake_sol=0.25)
    for t in (5, 1799, 1800):
        lab.tick({M: no_provenance}, t, P.sniper)
    r = next(json.loads(x) for x in (tmp_path / "c.jsonl").read_text().splitlines() if "after_exit" in x)
    assert not r["followed_to_end"] and r["end_after_pct"] is None and r["observations"] == 0 and r["unknown_observations"] == 2
