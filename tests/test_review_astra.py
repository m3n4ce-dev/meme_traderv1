"""An external review (2026-10-06): unknown transactions, take profits spent without a sale, prices rolled back by
late trades, paper sells that hide fees, Mayhem supply. Plus the owner's call: the bots stay out of Mayhem coins."""
import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper.curve import Curve
from meme_trader.sniper.engine import UNRESOLVED_ALERT_S, Engine
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.execution import LiveExecutor, PaperExecutor, SniperFill
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.strategy import SniperPosition, evaluate_entry, evaluate_late_entry
from meme_trader.sniper.tracker import MAYHEM_AGENT, TokenState

P = config.load(config.EXAMPLE)
M = "Q" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


# --------------------------------------------------------------------------- A: "never landed" needs proof
class Chain:
    """A wallet whose RPC answers are scripted."""
    def __init__(self, deltas=None, status=None, valid=True, fail=None):
        self.deltas, self.status, self.valid, self.fail = deltas, status, valid, fail

    def tx_deltas(self, sig, mint):
        if self.fail == "tx":
            raise RuntimeError("RPC getTransaction: timeout")
        return self.deltas

    def signature_status(self, sig):
        if self.fail == "status":
            raise RuntimeError("RPC getSignatureStatuses: 503")
        return self.status

    def blockhash_valid(self, bh):
        return self.valid

    def token_balance(self, mint):
        return 0


def resolve(chain, **kw):
    return asyncio.run(LiveExecutor(P.sniper.execution, chain).resolve("sig", M, "buy", **kw))


def test_an_unknown_order_counts_as_never_landed_only_with_proof():
    assert resolve(Chain(fail="tx"), blockhash="bh", age_s=9999).unknown and not resolve(Chain(fail="tx"), age_s=9999).expired
    assert not resolve(Chain(fail="status"), blockhash="bh", age_s=9999).expired     # history unavailable: no proof
    f = resolve(Chain(status=None, valid=True), blockhash="bh", age_s=9999)
    assert f.unknown and not f.expired                                                  # its blockhash still works
    f = resolve(Chain(status=None, valid=False), blockhash="bh")
    assert f.unknown and f.expired                                                      # not in history + expired
    f = resolve(Chain(status={"err": None, "confirmationStatus": "confirmed"}), blockhash="bh", age_s=9999)
    assert f.unknown and f.landed and not f.expired                                    # landed: wait for its amounts
    f = resolve(Chain(status={"err": {"InstructionError": [0, "x"]}}), blockhash="bh")
    assert not f.ok and not f.unknown and f.fees_lost > 0                              # landed and failed
    assert not resolve(Chain(status=None), age_s=60).expired                            # no blockhash: too young
    assert resolve(Chain(status=None), age_s=200).expired                               # ...older than any blockhash


class Unknown:
    def __init__(self):
        self.answer = SniperFill(False, unknown=True, signature="b1", error="status lookup failed")

    async def buy(self, mint, curve, sol, priority=None):
        return SniperFill(False, unknown=True, signature="b1", blockhash="BH1")

    async def resolve(self, sig, mint, side, blockhash="", age_s=0.0):
        self.asked = (blockhash, age_s)
        return self.answer


def test_the_engine_holds_an_unproven_order_and_tells_you():
    ex = Unknown()
    e = Engine(copy.deepcopy(P), Quiet(), ex, mode="paper", log_to_journal=False, persist=False)
    s = TokenState(M, Launch(M, 0.0, "dev", symbol="UNK"), 0.0)
    s.price_known = True
    e.tokens[M] = s

    async def go():
        await e._buy(s, 70, 0.05, ["x"])
        e.unresolved["b1"]["sent_at"] -= UNRESOLVED_ALERT_S + 1
        await e._resolve_unresolved()
        await e._resolve_unresolved()
    asyncio.run(go())
    assert ex.asked[0] == "BH1" and M in e.book.reserved and "b1" in e.unresolved
    assert sum("still unknown" in x["text"] for x in e.log) == 1                      # told once
    ex.answer = SniperFill(False, unknown=True, expired=True, signature="b1")
    asyncio.run(e._resolve_unresolved())
    assert M not in e.book.reserved and "b1" not in e.unresolved


# --------------------------------------------------------------------------- B: a take profit is spent by a fill
class Rejects(PaperExecutor):
    ok = False

    async def sell(self, mint, curve, tokens, priority=None, steps=None):
        if not self.ok:
            return SniperFill(False, error="slippage")
        return await super().sell(mint, curve, tokens, priority, steps)


def held(e, manual=None, bot=""):
    s = TokenState(M, Launch(M, 0.0, "dev", symbol="TP"), 0.0)
    s.price_known = True
    s.curve = Curve(60.0, 500_000_000.0)                                  # well above the entry below
    e.tokens[M] = s
    pos = SniperPosition(mint=M, symbol="TP", opened_at=0, entry_price=s.curve.price / 2.5, tokens=1e6, initial_tokens=1e6,
                         cost_sol=0.1, initial_cost_sol=0.1, score=50, peak_price=s.curve.price, source="manual", exits=[],
                         manual=manual, bot=bot, handed_price=s.curve.price / 2.5, handed_peak=s.curve.price / 2.5)
    e.positions[M] = pos
    return s, pos


def test_a_failed_take_profit_sell_doesnt_spend_it():
    ex = Rejects(P.sniper.execution)
    e = Engine(copy.deepcopy(P), Quiet(), ex, mode="paper", log_to_journal=False, persist=False)
    s, pos = held(e, manual={"tp": 50, "tp_frac": 0.5})
    asyncio.run(e._check_exit(s))
    assert not pos.manual.get("tp_done") and pos.tokens == 1e6                         # rejected: still owed
    ex.ok = True
    asyncio.run(e._check_exit(s))
    assert pos.manual["tp_done"] and pos.tokens == pytest.approx(5e5)                 # filled: spent
    e2 = Engine(copy.deepcopy(P), Quiet(), Rejects(P.sniper.execution), mode="paper", log_to_journal=False, persist=False)
    s2, pos2 = held(e2, bot="ride")                                                   # handed to the bots: 2.5x
    asyncio.run(e2._check_exit(s2))
    assert not pos2.ride_tp and pos2.tokens == 1e6


# --------------------------------------------------------------------------- C: chain order, not arrival order
def trade(slot, side, tokens, v_tokens, v_sol=40.0):
    return Trade(M, 0.0, "w", side, 0.1, tokens, v_sol, v_tokens, slot=slot)


def test_a_late_trade_doesnt_roll_the_price_back():
    s = TokenState(M, Launch(M, 0.0, "dev", symbol="C"), 0.0)
    s.on_trade(trade(102, "buy", 10e6, 700e6, 45.0), 5)
    s.on_trade(trade(101, "buy", 10e6, 710e6, 44.0), 5)                     # an older slot, delivered late
    assert s.curve.v_tokens == 700e6
    # one slot, three buys A (720->710) -> B (710->700) -> C (700->690), delivered C, A, B
    s2 = TokenState(M, Launch(M, 0.0, "dev", symbol="C"), 0.0)
    for vt in (690e6, 710e6, 700e6):
        s2.on_trade(trade(200, "buy", 10e6, vt), 5)
        assert s2.curve.v_tokens == 690e6                                   # always the newest state of the slot
    s3 = TokenState(M, Launch(M, 0.0, "dev", symbol="C"), 0.0)             # no slots (synthetic): arrival order
    s3.on_trade(trade(0, "buy", 10e6, 700e6), 5)
    s3.on_trade(trade(0, "buy", 10e6, 710e6), 5)
    assert s3.curve.v_tokens == 710e6


# --------------------------------------------------------------------------- D, E
def test_a_paper_sell_can_cost_more_than_it_fetches_and_mayhem_shares_use_2b():
    dust = asyncio.run(PaperExecutor(P.sniper.execution).sell(M, Curve(30.0, 1_073_000_000.0), 1.0))
    assert dust.ok and dust.sol < 0                                          # the fee is bigger than the proceeds
    s = TokenState(M, Launch(M, 0.0, "dev", symbol="MH"), 0.0)
    s.holders["whale"] = 100e6
    assert s.top_holders_pct(1) == pytest.approx(10.0)
    s.on_trade(Trade(M, 1.0, MAYHEM_AGENT, "buy", 0.1, 1e6, 31, 1.07e9), 5)
    assert s.mayhem and s.top_holders_pct(1) == pytest.approx(5.0)          # 100M of 2B minted


# --------------------------------------------------------------------------- the owner: no Mayhem coins
def test_the_bots_stay_out_of_mayhem_coins_but_your_trades_dont():
    s = TokenState(M, Launch(M, 0.0, "dev", symbol="MH"), 0.0)
    s.price_known, s.mayhem = True, True
    d = evaluate_entry(s, 5.0, P.sniper.entry, {"skip_mayhem": True})
    assert d.action == "reject" and "Mayhem mode" in d.notes
    s.curve = Curve(60.0, 500_000_000.0)
    red = {"max_bundle_pct": 100, "max_early_sold_ratio": 9, "creator_launches": 0, "max_creator_launches_24h": 9,
           "max_cluster_pct": 100, "skip_mayhem": True}
    assert evaluate_late_entry(s, 60.0, P.sniper.late, red) == (False, "Mayhem mode")
    e = Engine(copy.deepcopy(P), Quiet(), PaperExecutor(P.sniper.execution), mode="paper", log_to_journal=False, persist=False)
    assert e._ctx(s)["skip_mayhem"] and e._copy_blocked(s, NS(pool="pump", sol=1.0, tokens=1.0, trader="w")) == "Mayhem mode"
    s2, mine = held(e)                                                        # yours: never touched
    s2.mayhem = True
    asyncio.run(e._check_exit(s2))
    assert mine.tokens == 1e6
    mine.source = "late"                                                      # a bot's: out
    asyncio.run(e._check_exit(s2))
    assert M not in e.positions
