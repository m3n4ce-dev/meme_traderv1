"""A third external review (2026-10-06, revision 337ff7d): seven invariants, written as this project's tests (its own
file wasn't run here). Unknown Mayhem reads stay unknown; trade identity is the event, not its content; a restart
keeps the balance ledger; the revival evaluator loses no rows and times exits right; research records catch any edit."""
import asyncio
import base64
import copy
import json
import subprocess
import time
from collections import deque

import pytest

from meme_trader import config
from meme_trader.sniper import mayhem as mh
from meme_trader.sniper import revival as rv
from meme_trader.sniper.curve import Curve
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import PUMP_PROGRAM, SolanaTradeFeed
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState
from tests.test_review_round2 import trade_log

P = config.load(config.EXAMPLE)
M = "S" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    def __init__(self, realtime=False):
        self.realtime = realtime

    async def events(self):
        return
        yield


def engine(realtime=False, persist=False):
    p = copy.deepcopy(P)
    p.sniper.execution["paper_delay_s"] = 0
    return Engine(p, Quiet(realtime), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=persist)


# 1. an unreadable Mayhem result is unknown, never "checked safe"
def test_an_unreadable_mayhem_result_stays_unknown(monkeypatch):
    monkeypatch.setattr(mh, "lookup", lambda mint: None)
    e = engine(realtime=True, persist=True)
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.price_known = True
    e.tokens[M] = s

    async def go():
        e._check_mayhem(s)
        e._mayhem_tick()
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
    asyncio.run(go())
    assert not s.mayhem_checked and e.mayhem_reads["unknown"] == 1 and M in e._mayhem_q   # retried later
    assert e._authorize(M, 0.01, "late") == "checking Mayhem mode"


def test_only_a_validated_curve_account_answers():
    def acct(flag=0, n=151, disc=mh.DISCRIMINATOR):
        raw = bytearray(disc + bytes(n - 8))
        if n > mh.MAYHEM_BYTE:
            raw[mh.MAYHEM_BYTE] = flag
        return base64.b64encode(bytes(raw)).decode()
    assert mh.parse(acct(1)) is True and mh.parse(acct(0)) is False
    assert mh.parse(acct(1), owner="SomeOtherProgram") is None                  # not pump.fun's
    assert mh.parse(acct(1, disc=bytes(8))) is None                             # not a BondingCurve
    assert mh.parse(acct(7)) is None                                            # not a boolean
    assert mh.parse(acct(n=81)) is False and mh.parse(acct(n=49)) is False     # before Mayhem existed
    assert mh.parse(acct(n=20)) is None


# 2. two identical events in one transaction are two trades
def test_distinct_events_in_one_transaction_both_count():
    s = TokenState(M, Launch(M, 0, "DEV"), 0)
    s.curve, s.price_known = Curve(30, 100e6), True
    for i, (side, vt) in enumerate([("buy", 90e6), ("sell", 100e6), ("buy", 90e6)]):
        s.on_trade(Trade(M, 10 + i, "W", side, 3.333333, 10e6, 3e9 / vt, vt, slot=200, signature="ONE_TX", event_index=i), 3)
    assert s.buys == 2 and s.holders.get("W") == 10e6
    s.on_trade(Trade(M, 13, "W", "buy", 3.333333, 10e6, 3e9 / 90e6, 90e6, slot=200, signature="ONE_TX", event_index=2), 3)
    assert s.buys == 2                                                          # the same event again: once


def test_the_parser_numbers_each_trade_event_in_its_transaction():
    ev = trade_log(bytes(range(32)), 250_000_000, 7_000_000_000_000, True, bytes(range(32, 64)))
    out = SolanaTradeFeed.parse_logs({"err": None, "signature": "TX", "logs": [
        f"Program {PUMP_PROGRAM} invoke [1]", ev, ev, f"Program {PUMP_PROGRAM} success"]}, 1.0)
    assert [t.event_index for t in out] == [0, 1]


# 3. a restart keeps the signed ledger in step with what it restores
def test_a_restored_coin_subtracts_a_sale_from_what_was_held():
    e = engine(persist=True)
    s = TokenState(M, Launch(M, 0, "DEV", dev_buy_tokens=100), 0)
    s.on_launch(s.launch)
    s.on_trade(Trade(M, 1, "DEV", "sell", 0.01, 5, 30, 1_073_000_005, slot=1), 3)
    e.tokens[M] = s
    e.unresolved["SIG"] = {"mint": M, "side": "buy", "sent_at": time.time()}
    e.save_state()
    e.tokens = {}
    asyncio.run(e.restore_state())
    r = e.tokens[M]
    assert r.holders["DEV"] == 95 and r.partial                                # 100 bought, 5 already sold
    r.on_trade(Trade(M, 2, "DEV", "sell", 0.01, 10, 30, 1_073_000_015, slot=2), 3)
    assert r.holders["DEV"] == 85


# 4. a half-written row survives a checkpoint and a restart
def test_a_partial_row_is_read_whole_after_a_restart(tmp_path):
    day = 1_791_300_000.0
    p = tmp_path / "wallets" / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(day))}.jsonl"
    p.parent.mkdir()
    p.write_text("")
    r = rv.Revival(tmp_path, now=day)
    r.tick(now=day)
    p.write_text('{"t":1791300010,"pool":"P","px":1,"sol"')
    r.tick(now=day + 10)
    assert r.rest
    r.save(force=True)
    again = rv.Revival(tmp_path, now=day + 11)
    with p.open("a") as f:
        f.write(':0.1}\n')
    again.tick(now=day + 12)
    assert "P" in again.pools


# 5. a timed exit fills on the first trade at or after its time
def test_a_timed_exit_fills_on_the_first_eligible_trade(tmp_path):
    r = rv.Revival(tmp_path, now=0)
    f = {"id": "P|r|0|5|s", "pool": "P", "symbol": "T", "mint": "M", "rule": "+40% in 5 min, volume x4", "delay": 5,
         "control": False, "signal_t": 0, "decided_at": 0, "lag_s": 0, "ret5": 0.4, "surge": 4, "p0": 1, "fill_t": 10,
         "cost": 0.012, "cost_how": "flat", "exits": {}}
    r._follow(f, 3616, 2, 3616)                                                 # fill 10 + 1 h + 5 s = 3615
    assert f["exits"]["hold 1 h"]["pnl"] == pytest.approx((2 - 1 - 0.012) * 100)
    assert len([x for x in r.done if x["exit"] == "hold 1 h"]) == 1
    r._close(f, "hold 1 h", f["exits"]["hold 1 h"], 3700)                      # a replay after a crash: once only
    assert len([x for x in r.done if x["exit"] == "hold 1 h"]) == 1


# 6. a recording's fingerprint is its whole content
def test_an_edit_in_the_middle_of_a_recording_changes_its_fingerprint(tmp_path, monkeypatch):
    from meme_trader.sniper import research as R
    monkeypatch.setattr(config, "ROOT", tmp_path)
    p = tmp_path / "feed.jsonl"
    p.write_bytes(b"A" * (3 << 20))
    before = R.manifest([p])["inputs"]
    with p.open("r+b") as f:
        f.seek(1_500_000)
        f.write(b"B")
    assert R.manifest([p])["inputs"] != before


# 7. two different uncommitted patches are two different codes
def test_a_different_dirty_patch_is_changed_code(tmp_path, monkeypatch):
    from meme_trader.sniper import research as R
    root = tmp_path / "repo"
    (root / "meme_trader").mkdir(parents=True)
    f = root / "meme_trader" / "strategy.py"
    f.write_text("x=1\n")

    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    monkeypatch.setattr(config, "ROOT", root)
    f.write_text("x=2\n")
    m1 = R.manifest()
    pol = {"_path": str(tmp_path / "p.yaml")}
    R.lock_path(pol).write_text(json.dumps({"code_revision": m1["code_revision"], "manifest": m1}))
    assert R.code_note(pol)["code_changed_since_freeze"] is False
    f.write_text("x=3\n")
    assert R.manifest()["patch"] != m1["patch"] and R.code_note(pol)["code_changed_since_freeze"] is True
