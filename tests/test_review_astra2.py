"""A second external review (2026-10-06, revision 38bfd0b): its nine invariants, written as this project's tests
(its own file wasn't run here). Plus duplicates and the owner's reconciliation of an unprovable order."""
import asyncio
import copy
import itertools
import time
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.execution import LiveExecutor, PaperExecutor, SniperFill
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
M = "R" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine(ex=None):
    p = copy.deepcopy(P)
    p.sniper.execution["paper_delay_s"] = 0
    return Engine(p, Quiet(), ex or PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=False)


def coin(**kw):
    s = TokenState(M, Launch(M, 0.0, "DEV", symbol="RV"), 0.0)
    s.price_known = True
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# 1. Mayhem found while the AI team voted: no buy
def test_a_coin_found_to_be_mayhem_during_the_vote_isnt_bought():
    e = engine()
    s = coin(mayhem=True)
    e.tokens[M] = s
    e._size = lambda *a, **k: 0.01
    verdict = NS(approve=True, summary="approved", size_mult=1.0, votes=[])
    asyncio.run(e._after_review(s, "sniper", verdict, time.time(), s.curve.price, 80, 0.01, [], "sniper", ""))
    assert M not in e.positions and M not in e.pending
    assert e._authorize(M, 0.01, "late") == "Mayhem mode" and e._authorize(M, 0.01, "manual") != "Mayhem mode"


# 2. balances don't depend on arrival order
def test_a_sell_arriving_before_its_buy_nets_to_zero():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_trade(Trade(M, 10, "W", "sell", 1, 10, 30, 100, slot=102), 3)
    s.on_trade(Trade(M, 11, "W", "buy", 1, 10, 33.333333, 90, slot=101), 3)
    assert s.holders.get("W", 0) == 0


# 3. a slot whose reserves revisit a state, delivered in any order
def test_a_buy_sell_buy_slot_ends_right_in_every_arrival_order():
    moves = [("buy", 10e6, 90e6), ("sell", 5e6, 95e6), ("buy", 5e6, 90e6)]           # 100M -> 90M -> 95M -> 90M
    for order in itertools.permutations(range(3)):
        s = TokenState(M, Launch(M, 0, "DEV"), 0)
        for i in order:
            side, amount, vt = moves[i]
            s.on_trade(Trade(M, 10 + i, "W", side, 1, amount, 3e9 / vt, vt, slot=200), 3)
        assert s.curve.v_tokens == 90e6, order


def test_the_same_trade_delivered_twice_counts_once():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    t = Trade(M, 10, "W", "buy", 1, 10e6, 31, 1.063e9, signature="SIG", slot=5)
    s.on_trade(t, 3)
    s.on_trade(t, 3)
    assert s.holders["W"] == 10e6 and s.buys == 1 and s.volume_sol == 1


# 4. confirmed, amounts not retrievable: no guessed fill
def test_a_confirmed_buy_without_its_amounts_stays_pending(monkeypatch):
    import httpx

    import meme_trader.sniper.execution as xm
    import meme_trader.wallet as wm
    ex = copy.deepcopy(P.sniper.execution)
    ex["verify_tx"] = False

    class W:
        pubkey = "W"

        def sign(self, *a):
            return "raw", "sig"

        def blockhash_of(self, *a):
            return "BH"

        def send(self, *a):
            return "sig"

        def tx_deltas(self, *a):
            return None

        def token_balance(self, *a):
            return 1_500_000                      # may include an earlier position: not this order's amount
    monkeypatch.setattr(httpx, "post", lambda *a, **k: NS(status_code=200, content=b"tx"))
    monkeypatch.setattr(wm, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(xm.time, "sleep", lambda *a: None)
    f = LiveExecutor(ex, W())._attempt(M, "buy", 0.01, True, 15)
    assert f.unknown and f.landed and not f.ok and f.blockhash == "BH"


# 5. no blockhash: no amount of waiting proves "never landed"
def test_a_missing_blockhash_never_expires_by_the_clock():
    class W:
        def tx_deltas(self, *a):
            return None

        def signature_status(self, *a):
            return None
    f = asyncio.run(LiveExecutor(P.sniper.execution, W()).resolve("sig", M, "buy", blockhash="", age_s=10 ** 6))
    assert f.unknown and not f.expired


# 9. a known landing is remembered, and then nothing can call it "never landed"
def test_known_landing_evidence_is_kept_and_wins():
    class KnownLanded:
        answer = SniperFill(False, unknown=True, expired=True, signature="S")

        async def buy(self, *a, **k):
            return SniperFill(False, unknown=True, landed=True, signature="S", blockhash="BH",
                              error="confirmed; amounts not visible yet")

        async def resolve(self, sig, mint, side, blockhash="", age_s=0.0):
            return self.answer
    ex = KnownLanded()
    e = engine(ex)
    s = coin()
    e.tokens[M] = s
    asyncio.run(e._buy(s, 80, 0.01, [], "sniper"))
    assert e.unresolved["S"]["landed"] and e.unresolved["S"]["blockhash"] == "BH"
    asyncio.run(e._resolve_unresolved())                 # the chain later says "not found + expired"...
    assert "S" in e.unresolved and M in e.book.reserved  # ...but it landed: still held, never released
    assert e.reconcile_unresolved("S", "not landed").startswith("the chain showed")


def test_the_owner_settles_an_order_with_no_proof_either_way():
    class Silent:
        async def buy(self, *a, **k):
            return SniperFill(False, unknown=True, signature="Q")

        async def resolve(self, sig, mint, side, blockhash="", age_s=0.0):
            return SniperFill(False, unknown=True, signature=sig)
    e = engine(Silent())
    s = coin()
    e.tokens[M] = s
    asyncio.run(e._buy(s, 80, 0.01, [], "sniper"))
    asyncio.run(e._resolve_unresolved())
    assert "Q" in e.unresolved
    assert e.reconcile_unresolved("Q", "not landed") == "" and M not in e.book.reserved and "Q" not in e.unresolved


def test_the_dexscreener_bots_live_executor_is_blocked():
    from meme_trader.agents.executor import Executor
    import meme_trader.clients.jupiter as jup
    p = NS(mode="live", execution=NS())
    real = jup.has_key
    jup.has_key = lambda: True
    try:
        with pytest.raises(RuntimeError, match="blocked"):
            Executor(p, wallet=object())
    finally:
        jup.has_key = real
