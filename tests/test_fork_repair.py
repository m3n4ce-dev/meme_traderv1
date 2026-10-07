"""Fork repair (a sixth review, 2026-10-07): a trade event delivered twice with different content is a fork. The coin
is flagged, rebuilt provisionally with the later slot's version, and made final only by the chain's answer (a
recorded Reconcile event), so replays repair it the same way."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Reconcile, Trade, dumps, loads
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState

M = "F" * 40 + "pump"
P = config.load(config.EXAMPLE)


def tr(sig, slot, wallet, qty, vs, ei=0, ts=None, side="buy", sol=1.0):
    return Trade(M, ts if ts is not None else slot / 100, wallet, side, sol, qty, vs, 1000 - qty, signature=sig,
                 slot=slot, event_index=ei)


def coin():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.on_launch(s.launch)
    return s


def test_the_same_event_again_counts_once_even_from_another_slot():
    s = coin()
    s.on_trade(tr("S", 100, "W", 10, 31), 10)
    s.on_trade(tr("S", 101, "W", 10, 31), 10)            # same content, the fork's next slot
    assert s.holders["W"] == 10 and s.buys == 1 and not s.conflicts and not s.unsafe


def test_two_equal_trades_in_one_transaction_both_count():
    s = coin()
    s.on_trade(tr("S", 100, "W", 10, 31, ei=0), 10)
    s.on_trade(tr("S", 100, "W", 10, 31, ei=1), 10)
    assert s.holders["W"] == 20 and s.buys == 2


def test_a_conflict_is_flagged_and_every_derived_field_rebuilt():
    s = coin()
    s.on_trade(tr("S", 100, "W", 10, 31, ts=1, sol=0.5), 10)        # in the bundle window
    s.on_trade(tr("S", 101, "W", 12, 32, ts=1, sol=0.7), 10)        # the other version
    s.on_trade(tr("LATER", 102, "OTHER", 5, 33, ts=2), 10)
    assert s.holders["W"] == 12 and s.early_bought["W"] == 12 and s.volume_sol == pytest.approx(1.7)
    assert s.buys == 2 and s.curve_slot == 102 and s.unsafe == "fork conflict unresolved"


def test_the_chains_answer_keeps_or_reverts_and_is_idempotent():
    s = coin()
    s.on_trade(tr("S", 100, "W", 10, 31), 10)
    s.on_trade(tr("S", 101, "W", 12, 32), 10)
    s.on_trade(tr("LATER", 102, "OTHER", 5, 33), 10)
    assert s.resolve_conflict(("S", 0), 100, "finalized", "test") == "replaced"   # the first copy landed after all
    assert s.holders["W"] == 10 and s.curve_slot == 102 and not s.unsafe
    before = (dict(s.holders), s.volume_sol, s.curve.v_sol)
    assert s.resolve_conflict(("S", 0), 100) == "" and (dict(s.holders), s.volume_sol, s.curve.v_sol) == before
    s2 = coin()
    s2.on_trade(tr("S", 100, "W", 10, 31), 10)
    s2.on_trade(tr("S", 101, "W", 12, 32), 10)
    assert s2.resolve_conflict(("S", 0), 101) == "kept" and s2.holders["W"] == 12 and not s2.unsafe


def test_an_answer_matching_no_copy_stays_unsafe():
    s = coin()
    s.on_trade(tr("S", 100, "W", 10, 31), 10)
    s.on_trade(tr("S", 101, "W", 12, 32), 10)
    assert s.resolve_conflict(("S", 0), 0, "not found") == "unresolvable" and s.unsafe


def test_a_coin_restored_mid_life_cant_be_rebuilt_so_it_stays_unsafe():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.partial = True
    s.on_trade(tr("S", 100, "W", 10, 31), 10)
    s.on_trade(tr("S", 101, "W", 12, 32), 10)
    assert s.unrepairable and s.unsafe == "fork conflict: state can't be rebuilt"


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine():
    p = copy.deepcopy(P)
    p.sniper.execution["paper_delay_s"] = 0
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), persist=False, log_to_journal=False)


def test_automated_buys_wait_for_the_chain_and_manual_ones_dont():
    e = engine()
    s = e.tokens[M] = coin()
    s.price_known = True
    s.on_trade(tr("S", 100, "W", 10, 31), 10)
    s.on_trade(tr("S", 101, "W", 12, 32), 10)
    assert e._authorize(M, 0.01, "late") == "fork conflict unresolved"
    assert e._authorize(M, 0.01, "manual") == ""
    s.resolve_conflict(("S", 0), 101)
    assert e._authorize(M, 0.01, "late") == ""


def test_a_replay_with_the_recorded_answer_repairs_the_same_way():
    events = [Launch(M, 0, "DEV", symbol="F"), tr("S", 100, "W", 10, 31), tr("S", 101, "W", 12, 32),
              tr("LATER", 102, "OTHER", 5, 33), Reconcile(3, M, "S", 0, 100, "finalized", "test")]
    events = [loads(dumps(x)) for x in events]            # through a recording and back

    def play():
        e = engine()

        async def go():
            for x in events:
                await e.handle(x)
        asyncio.run(go())
        return e
    a, b = play(), play()
    assert a.tokens[M].holders["W"] == 10 == b.tokens[M].holders["W"] and not a.tokens[M].unsafe
    assert a.stats["fork_conflicts"] == 1 and a.stats["fork_replaced"] == 1 and not a.fork_pending


def test_the_engine_asks_the_chain_in_batches(monkeypatch):
    e = engine()
    e.tokens[M] = coin()
    seen = []

    def lookup(url, sigs):
        seen.append(("getSignatureStatuses", sigs))
        return [{"slot": 101, "confirmationStatus": "confirmed"}, None]
    monkeypatch.setattr(Engine, "_status_lookup", staticmethod(lookup))
    monkeypatch.setenv("SOLANA_WS_URL", "wss://feed.example/?api_key=SECRET")

    async def go():
        await e.handle(Launch(M, 0, "DEV"))
        await e.handle(tr("S", 100, "W", 10, 31))
        await e.handle(tr("S", 101, "W", 12, 32))
        e.fork_pending[("X", 0)] = {"mint": M, "since": e.now}       # not found yet: asked again later
        await e._reconcile_forks()
    asyncio.run(go())
    assert seen and seen[0][0] == "getSignatureStatuses" and set(seen[0][1]) == {"S", "X"}
    assert e.tokens[M].conflicts[("S", 0)]["status"] == "resolved" and ("X", 0) in e.fork_pending
    ev = NS(**e.tokens[M].conflicts[("S", 0)]["evidence"])
    assert ev.source == "getSignatureStatuses@feed.example" and "SECRET" not in json.dumps(vars(ev))   # host only
    assert e._fork_rpc()[0] == "https://feed.example/?api_key=SECRET"            # the feed's own provider, over HTTP
    assert e.intel_view()["forks"]["conflicts"] == 1 and e.intel_view()["forks"]["replaced"] == 0
