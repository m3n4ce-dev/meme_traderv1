import asyncio
import copy

import pytest

from meme_trader import config
from meme_trader.sniper.events import Funding, Launch, Trade
from meme_trader.sniper.feeds import SyntheticFeed
from meme_trader.sniper.sweep import beats, combos, split, sweep

P = config.load(config.EXAMPLE)


def test_split_keeps_each_token_on_one_side_and_funding_on_both():
    ev = [Launch("a", 1, "d"), Trade("a", 50, "w", "buy", 1, 1, 31, 1e9), Launch("b", 10, "d"),
          Trade("b", 11, "w", "buy", 1, 1, 31, 1e9), Funding("w", 0, "f")]
    tr, te = split(ev, 0.5)
    assert {getattr(e, "mint", None) for e in tr} == {"a", None}       # a's late trade stays with a
    assert {getattr(e, "mint", None) for e in te} == {"b", None}
    assert any(isinstance(e, Funding) for e in tr) and any(isinstance(e, Funding) for e in te)


def test_grid_and_noise_guard():
    assert len(combos(["exit.stop_loss_pct=20,30", "entry.min_score=50,60,70"])) == 6
    assert not beats(1.3400001, 1.34, "pnl")            # rounding is not an improvement
    assert beats(1.50, 1.34, "pnl") and not beats(1.36, 1.34, "pnl")


def test_sweep_runs_and_never_recommends_a_no_op():
    feed = SyntheticFeed(seed=3, speed=0, launches=250, start_ts=1_780_000_000)

    async def collect():
        return [e async for e in feed.events()]
    events = asyncio.run(collect())
    params = copy.deepcopy(P)
    res = asyncio.run(sweep(params, events, ["exit.max_hold_s=1800,1801"], min_trades=1, log=lambda *a: None))
    assert res["baseline"]["test"] is not None and len(res["all"]) == 2
    assert res["best"] is None                       # 1800 vs 1801 s max hold changes nothing


def test_compare_runs_variants_on_matched_samples_and_reports_consistency():
    from meme_trader.sniper.compare import compare, parse_seeds, parse_variant, report, summarize

    assert parse_seeds("1-3,7") == [1, 2, 3, 7]
    assert parse_variant("late: late.enabled=true exit.stop_loss_pct=25") == ("late", ["late.enabled=true", "exit.stop_loss_pct=25"])
    with pytest.raises(SystemExit):
        parse_variant("broken: nokey")
    samples = [(f"seed {s}", {"synthetic": 40, "seed": s}) for s in (1, 2)]
    res = compare(P, [parse_variant("same: exit.stop_loss_pct=30")], samples, jobs=1, log=lambda *_: None)
    rows = {r["variant"]: r for r in summarize(res)}
    assert set(rows) == {"base", "same"} and rows["base"]["samples"] == 2
    assert rows["same"]["mean_diff"] == pytest.approx(0.0) and rows["same"]["beat_base"] == 0   # identical settings
    assert "No variant beat base consistently" in report(res, synthetic=True)
