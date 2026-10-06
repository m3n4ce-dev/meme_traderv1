"""The stronger model (trees) and its picks: plain-Python trees, picks at the trained ages, and the exit lab's
follow of each pick, filled instantly and as late as the paper bot lands."""
import copy
from types import SimpleNamespace as NS

import pytest

from meme_trader import config
from meme_trader.sniper.curve import INITIAL_V_TOKENS, Curve
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.exitlab import PICK_EXITS, PICKS, ExitLab
from meme_trader.sniper.features import FEATURES
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.predictor import TreeModel
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    def __init__(self, realtime=True):
        self.realtime = realtime

    async def events(self):
        return
        yield


def stump(feature: str, thr: float, lo: float, hi: float, nan_left=True) -> dict:
    return {"f": [FEATURES.index(feature), -1, -1], "t": [thr, 0, 0], "l": [1, -1, -1], "r": [2, -1, -1],
            "v": [0, lo, hi], "d": [nan_left, True, True]}


def test_plain_python_trees_sum_their_leaves(tmp_path):
    m = TreeModel(FEATURES, [stump("buyers_20s", 10, -3.0, 1.0), stump("curve_pct", 50, 0.0, 0.5)])
    row = [0.0] * len(FEATURES)
    assert abs(m.predict_rows([row])[0] - 1 / (1 + 2.718281828 ** 3)) < 1e-6        # both left: -3 + 0
    row[FEATURES.index("buyers_20s")], row[FEATURES.index("curve_pct")] = 20, 80
    assert abs(m.raw_row(row) - 1.5) < 1e-9
    row[FEATURES.index("buyers_20s")] = float("nan")                                 # missing: the trained side
    assert abs(m.raw_row(row) - (-3.0 + 0.5)) < 1e-9
    m.save(tmp_path / "t.json")
    assert TreeModel.load(tmp_path / "t.json").raw_row(row) == m.raw_row(row)
    (tmp_path / "old.json").write_text('{"kind": "trees", "features": ["x"], "trees": []}')
    assert TreeModel.load(tmp_path / "old.json") is None                             # another feature set: ignored


def priced(mint="M" * 40 + "pump", price=1.0):
    s = TokenState(mint, Launch(mint, 0.0, "dev", symbol="PK"), 0.0)
    s.price_known = True
    s.curve = Curve(price * INITIAL_V_TOKENS, INITIAL_V_TOKENS)
    return s


def set_price(s, price):
    s.curve = Curve(price * INITIAL_V_TOKENS, INITIAL_V_TOKENS)


def test_a_pick_is_followed_instantly_and_landing_late():
    lab, p = ExitLab(None, 1.0), copy.deepcopy(P).sniper
    p.execution["paper_delay_s"] = 2.5
    s = priced()
    lab.start(s.mint, "PK", PICKS, s.curve.price, 100.0, p, extra={"p": 0.33})
    lab.start(s.mint, "PK", PICKS, s.curve.price, 100.0, p)                 # one follow per coin and kind
    lab.start(s.mint, "PK", "sniper-pass", s.curve.price, 100.0, p)         # another kind on the same coin: fine
    names = [sh["variant"] for sh in lab.open[s.mint] if sh["kind"] == PICKS]
    assert len(names) == 2 * len(PICK_EXITS) + 1 and sum("lands 2.5 s late" in n for n in names) == len(PICK_EXITS)
    toks = {s.mint: s}
    set_price(s, 1.5)                    # +50% before the late buy lands: it pays 1.5
    lab.tick(toks, 101.0, p)
    lab.tick(toks, 103.0, p)
    late = next(sh for sh in lab.open[s.mint] if sh["variant"] == "+100% or -30%, out at 10 min · lands 2.5 s late")
    assert abs(late["pos"].entry_price / s.curve.price - 1) < 1e-9
    set_price(s, 2.05)                   # instant: +105% -> take profit now; late: only +37% on its 1.5 entry
    lab.tick(toks, 104.0, p)
    done = {r["variant"]: r for r in lab.done}
    assert done["+100% or -30%, out at 10 min"]["pnl_pct"] > 100 and done["+100% or -30%, out at 10 min"]["p"] == 0.33
    set_price(s, 0.9)                    # late: stop (-40%) triggers, the sell lands 2.5 s later at a lower price
    lab.tick(toks, 105.0, p)
    assert "+100% or -30%, out at 10 min · lands 2.5 s late" not in {r["variant"] for r in lab.done}
    set_price(s, 0.8)
    lab.tick(toks, 107.6, p)
    r = {r["variant"]: r for r in lab.done}["+100% or -30%, out at 10 min · lands 2.5 s late"]
    assert abs(r["pnl_pct"] - ((0.8 / 1.5) * 0.99 / 1.01 - 1) * 100) < 0.05
    v = lab.picks_view()
    assert v["picks"] == 1 and {x["variant"] for x in v["bands"]["30%+"]} >= {"+100% or -30%, out at 10 min"}
    assert v["bands"]["25-30%"] == [] and lab.view("sniper")["entries"] == 0     # picks aren't sniper trades


def test_the_bot_picks_at_the_trained_ages_once_and_never_in_replays():
    e = Engine(copy.deepcopy(P), Quiet(), PaperExecutor(P.sniper.execution), mode="paper", log_to_journal=False, persist=False)
    seen = []
    e.pick_model = NS(predict=lambda f: seen.append(f) or 0.31, trees=[])
    s = priced()
    s.buyers.update({"a", "b", "c"})
    e.tokens[s.mint] = s
    e.now = 10.0
    e._model_picks()
    assert not seen and s.mint not in e.lab.open                  # 10 s old: before the first checkpoint
    e.now = 16.0
    e._model_picks()
    assert len(seen) == 1 and e.lab.open[s.mint][0]["extra"] == {"p": 0.31, "age_s": 16}
    e.now = 31.0
    e._model_picks()
    assert len(seen) == 1                                         # picked: not looked at again
    r = Engine(copy.deepcopy(P), Quiet(realtime=False), PaperExecutor(P.sniper.execution), mode="paper",
               log_to_journal=False, persist=False)
    r.pick_model = e.pick_model
    r.tokens[s.mint], r.now = priced(), 16.0
    r._model_picks()
    assert not r.lab.open                                         # replays: no picks


def test_train_trees_end_to_end_on_a_synthetic_market():
    pytest.importorskip("lightgbm")                               # optional: only `train --kind trees` needs it
    from meme_trader.sniper.predictor import train_trees
    from meme_trader.sniper.sweep import load_events
    events = load_events({"synthetic": 2000, "seed": 3})
    m, info = train_trees(events, P, source={"synthetic": 2000})
    assert info["trees"] >= 1 and info["source"] == "synthetic" and 0 <= m.predict({}) <= 1
