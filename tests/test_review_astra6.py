"""A sixth external review (2026-10-07, revision 75fb797): its contracts as this project's tests (the reviewer's own
file is also run as an acceptance check). Exports are one row per key with honest counts; a closed trade's P&L is net
of failed-transaction fees; replays never invent an exit; a fork version proven canonical repairs the whole coin."""
import ast
import asyncio
import bisect
import copy
import json
from pathlib import Path

import pytest

from meme_trader import config
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.execution import PaperExecutor, SniperFill
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.pnl import upgrade
from meme_trader.sniper.review_export import Export, Pseudo
from meme_trader.sniper.revival import Store
from meme_trader.sniper.tracker import TokenState

ROOT = Path(__file__).resolve().parents[1]
P = config.load(config.EXAMPLE)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


# --------------------------------------------------------------------------- the forward-test export
def test_an_exit_with_a_durable_result_isnt_also_exported_open(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    Store(data / "revival-v2.db").commit(
        [{"id": "F", "exit": "hold 1 h", "pnl_pct": 1}],
        {"open": {"P": [{"id": "F", "pool": "P", "exits": {"hold 1 h": {"pnl": 1}, "hold 2 h": {},
                                                           "stop": {"pnl": 2}}}]}, "saved": 100})
    ex = Export(data, out, Pseudo(b"k"), now=100)
    ex.revival()
    rows = [json.loads(x) for x in (out / "revival_outcomes.jsonl").read_text().splitlines()]
    assert sorted((r["exit"], r["status"]) for r in rows) == [("hold 1 h", "closed"), ("hold 2 h", "open"),
                                                              ("stop", "inconsistent")]   # terminal, but not in the store
    c = ex.counts["revival_outcomes.jsonl"]
    assert c["rows"] == len(rows) == 3 and c["by_status"] == {"closed": 1, "open": 1, "inconsistent": 1}


# --------------------------------------------------------------------------- net P&L
def engine(tmp_path, monkeypatch):
    import meme_trader.sniper.engine as em
    monkeypatch.setattr(em, "DATA", tmp_path)
    p = copy.deepcopy(P)
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", persist=False, log_to_journal=False)


def coin(e, m="M"):
    s = e.tokens[m] = TokenState(m, Launch(m, 0, "DEV", symbol="T"), 0)
    s.price_known = True
    return s


def test_a_failed_sell_fee_makes_a_breakeven_trade_zero_not_a_win(tmp_path, monkeypatch):
    e = engine(tmp_path, monkeypatch)
    s, cash = coin(e), e.book.sol

    async def go():                                       # (a close schedules the coin's unwatch)
        e._apply_buy(s, SniperFill(True, sol=0.05, tokens=100), 70, [], "late", "")
        pos = e.positions["M"]
        e._apply_sell(s, pos, SniperFill(False, fees_lost=0.01, error="landed, failed"), "retry")
        e._apply_sell(s, pos, SniperFill(True, sol=0.06, tokens=100), "exit")
    asyncio.run(go())
    row = e.book.closed[-1]
    assert e.book.sol - cash == pytest.approx(0) == e.book.day_pnl == row["pnl"]
    assert row["gross_pnl"] == pytest.approx(0.01) and row["pnl_schema"] == 2 and e.stats["wins"] == 0


def test_partial_sells_add_ons_and_failed_buys_reconcile_with_cash(tmp_path, monkeypatch):
    e = engine(tmp_path, monkeypatch)
    s, cash = coin(e), e.book.sol

    async def go():
        e._apply_buy(s, SniperFill(False, fees_lost=0.002, error="failed buy"), 70, [], "late", "")   # no position
        e._apply_buy(s, SniperFill(True, sol=0.05, tokens=2e6, fees_lost=0.001), 70, [], "late", "")
        e._apply_buy(s, SniperFill(True, sol=0.05, tokens=2e6), 70, [], "late", "", {"add": True})
        pos = e.positions["M"]                            # (real token amounts: a tiny remainder is written off as dust)
        e._apply_sell(s, pos, SniperFill(True, sol=0.03, tokens=1e6), "part")
        assert "M" in e.positions
        e._apply_sell(s, pos, SniperFill(False, fees_lost=0.003), "retry")
        e._apply_sell(s, pos, SniperFill(True, sol=0.09, tokens=3e6), "rest")
    asyncio.run(go())
    row = e.book.closed[-1]
    assert row["pnl"] == pytest.approx(0.12 - 0.10 - 0.004)                       # its own failed fees, once
    assert e.book.sol - cash == pytest.approx(row["pnl"] - 0.002)                  # minus the failed buy's: no position


def test_rows_from_before_net_pnl_are_upgraded_from_their_own_fees():
    old = {"pnl": 0.01, "pnl_pct": 20.0, "cost": 0.05, "failed_fees_sol": 0.01}
    new = upgrade(dict(old))
    assert new["pnl"] == pytest.approx(0) and new["gross_pnl"] == 0.01 and new["net_derived"] and new["pnl_schema"] == 2
    assert upgrade(dict(new)) == new                                               # idempotent
    assert "net_derived" not in upgrade({"pnl": 0.02, "cost": 0.1})               # no fees: unchanged, still schema 2


# --------------------------------------------------------------------------- the historical replay
def replay_run():
    path = ROOT / "research/exploratory/edge2-swap_robust.py"
    nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in ("fill", "run")]
    ns = {"bisect": bisect, "DELAY": 60, "COST": 0.012}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
    return ns["run"]


@pytest.mark.parametrize("tp", [None, 0.5])
def test_the_published_replay_never_books_an_unobserved_exit(tp):
    p = [(60, 1, True, 1), (120, 10 if tp is None else 2, True, 1)]
    assert replay_run()(p, [60, 120], 0, tp, None, 3600, 0, None) is None


# --------------------------------------------------------------------------- forks
def test_a_later_version_repairs_the_holder_ledger_provisionally():
    s = TokenState("M", Launch("M", 0, "DEV"), 0)

    def trade(sig, slot, wallet, qty, vs):
        return Trade("M", slot / 100, wallet, "buy", 1, qty, vs, 1000 - qty, signature=sig, slot=slot, event_index=0)
    s.on_trade(trade("S", 100, "W", 10, 31), 10)
    s.on_trade(trade("S", 101, "W", 12, 32), 10)
    s.on_trade(trade("LATER", 102, "OTHER", 5, 33), 10)
    assert s.curve_slot == 102 and s.holders["W"] == 12 and s.unsafe            # provisional until the chain answers
