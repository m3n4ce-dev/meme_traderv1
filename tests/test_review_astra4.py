"""A fourth external review (2026-10-07, revision 6f6101b): its contracts as this project's tests (its own file and
patch weren't run or applied here). The live order outbox above all: a signed order is on disk before it's sent."""
import asyncio
import base64
import copy
import json
import sqlite3
import subprocess
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper import revival as rv
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch
from meme_trader.sniper.execution import LiveExecutor
from meme_trader.sniper.feeds import Feed, FileFeed, compress_file
from meme_trader.sniper.outbox import Outbox
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
M = "U" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def live_engine(wallet):
    p = copy.deepcopy(P)
    p.sniper.execution["verify_tx"] = False
    return Engine(p, Quiet(), LiveExecutor(p.sniper.execution, wallet), mode="live", log_to_journal=False, persist=True)


def outbox_rows(tmp_path):
    return {r[0]: r[1] for r in sqlite3.connect(str(tmp_path / "orders.db")).execute("SELECT sig, status FROM orders")}


# 1. the signature is on disk before the send can reach the network
def test_a_signed_order_is_durable_before_it_is_sent(tmp_path, monkeypatch):
    import httpx
    seen = []

    def send(raw):
        seen.append(outbox_rows(tmp_path))
        raise RuntimeError("connection lost after submitting")      # a timeout: maybe out there, maybe not
    w = NS(pubkey="W", sign=lambda _: ("RAW", "SIG"), blockhash_of=lambda _: "BH", send=send)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: NS(status_code=200, content=b"unsigned"))
    e = live_engine(w)
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.price_known = True
    e.tokens[M] = s
    asyncio.run(e._buy(s, 80, 0.01, ["x"], source="manual"))
    assert seen and seen[0] == {"SIG": "signed"}                     # committed before send was called
    assert "SIG" in e.unresolved and M in e.book.reserved


def test_no_send_when_the_outbox_cant_be_written(tmp_path, monkeypatch):
    import httpx
    sent = []
    w = NS(pubkey="W", sign=lambda _: ("RAW", "SIG"), blockhash_of=lambda _: "BH", send=lambda raw: sent.append(raw))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: NS(status_code=200, content=b"unsigned"))
    e = live_engine(w)

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk full")
    e.outbox.prepare = broken
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.price_known = True
    e.tokens[M] = s
    asyncio.run(e._buy(s, 80, 0.01, ["x"], source="manual"))
    assert not sent and "SIG" not in e.unresolved and M not in e.positions and M not in e.book.reserved


# a crash after the send: the restart holds the order until the chain answers; a booked one isn't booked twice
def test_a_restart_holds_every_signed_order_the_book_never_booked(tmp_path):
    ob = Outbox(tmp_path / "orders.db")
    ob.prepare("LOST", "BH", M, "buy", {"side": "buy", "mint": M, "sol": 0.02, "score": 70, "notes": [], "source": "late"})
    ob.prepare("DONE", "BH", M, "sell", {"side": "sell", "mint": M, "tokens": 5.0, "reason": "stop"})
    (tmp_path / "sniper_state_live.json").write_text(json.dumps({
        "book": {"sol": 1.0, "start_sol": 1.0, "day": "", "day_pnl": 0.0, "halted": "", "closed": [], "reserved": {}},
        "positions": {}, "unresolved": {}, "booked_sigs": ["DONE"]}))
    e = live_engine(NS(pubkey="W"))
    e.state_path = tmp_path / "sniper_state_live.json"

    async def no_reconcile():
        return None
    e.reconcile = no_reconcile
    asyncio.run(e.restore_state())
    assert "LOST" in e.unresolved and e.unresolved["LOST"]["blockhash"] == "BH" and M in e.pending
    assert e.book.reserved[M] >= 0.02                                       # its cash is held again
    assert "DONE" not in e.unresolved and outbox_rows(tmp_path)["DONE"] == "booked"


def test_every_sell_retry_is_recorded_then_booked(tmp_path, monkeypatch):
    import httpx

    from meme_trader import wallet as wallet_mod
    from meme_trader.sniper.strategy import SniperPosition
    sigs = iter(["S1", "S2", "S3"])

    def send(raw):
        if raw == "RAW-S1":
            raise RuntimeError("RPC sendTransaction: preflight failed")         # rejected: not sent, retried
    w = NS(pubkey="W", blockhash_of=lambda _: "BH", send=send, token_balance=lambda _: 5,
           tx_deltas=lambda sig, mint: {"failed": False, "dsol": 0.04, "dtok": -10e6, "rent": 0, "slot": 1,
                                         "block_time": None})
    w.sign = lambda _: (lambda x: (f"RAW-{x}", x))(next(sigs))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: NS(status_code=200, content=b"unsigned"))
    monkeypatch.setattr(wallet_mod, "confirm", lambda sig, timeout_s=60: True)
    e = live_engine(w)
    st = e.tokens[M] = TokenState(M, Launch(M, 0, "DEV", symbol="UUU"), 0)
    st.on_launch(st.launch)
    pos = e.positions[M] = SniperPosition(M, "UUU", 0.0, st.curve.price, 10.0, 10.0, 0.05, 0.05, 70,
                                          peak_price=st.curve.price, exits=[], source="sniper")
    st.decided = "entered"
    asyncio.run(e._sell(st, pos, 10.0, "stop"))
    assert outbox_rows(tmp_path) == {"S1": "booked", "S2": "booked"}
    saved = json.loads(e.state_path.read_text())
    assert {"S1", "S2"} <= set(saved["booked_sigs"]) and M not in e.positions


def test_a_recovered_order_is_booked_once_when_the_chain_answers(tmp_path):
    from meme_trader.sniper.execution import SniperFill
    Outbox(tmp_path / "orders.db").prepare("LOST", "BH", M, "buy", {"side": "buy", "mint": M, "sol": 0.02, "score": 70,
                                                                    "notes": [], "source": "late", "leader": ""})

    async def nothing():
        return None

    async def landed(*a, **k):
        return SniperFill(True, sol=0.02, tokens=1000.0, signature="LOST")

    async def go():
        e = live_engine(NS(pubkey="W"))
        e.reconcile = nothing
        await e.restore_state()
        assert "LOST" in e.unresolved
        e.ex.resolve = landed
        await e._resolve_unresolved()
        assert M in e.positions and "LOST" not in e.unresolved and outbox_rows(tmp_path)["LOST"] == "booked"
        again = live_engine(NS(pubkey="W"))
        again.reconcile = nothing
        await again.restore_state()
        assert "LOST" not in again.unresolved and list(again.positions) == [M]
        assert again.book.sol == pytest.approx(e.book.sol)                     # booked once, not twice
    asyncio.run(go())


# 2. results: a broken tail keeps the rest; a failed write isn't taken as done
def follow():
    return {"id": "F", "pool": "P", "symbol": "T", "mint": "M", "rule": "R", "delay": 5, "control": False,
            "signal_t": 0, "decided_at": 0, "lag_s": 0, "ret5": .4, "surge": 4, "p0": 1, "fill_t": 10,
            "cost": .012, "cost_how": "flat", "exits": {}}


def test_a_broken_last_row_of_the_export_keeps_the_rest(tmp_path):
    good = {"id": "F", "exit": "hold 1 h", "pnl_pct": 1.0, "signal_t": 0, "rule": "R", "delay": 5}
    (tmp_path / "revival-v2.jsonl").write_text(json.dumps(good) + "\n" + '{"id":"unfinished')
    r = rv.Revival(tmp_path, now=101)
    assert r.done_keys == {("F", "hold 1 h")}
    assert (tmp_path / "revival-v2.quarantine.jsonl").read_text().startswith('{"id":"unfinished')


def test_a_failed_result_write_is_not_taken_as_done(tmp_path):
    r = rv.Revival(tmp_path, now=100)
    real = r.store.commit

    def full(*a, **k):
        raise sqlite3.OperationalError("disk full")
    r.store.commit = full
    with pytest.raises(sqlite3.OperationalError):
        r._close(follow(), "hold 1 h", {"pnl": 1, "why": "time"}, 3616)
    assert not r.done_keys and not r.done
    r.store.commit = real
    r._close(follow(), "hold 1 h", {"pnl": 1, "why": "time"}, 3616)
    assert r.done_keys == {("F", "hold 1 h")} and rv.Revival(tmp_path, now=200).done_keys == {("F", "hold 1 h")}


def test_a_re_read_trade_from_before_the_fill_cant_stop_a_follow(tmp_path):
    r = rv.Revival(tmp_path, now=0)
    f = follow()
    r._follow(f, 8, .3, 8)                                        # re-read after a crash: older than the fill (t=10)
    assert all("sell_at" not in x for x in f["exits"].values())


# 3. the small ones
def test_only_the_documented_legacy_curve_lengths_count():
    from meme_trader.sniper import mayhem as m
    for n in (50, 60, 80):
        assert m.parse(base64.b64encode(m.DISCRIMINATOR + bytes(n - 8)).decode()) is None
    assert m.parse("not base64!!") is None


def test_a_timer_already_due_wins_over_a_stop_on_the_same_trade(tmp_path):
    r = rv.Revival(tmp_path, now=0)
    f = follow()
    r._follow(f, 3616, .5, 3616)                                 # due 3610 + 5 s; this trade is also below the stop
    x = f["exits"]["stop 30%, hold 1 h"]
    assert x["why"] == "time" and "pnl" in x


def test_resaving_old_weights_keeps_their_old_version(tmp_path):
    from meme_trader.sniper.features import FEATURES
    from meme_trader.sniper.predictor import LogisticModel
    n = len(FEATURES)
    m = LogisticModel.from_dict({"features": FEATURES, "mean": [0] * n, "std": [1] * n, "w": [0] * n, "b": 0,
                                 "feature_version": 1})
    m.save(tmp_path / "model.json")
    assert LogisticModel.load(tmp_path / "model.json") is None


def test_a_replay_reads_the_archive_when_the_plain_file_was_compressed(tmp_path):
    from meme_trader.sniper.events import Tick, dumps
    p = tmp_path / "feed-day.jsonl"
    p.write_text(dumps(Tick(1)) + "\n")
    feed = FileFeed(p)
    compress_file(p)

    async def read():
        return [e async for e in feed.events()]
    assert len(asyncio.run(read())) == 1


# 4. research identity: untracked code counts; a final verdict needs committed code
def test_untracked_code_changes_the_identity_and_blocks_a_final(tmp_path, monkeypatch):
    from meme_trader.sniper import research as R
    root = tmp_path / "repo"
    (root / "meme_trader").mkdir(parents=True)
    (root / "meme_trader" / "main.py").write_text("x=1\n")

    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    monkeypatch.setattr(config, "ROOT", root)
    clean = R.manifest()["code_identity"]
    assert R.clean_tree()
    f = root / "meme_trader" / "new_strategy.py"
    f.write_text("threshold=1\n")
    first = R.manifest()["code_identity"]
    f.write_text("threshold=2\n")
    assert len({clean, first, R.manifest()["code_identity"]}) == 3 and not R.clean_tree()
