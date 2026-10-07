"""A fifth external review (2026-10-07, revision 661e033): its acceptance contracts as this project's tests (its own
file wasn't run here). A coin stays locked while any of its orders is unsettled; a booking receipt is pinned until the
outbox confirms it; recovery stores belong to one wallet."""
import asyncio
import copy
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch
from meme_trader.sniper.execution import LiveExecutor, SniperFill
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.outbox import Outbox, StoreOwnerError
from meme_trader.sniper.strategy import SniperPosition
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
M = "V" * 40 + "pump"
OWNER = {"network": "solana-mainnet", "wallet": "W", "schema": 2}


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def live_engine(wallet=None):
    p = copy.deepcopy(P)
    p.sniper.execution["verify_tx"] = False
    return Engine(p, Quiet(), LiveExecutor(p.sniper.execution, wallet or NS(pubkey="W")), mode="live",
                  log_to_journal=False, persist=True)


def coin(e, creator="DEV", dev_tokens=0.0):
    s = e.tokens[M] = TokenState(M, Launch(M, 0, creator, symbol="VVV", dev_buy_tokens=dev_tokens), 0)
    s.on_launch(s.launch)
    return s


def hold(e, s, tokens=100.0):
    pos = e.positions[M] = SniperPosition(M, "VVV", 0.0, s.curve.price, tokens, tokens, 0.05, 0.05, 70,
                                          peak_price=s.curve.price, exits=[], source="sniper")
    s.decided = "entered"
    return pos


def rows(tmp_path):
    return dict(sqlite3.connect(str(tmp_path / "orders.db")).execute("SELECT sig, status FROM orders").fetchall())


async def nothing():
    return None


def restarted(wallet=None):
    e = live_engine(wallet)
    e.reconcile = nothing
    asyncio.run(e.restore_state())
    return e


def failing(*a, **k):
    raise sqlite3.OperationalError("disk I/O error")


# --------------------------------------------------------------------------- A1: the lock is per coin, not per order
@pytest.mark.parametrize("failed_first", [True, False])
def test_a_failed_retry_cant_unlock_a_coin_while_another_is_unknown(failed_first):
    sent = []
    e = live_engine(NS(pubkey="W", sign=lambda _: sent.append(1) or ("RAW", "NEW"), blockhash_of=lambda _: "BH"))
    s = coin(e)
    pos = hold(e, s)
    order = ["FAILED", "UNKNOWN"] if failed_first else ["UNKNOWN", "FAILED"]
    for sig in order:
        e.outbox.prepare(sig, "BH", M, "sell", {"tokens": 100, "reason": "stop"})
    e._reconcile_outbox()
    e.pending.add(M)

    async def resolve(sig, *a, **k):
        if sig == "FAILED":
            return SniperFill(False, signature=sig, error="failed on-chain", fees_lost=0.001)
        return SniperFill(False, unknown=True, signature=sig, blockhash="BH")
    sol = e.book.sol
    asyncio.run(e._resolve_each(resolve))
    assert "UNKNOWN" in e.unresolved and M in e.positions and M in e.pending
    assert e.book.sol == pytest.approx(sol - 0.001)                     # the failed retry's own fee, once
    asyncio.run(e._sell(s, pos, 1.0, "stop"))
    assert not sent                                                      # no fresh sell while one could still land

    async def expired(sig, *a, **k):                                     # proven: it can never land now
        return SniperFill(False, unknown=True, expired=True, signature=sig, blockhash="BH")
    asyncio.run(e._resolve_each(expired))
    assert not e.unresolved and M not in e.pending                       # nothing left: unlocked
    assert e.book.sol == pytest.approx(sol - 0.001)
    assert rows_status(e) == {"FAILED": "booked", "UNKNOWN": "booked"}


def rows_status(e):
    return dict(e.outbox.db.execute("SELECT sig, status FROM orders").fetchall())


def test_settling_one_order_by_hand_keeps_the_other_locked():
    e = live_engine()
    s = coin(e)
    hold(e, s)
    for sig in ("A", "B"):
        e.outbox.prepare(sig, "BH", M, "sell", {"tokens": 100, "reason": "stop"})
    e._reconcile_outbox()
    e.pending.add(M)
    assert e.reconcile_unresolved("A", "not landed") == ""
    assert M in e.pending and "B" in e.unresolved
    assert e.reconcile_unresolved("B", "not landed") == "" and M not in e.pending


def test_a_paper_order_in_flight_keeps_its_coin_locked():
    e = live_engine()
    e.pending.add(M)
    e.deferred.append((0.0, 1, {"side": "sell", "mint": M}))
    e._release(M)
    assert M in e.pending
    e.deferred.clear()
    e._release(M)
    assert M not in e.pending


def test_each_unsettled_buy_keeps_its_own_cash():
    e = live_engine()
    coin(e)
    e.outbox.prepare("B1", "BH", M, "buy", {"sol": 0.02, "source": "late", "score": 70, "notes": []})
    e.outbox.prepare("B2", "BH", M, "buy", {"sol": 0.03, "source": "late", "score": 70, "notes": []})
    e._reconcile_outbox()
    over = e._order_overhead("late")
    assert e.book.reserved[M] == pytest.approx(0.05 + 2 * over)

    async def b1_never(sig, *a, **k):
        if sig == "B1":
            return SniperFill(False, unknown=True, expired=True, signature=sig)
        return SniperFill(False, unknown=True, signature=sig)
    asyncio.run(e._resolve_each(b1_never))
    assert e.book.reserved[M] == pytest.approx(0.03 + over) and M in e.pending   # B2's cash, not freed with B1's


# --------------------------------------------------------------------------- A2: receipts pinned until confirmed
def landed_buy(sig, sol=0.02, tokens=1000.0):
    async def resolve(s, *a, **k):
        return SniperFill(True, sol=sol, tokens=tokens, signature=sig) if s == sig else \
            SniperFill(False, unknown=True, signature=s)
    return resolve


def test_a_booking_whose_mark_failed_is_never_booked_again():
    e = live_engine()
    coin(e)
    e.outbox.prepare("OLD", "BH", M, "buy", {"sol": 0.02, "source": "late", "score": 70, "notes": []})
    e._reconcile_outbox()
    real, e.outbox.mark = e.outbox.mark, failing
    asyncio.run(e._resolve_each(landed_buy("OLD")))                     # booked; confirming it failed
    assert "OLD" in e.booked_sigs.unacked()
    later = [f"N{i}" for i in range(6000)]                              # more than the receipts kept
    e.booked_sigs.extend(later)
    e.booked_sigs.ack(later)
    assert "OLD" in e.booked_sigs                                        # pinned: never pruned while unconfirmed
    e.save_state()
    sol, tokens = e.book.sol, e.positions[M].tokens
    for _ in range(3):                                                   # repeated restarts, the same fill re-read
        again = restarted()
        asyncio.run(again._resolve_each(landed_buy("OLD")))
        assert again.book.sol == pytest.approx(sol) and again.positions[M].tokens == pytest.approx(tokens)
        assert "OLD" not in again.unresolved and rows_status(again)["OLD"] == "booked"
    e.outbox.mark = real


def test_an_unconfirmed_receipt_is_retried_until_it_works():
    e = live_engine()
    coin(e)
    e.outbox.prepare("R", "BH", M, "buy", {"sol": 0.02, "source": "late", "score": 70, "notes": []})
    e._reconcile_outbox()
    real, e.outbox.mark = e.outbox.mark, failing
    asyncio.run(e._resolve_each(landed_buy("R")))
    e._retry_acks()
    assert e.booked_sigs.unacked() == ["R"]
    e.outbox.mark = real
    e._retry_acks()
    assert e.booked_sigs.unacked() == [] and rows_status(e)["R"] == "booked"


def test_a_sell_with_a_failed_retry_is_booked_once_across_restarts(monkeypatch):
    import httpx

    from meme_trader import wallet as wallet_mod
    sigs = iter(["S1", "S2"])
    deltas = {"S1": {"failed": True, "dsol": -0.001, "dtok": 0, "rent": 0, "slot": 1, "block_time": None},
              "S2": {"failed": False, "dsol": 0.04, "dtok": -100e6, "rent": 0, "slot": 2, "block_time": None}}
    w = NS(pubkey="W", blockhash_of=lambda _: "BH", send=lambda raw: None, token_balance=lambda _: 5,
           tx_deltas=lambda sig, mint: deltas[sig])
    w.sign = lambda _: (lambda x: (f"RAW-{x}", x))(next(sigs))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: NS(status_code=200, content=b"unsigned"))
    monkeypatch.setattr(wallet_mod, "confirm", lambda sig, timeout_s=60: True)
    e = live_engine(w)
    s = coin(e)
    pos = hold(e, s)
    e.outbox.mark = failing
    asyncio.run(e._sell(s, pos, 1.0, "stop"))                            # S1 failed on-chain (fee), S2 sold it all
    assert set(e.booked_sigs.unacked()) == {"S1", "S2"} and M not in e.positions
    sol, closed = e.book.sol, len(e.book.closed)
    for _ in range(2):
        again = restarted(w)

        async def same(sig, *a, **k):
            return SniperFill(True, sol=0.04, tokens=100.0, signature=sig)
        asyncio.run(again._resolve_each(same))
        assert not again.unresolved and again.book.sol == pytest.approx(sol) and len(again.book.closed) == closed


# --------------------------------------------------------------------------- A3: stores belong to one wallet
def test_another_wallet_refuses_this_wallets_order_store():
    first = live_engine(NS(pubkey="WALLET_A"))
    first.outbox.prepare("A_SIG", "BH", M, "buy", {"sol": 0.02, "source": "late", "score": 70, "notes": []})
    first.save_state()
    with pytest.raises(StoreOwnerError, match="belongs to wallet"):
        live_engine(NS(pubkey="WALLET_B"))


def test_another_wallet_refuses_this_wallets_book(tmp_path):
    first = live_engine(NS(pubkey="WALLET_A"))
    first.save_state()
    (tmp_path / "orders.db").unlink()                                    # even with no order store to object
    for x in ("-wal", "-shm"):
        (tmp_path / f"orders.db{x}").unlink(missing_ok=True)
    e = live_engine(NS(pubkey="WALLET_B"))
    e.reconcile = nothing
    with pytest.raises(StoreOwnerError, match="refusing to adopt"):
        asyncio.run(e.restore_state())


def test_unbound_stores_need_the_owners_claim(tmp_path):
    Outbox(tmp_path / "orders.db").prepare("OLD", "BH", M, "buy", {"sol": 0.02})     # from before stores were bound
    (tmp_path / "sniper_state_live.json").write_text(json.dumps({
        "book": {"sol": 1.0, "start_sol": 1.0, "day": "", "day_pnl": 0.0, "halted": "", "closed": [], "reserved": {}},
        "positions": {}, "unresolved": {}}))
    with pytest.raises(StoreOwnerError, match="no owner recorded"):
        live_engine()
    from meme_trader.sniper.outbox import _main
    assert _main(["claim", "W", "--data", str(tmp_path)]) == 0
    e = restarted()
    assert "OLD" in e.unresolved
    assert _main(["claim", "SOMEONE_ELSE", "--data", str(tmp_path)]) == 1          # never rebinds a bound store


# --------------------------------------------------------------------------- recovery keeps the coin's protections
def test_a_recovered_buy_keeps_its_creator_and_its_age(tmp_path):
    """The whole lifecycle in a real process: it dies while sending; the next start holds the order; the chain
    answers; the position knows its creator (dev-sell exits) and counts its age from the send, not from now."""
    child = tmp_path / "child.py"
    child.write_text(f'''
import asyncio, copy, os
from pathlib import Path
from types import SimpleNamespace as NS
import httpx
import meme_trader.journal as j, meme_trader.sniper.engine as em
j.DATA = em.DATA = Path({str(tmp_path)!r})
from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch
from meme_trader.sniper.execution import LiveExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState
class Quiet(Feed):
    realtime = False
    async def events(self):
        return
        yield
p = copy.deepcopy(config.load(config.EXAMPLE)); p.sniper.execution["verify_tx"] = False
httpx.post = lambda *a, **k: NS(status_code=200, content=b"unsigned")
w = NS(pubkey="W", sign=lambda _: ("RAW", "SIGX"), blockhash_of=lambda _: "BH", send=lambda raw: os._exit(23))
e = Engine(p, Quiet(), LiveExecutor(p.sniper.execution, w), mode="live", persist=True, log_to_journal=False)
s = e.tokens[{M!r}] = TokenState({M!r}, Launch({M!r}, 0, "DEV", symbol="VVV", dev_buy_tokens=5e7), 0)
s.on_launch(s.launch)
asyncio.run(e._buy(s, 80, 0.02, [], source="manual"))
''')
    ret = subprocess.run([sys.executable, str(child)], capture_output=True, text=True, cwd=config.ROOT,
                         env={**os.environ, "PYTHONPATH": str(config.ROOT)})
    assert ret.returncode == 23, ret.stderr[-2000:]
    e = restarted()
    assert "SIGX" in e.unresolved and M in e.pending and e.book.reserved[M] >= 0.02
    sent_at = e.unresolved["SIGX"]["sent_at"]
    asyncio.run(e._resolve_each(landed_buy("SIGX")))
    pos, s = e.positions[M], e.tokens[M]
    assert s.launch is not None and s.launch.creator == "DEV" and s.net["DEV"] == pytest.approx(5e7)
    assert pos.opened_at <= sent_at + 1e-6 and M not in e.pending and M not in e.book.reserved
