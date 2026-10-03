"""Probability model, feature extraction, graduation plays and the gate checklist."""
import asyncio
import json
import math

import pytest

from meme_trader import config
from meme_trader.sniper.curve import Curve
from meme_trader.sniper.events import Trade
from meme_trader.sniper.features import FEATURES, extract, social_strength
from meme_trader.sniper.feeds import SyntheticFeed
from meme_trader.sniper.predictor import (LogisticModel, auc, build_dataset, evaluate, expected_value_pct,
                                          fit_temperature, kelly)
from meme_trader.sniper.strategy import (SniperPosition, evaluate_entry, evaluate_late_entry, evaluate_late_exit,
                                         gate_checklist)
from tests.test_sniper import CTX, MINT, make_state

P = config.load(config.EXAMPLE)
E, L, X = P.sniper.entry, P.sniper.late, P.sniper.exit


def test_features_are_complete_and_finite():
    s, now = make_state(organic=30)
    f = extract(s, now, {"creator_launches": 2, "symbol_dupes": 1})
    assert list(f) == FEATURES
    assert all(isinstance(v, float) and math.isfinite(v) for v in f.values())
    assert f["creator_launches"] == 2 and f["has_twitter"] == 1.0 and f["buyers_60s"] > 10


def test_social_strength_weights_telegram_most():
    s, _ = make_state()
    s.launch.twitter, s.launch.telegram, s.launch.website = "", "t.me/x", ""
    tg = social_strength(s)
    s.launch.twitter, s.launch.telegram = "x.com/y", ""
    assert tg == 0.5 and social_strength(s) == 0.25


def test_auc_and_kelly_basics():
    assert auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == 0.5
    assert kelly(0.5, 100, 30, 4) > 0 > kelly(0.1, 100, 30, 4)
    assert expected_value_pct(0.5, 100, 30, 4) == pytest.approx(0.5 * 100 - 0.5 * 30 - 4)


def _separable(n=400):
    import random
    rng = random.Random(3)
    X, y = [], []
    for _ in range(n):
        row = [rng.gauss(0, 1) for _ in FEATURES]
        X.append(row)
        y.append(1 if row[0] + 0.3 * rng.gauss(0, 1) > 1.0 else 0)
    return X, y


def test_logistic_model_learns_and_round_trips(tmp_path):
    X, y = _separable()
    m = LogisticModel.fit(X, y)
    assert evaluate(y, m.predict_rows(X))["auc"] > 0.9
    top = max(range(len(X)), key=lambda i: X[i][0])
    assert m.predict(dict(zip(FEATURES, X[top]))) > 0.8
    m.temp = 1.7
    path = tmp_path / "model.json"
    m.save(path)
    m2 = LogisticModel.load(path)
    assert m2.temp == 1.7 and m2.predict_rows(X[:5]) == pytest.approx(m.predict_rows(X[:5]))
    d = json.loads(path.read_text())
    d["features"] = d["features"][:-1] + ["something_new"]          # model from an older feature set
    path.write_text(json.dumps(d))
    assert LogisticModel.load(path) is None
    assert m.drivers(dict(zip(FEATURES, X[top])))[0][0] == FEATURES[0]


def test_temperature_scaling_tames_an_overconfident_model():
    X, y = _separable()
    m = LogisticModel.fit(X, y)
    logits = [z * 4 for z in m.logits_rows(X)]                     # same ranking, 4x too sure
    t = fit_temperature(logits, y)
    assert t > 2
    assert fit_temperature(logits[:10], y[:10]) == 1.0              # too little data: leave it alone


def test_build_dataset_labels_every_snapshot():
    async def collect():
        return [e async for e in SyntheticFeed(seed=4, speed=0, launches=60, start_ts=1_780_000_000).events()]
    X, y, meta = build_dataset(asyncio.run(collect()), P)
    assert X and len(X) == len(y) == len(meta)
    assert set(y) <= {0, 1} and 0 < sum(y) < len(y)
    assert all(len(r) == len(FEATURES) for r in X)


@pytest.mark.parametrize("kw,ctx", [
    ({"dev_sells": True}, {}), ({"dev_pct": 15}, {}), ({"bundle_buys": 8}, {}),
    ({}, {"creator_launches": 5}), ({}, {"symbol_dupes": 9}),
])
def test_checklist_agrees_with_the_entry_gate_on_hard_rejects(kw, ctx):
    s, now = make_state(**kw)
    c = {**CTX, **ctx}
    assert evaluate_entry(s, now, E, c).action == "reject"
    assert any(r["kind"] == "hard" and not r["ok"] for r in gate_checklist(s, now, E, c))


def test_checklist_all_green_when_the_gate_enters():
    s, now = make_state(organic=40)
    assert evaluate_entry(s, now, E, CTX).action == "enter"
    rows = gate_checklist(s, now, E, CTX)
    assert all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]


def _late_state():
    """A token ~60% up its curve with a fresh burst of broad buying."""
    s, now = make_state(organic=30)
    c = Curve(s.curve.v_sol, s.curve.v_tokens)
    for i in range(20):
        tok = c.quote_buy(0.8, 0)
        c.apply(0.8, -tok)
        s.on_trade(Trade(MINT, now + 1 + i, f"late{i}", "buy", 0.8, tok, c.v_sol, c.v_tokens), E.bundle_window_s)
    return s, now + 21


RED = {"max_bundle_pct": E.max_bundle_pct, "max_early_sold_ratio": E.max_early_sold_ratio, "creator_launches": 1,
       "max_creator_launches_24h": E.max_creator_launches_24h, "max_cluster_pct": E.funding.max_cluster_pct}


def test_graduation_play_entry_and_exit_before_migration():
    s, now = _late_state()
    assert L.min_curve_pct <= s.curve.progress * 100 <= L.max_curve_pct
    ok, why = evaluate_late_entry(s, now, L, RED)
    assert ok, why
    assert evaluate_late_entry(s, now, L, {**RED, "creator_launches": 9})[1] == "serial deployer"
    s.dev_sold = 1
    assert evaluate_late_entry(s, now, L, RED) == (False, "red flag")
    s.dev_sold = 0
    pos = SniperPosition(MINT, "T", now, s.curve.price, 1e6, 1e6, 0.05, 0.05, 0, peak_price=s.curve.price, exits=[])
    assert evaluate_late_exit(pos, s, now + 1, L, X) is None
    c = s.curve                                                     # buy the curve up to the exit level
    while c.progress * 100 < L.exit_curve_pct:
        tok = c.quote_buy(2.0, 0)
        c.apply(2.0, -tok)
    frac, why = evaluate_late_exit(pos, s, now + 2, L, X)
    assert frac == 1.0 and why.startswith("graduation exit")


def test_running_engine_hot_reloads_a_retrained_model(tmp_path, monkeypatch):
    import copy as _copy
    import os

    from meme_trader.sniper.engine import Engine
    from meme_trader.sniper.execution import PaperExecutor
    from tests.test_review_fixes import Quiet

    monkeypatch.setattr("meme_trader.sniper.engine.DATA", tmp_path)
    p = _copy.deepcopy(P)
    p.sniper.predict.model_path = str(tmp_path / "model.json")
    eng = Engine(p, Quiet(realtime=True), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)
    assert eng.model is None
    X, y = _separable(200)
    m = LogisticModel.fit(X, y)
    m.info = {"test": {"auc": 0.71}, "source": "recorded"}       # trained, but never promoted
    m.save(tmp_path / "model.json")
    os.utime(tmp_path / "model.json", (1, 1_900_000_000))
    eng.now = 1_000.0
    eng._maybe_reload_model()
    assert eng.model is None and "isn't a promoted model" in eng.log[-1]["text"]
    m.info["promoted"] = True
    m.save(tmp_path / "model.json")
    os.utime(tmp_path / "model.json", (1, 1_900_000_100))
    eng.now = 2_000.0
    eng._maybe_reload_model()
    assert eng.model is not None and "0.710" in eng.log[-1]["text"]
