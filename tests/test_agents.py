import time

import pytest

from meme_trader import config
from meme_trader.agents import analyst, monitor, safety
from meme_trader.agents.executor import Executor
from meme_trader.agents.risk import Portfolio, RiskManager
from meme_trader.models import Candidate, Position, Signal

P = config.load(config.EXAMPLE)


@pytest.fixture(autouse=True)
def no_jupiter(monkeypatch, tmp_path):
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.setattr("meme_trader.journal.DATA", tmp_path)


def good_candidate(**kw) -> Candidate:
    d = dict(mint="MINT", symbol="MEME", pair_address="POOL", price_native=0.001, liquidity_usd=50_000,
             fdv_usd=200_000, volume_5m_usd=20_000, buys_5m=300, sells_5m=100, price_change_5m_pct=8,
             price_change_1h_pct=40, pair_created_at=time.time() - 3600)
    d.update(kw)
    return Candidate(**d)


def good_report(**kw) -> dict:
    d = {"rugged": False, "mintAuthority": None, "freezeAuthority": None, "score_normalised": 5, "risks": [],
         "topHolders": [{"owner": "POOL", "pct": 40}] + [{"owner": f"h{i}", "pct": 2} for i in range(12)],
         "totalHolders": 900, "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 100}}],
         "token": {"decimals": 6}}
    d.update(kw)
    return d


def test_safety_passes_clean_token_and_ignores_pool_holder():
    rep = safety.evaluate(good_candidate(), good_report(), P.safety)
    assert rep.passed, rep.reasons
    assert rep.raw["top10_pct"] == 20


@pytest.mark.parametrize("override,needle", [
    ({"mintAuthority": "X"}, "mint authority"),
    ({"freezeAuthority": "X"}, "freeze authority"),
    ({"risks": [{"name": "Single holder ownership", "level": "danger"}]}, "danger"),
    ({"topHolders": [{"owner": "whale", "pct": 50}]}, "top10"),
    ({"markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 0}}]}, "LP locked"),
    ({"rugged": True}, "rugged"),
])
def test_safety_rejects(override, needle):
    rep = safety.evaluate(good_candidate(), good_report(**override), P.safety)
    assert not rep.passed and any(needle in r for r in rep.reasons)


def test_safety_rejects_too_new():
    assert not safety.evaluate(good_candidate(pair_created_at=time.time() - 60), good_report(), P.safety).passed


def test_analyst_scores():
    assert analyst.evaluate(good_candidate(), P.analyst).passed
    weak = analyst.evaluate(good_candidate(buys_5m=50, sells_5m=100), P.analyst)
    assert not weak.passed and any("buy/sell" in n for n in weak.notes)
    assert not analyst.evaluate(good_candidate(price_change_1h_pct=900), P.analyst).passed


def pos(**kw) -> Position:
    d = dict(mint="MINT", symbol="MEME", entry_price_sol=1.0, initial_tokens=1000, tokens=1000, cost_sol=0.05,
             peak_price_sol=1.0)
    d.update(kw)
    return Position(**d)


def test_monitor_stop_loss_and_tp_and_trailing():
    assert monitor.evaluate(pos(), 0.79, P.exits).token_amount == 1000
    assert monitor.evaluate(pos(), 1.1, P.exits) is None
    tp = monitor.evaluate(pos(), 1.6, P.exits)
    assert tp.reason.startswith(monitor.TP_REASON) and tp.token_amount == 500
    p = pos(tokens=500, tp_levels_hit=1, peak_price_sol=2.0)
    assert monitor.evaluate(p, 1.4, P.exits).reason.startswith("trailing")
    assert monitor.evaluate(pos(opened_at=time.time() - 10**6), 1.0, P.exits).reason == "time stop"


def test_risk_limits_and_pnl():
    rm = RiskManager(P, Portfolio(sol=1.0, start_sol=1.0))
    ex = Executor(P)
    sig = Signal("MINT", 80, True)
    order, why = rm.approve(good_candidate(), sig, 6)
    assert order and not why
    rm.apply_fill(ex.execute(order))
    assert "MINT" in rm.pf.positions and rm.pf.sol == pytest.approx(0.95)
    assert rm.approve(good_candidate(), sig, 6)[1]  # already held

    # sell everything at half price -> realised loss, position closed, cooldown set
    p = rm.pf.positions["MINT"]
    sell = monitor.evaluate(p, p.entry_price_sol * 0.5, P.exits)
    rm.apply_fill(ex.execute(sell))
    assert "MINT" not in rm.pf.positions and "MINT" in rm.pf.cooldown_until
    assert rm.pf.day_realized_sol == pytest.approx(-0.025, rel=0.05)

    rm.pf.day_realized_sol = -P.capital.daily_loss_limit_sol
    assert "daily loss" in rm.can_open_any()


def test_kill_switch():
    rm = RiskManager(P, Portfolio(sol=0.6, start_sol=1.0))
    assert rm.check_kill_switch({}) and rm.can_open_any().startswith("halted")


def test_config_validation():
    with pytest.raises(AssertionError):
        config.validate({**P, "capital": {**P["capital"], "per_trade_sol": 5}})
