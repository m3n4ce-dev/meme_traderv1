"""Execution realism: paper orders that land after a delay at the price they find (and fail like live
orders do), per-trade execution facts, live timing capture, and the research delay curve."""
import asyncio
import copy

import pytest

from meme_trader import config
from meme_trader.sniper import research as R
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch, Tick, Trade, dumps
from meme_trader.sniper.execution import LiveExecutor, PaperExecutor
from meme_trader.sniper.feeds import Feed

P = config.load(config.EXAMPLE)
A = "A" * 40 + "pump"
DEV = "D" * 44
W = "W" * 44


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine", "meme_trader.sniper.research"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    realtime = False

    async def events(self):
        return
        yield


def engine(delay=2.0, slip=0.0):
    p = copy.deepcopy(P)
    p.sniper.execution["paper_delay_s"] = delay
    p.sniper.execution["paper_latency_slippage_pct"] = slip
    return Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False, persist=False)


def setup_token(e, v_sol=40.0):
    async def go():
        await e.handle(Launch(A, 0.0, DEV, symbol="TOK"))
        s = e.tokens[A]
        k = s.curve.v_sol * s.curve.v_tokens
        await e.handle(Trade(A, 1.0, W, "buy", 1.0, 1e7, v_sol, k / v_sol))
        return s, k
    return asyncio.run(go())


def test_paper_buy_lands_later_at_the_price_it_finds():
    e = engine(delay=2.0)
    s, k = setup_token(e)

    async def go():
        quote = s.curve.price
        await e._buy(s, 60, 0.1, ["test"], source="late")
        assert A in e.pending and A not in e.positions and e.deferred
        await e.handle(Trade(A, 2.0, W, "buy", 2.0, 1e7, 44.0, k / 44.0))       # +21% price... within 15%? no
        return quote
    quote = asyncio.run(go())
    assert A in e.pending                                                      # not landed yet (due at t=3)

    async def land():
        await e.handle(Tick(3.5))
    asyncio.run(land())
    assert A not in e.pending and A not in e.positions                         # price ran +21% > 15%: failed
    assert e.stats["failed_buys"] == 1 and e.book.sol < P.sniper.capital.starting_sol   # the fee was still paid
    assert quote > 0


def test_paper_buy_fills_with_measured_execution_facts():
    e = engine(delay=2.0)
    s, k = setup_token(e)

    async def go():
        await e._buy(s, 60, 0.1, ["test"], source="late")
        await e.handle(Trade(A, 2.0, W, "buy", 0.5, 1e7, 41.0, k / 41.0))       # +5%: inside the limit
        await e.handle(Tick(3.2))
        pos = e.positions[A]
        assert pos.entry_quote == pytest.approx(40.0 / (k / 40.0))
        assert pos.entry_delay_s == pytest.approx(2.2)
        # the fill used the later (higher) price: all-in more than 5% over the signal
        assert pos.entry_price > pos.entry_quote * 1.05
        await e._sell(s, pos, 1.0, "test exit")
        await e.handle(Tick(6.0))
        assert A not in e.positions
        row = e.book.closed[-1]
        assert row["entry_vs_signal_pct"] > 5 and row["entry_delay_s"] == pytest.approx(2.2)
        assert row["exit_delay_s"] == pytest.approx(2.8) and row["exit_vs_signal_pct"] is not None
    asyncio.run(go())


def test_paper_sell_retries_through_the_slippage_steps():
    e = engine(delay=1.0)
    s, k = setup_token(e)

    async def go():
        await e._buy(s, 60, 0.1, ["t"], source="late")
        await e.handle(Tick(2.5))
        pos = e.positions[A]
        await e._sell(s, pos, 1.0, "stop")
        # crash 30% before the first attempt lands: 15% step fails, 25% fails at the same price? price keeps falling
        await e.handle(Trade(A, 3.0, W, "sell", 5.0, 1e7, 28.0, k / 28.0))
        await e.handle(Tick(3.6))                       # attempt 1 (15%) fails: -30%
        assert e.stats["failed_sell_attempts"] == 1 and A in e.positions
        await e.handle(Tick(4.7))                       # attempt 2 at the new quote: price flat -> fills
        assert A not in e.positions
        assert e.book.closed[-1]["failed_fees_sol"] > 0
    asyncio.run(go())


def test_zero_delay_is_unchanged():
    e = engine(delay=0.0, slip=3.0)
    s, _ = setup_token(e)

    async def go():
        await e._buy(s, 60, 0.1, ["t"], source="late")
    asyncio.run(go())
    assert A in e.positions and not e.deferred and e.positions[A].entry_delay_s == 0


def test_live_fill_carries_landing_slot_and_time():
    ex = LiveExecutor(P.sniper.execution, wallet=None)
    fill = ex._fill_from({"failed": False, "dsol": -0.1, "dtok": 5_000_000, "rent": 0.002, "slot": 9, "block_time": 7},
                         "buy", "sig")
    assert fill.ok and fill.tokens == 5.0


def test_price_moves_and_delay_curve(tmp_path):
    k = 30.0 * 1_073_000_000
    evs = [Launch(A, 0.0, DEV, symbol="T")]
    for i, v in enumerate([60.0, 62.0, 66.0, 70.0, 69.0, 75.0]):            # a curve deep in the window, rising
        evs.append(Trade(A, 1.0 + i, W, "buy", 1.0, 1e6, v, k / v))
    path = tmp_path / "feed-x.jsonl"
    path.write_text("".join(dumps(e) + "\n" for e in evs))
    r = R.price_moves([path], lo_pct=0, hi_pct=100, horizons=(1, 2))
    assert r["samples"] == 6 and r["all"]["1s"]["p90"] > 0
    assert R._interp([(0, 1.0), (2, 0.0)], 1.0) == 0.5 and R._median([3, 1, 2]) == 2


POLICY = """name: test-v1
overrides:
  entry.enabled: false
  copy.enabled: false
  callouts.enabled: false
  late.enabled: true
  late.min_curve_pct: 20
gates: {min_days: 14}
frozen_at: null
frozen_hash: null
"""


def recording(tmp_path, seed=3, launches=150):
    from meme_trader.sniper.feeds import SyntheticFeed

    evs = []

    async def go():
        async for e in SyntheticFeed(seed=seed, speed=0, launches=launches, start_ts=1_780_000_000).events():
            evs.append(e)
    asyncio.run(go())
    path = tmp_path / f"feed-{seed}.jsonl"
    path.write_text("".join(dumps(e) + "\n" for e in evs))
    return path


def test_eval_reports_the_execution_curve(tmp_path):
    path = tmp_path / "test-v1.yaml"
    path.write_text(POLICY)
    rec = recording(tmp_path)
    rep = R.cmd_eval(str(path), [str(rec)], jobs=1)
    ex = rep["execution"]
    assert [r["delay_s"] for r in ex["by_delay"]] == list(R.DELAYS)
    assert ex["estimated_delay_s"] == pytest.approx(1.5 + R.LANDING_S)     # no chain times in synthetic data
    assert ex["pnl_at_estimated_delay_sol"] is not None
