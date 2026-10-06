"""The Oracle: a learned probability that a token doubles before it drops 30% (within 10 minutes).

Not magic and not "quantum" - a calibrated logistic regression on the features in features.py,
trained on YOUR recorded launches and graded on later launches it never saw (walk-forward).
That matters doubly here: research found pump.fun models trained on one period do not carry
over to later ones (arXiv 2607.02823), so retrain often and trust the out-of-sample numbers only.

    python -m meme_trader.sniper train --file data/feed-*           # writes data/model.json

How the bot uses it (sniper.predict): P(2x first) is shown for every candidate, can gate entries
(min_p), and feeds quarter-Kelly sizing inside the sizing agent's $ hard cap.
Pure Python (no numpy needed). TreeModel is the stronger kind: gradient-boosted trees, fitted offline with LightGBM
(`train --kind trees`, which needs the lightgbm package) and run here in plain Python. Held out on Oct 5 and Oct 6
it ranked better than the logistic model (AUC 0.86 vs 0.83) and its estimates matched what happened.
"""
from __future__ import annotations

import heapq
import json
import math
import random
import time
from collections import defaultdict, deque
from pathlib import Path

from .curve import Curve
from .events import Funding, Launch, Metadata, Migration, Trade
from .features import FEATURES, extract
from .funding import cluster_report, cohort
from .tracker import TokenState


# --------------------------------------------------------------------------- dataset
def build_dataset(events, params, checkpoints=None, up_pct=None, down_pct=None, horizon_s=None):
    """Replay events; snapshot features at each checkpoint age; label = hit +up before -down within horizon.
    Returns (X rows, y labels, meta (mint, ts, curve_pct)).

    A snapshot whose horizon runs past the end of the recording is censored (dropped): we never saw
    whether it doubled, and calling it a loss would bias recent data toward failure."""
    pr = params.sniper.predict
    checkpoints = list(checkpoints or pr.checkpoints_s)
    up = (up_pct if up_pct is not None else pr.up_pct) / 100
    down = (down_pct if down_pct is not None else pr.down_pct) / 100
    horizon = horizon_s if horizon_s is not None else pr.horizon_s
    en = params.sniper.entry
    exch_fanout = en.funding.exchange_fanout

    tokens: dict[str, TokenState] = {}
    remaining_cp: dict[str, int] = {}
    due: list = []
    seq = 0
    creators: dict[str, deque] = defaultdict(deque)
    symbols: dict[str, deque] = defaultdict(deque)
    funders: dict[str, tuple] = {}
    fanout: dict[str, int] = defaultdict(int)
    open_labels: dict[str, list] = defaultdict(list)       # mint -> [[row, p0, deadline], ...]
    X: list[list[float]] = []
    y: list = []
    meta: list = []

    def count(book: dict, key: str, ts: float, horizon_s: float) -> int:
        q = book.get(key)
        if not q:
            return 0
        while q and q[0] < ts - horizon_s:
            q.popleft()
        return len(q)

    def snapshot(mint: str, ts: float) -> None:
        s = tokens.get(mint)
        remaining_cp[mint] = remaining_cp.get(mint, 1) - 1
        if s is None or not s.price_known or len(s.buyers) < 3 or s.migrated:
            return
        if funders:
            s.cluster = cluster_report(s, cohort(s, en.funding.cohort_size), funders, fanout, exch_fanout)
        ctx = {"creator_launches": count(creators, s.creator, ts, 86400),
               "symbol_dupes": max(count(symbols, s.symbol.upper(), ts, 3600) - 1, 0)}
        f = extract(s, ts, ctx)
        X.append([f[k] for k in FEATURES])
        y.append(None)
        meta.append((mint, ts, s.curve.progress * 100))
        open_labels[mint].append([len(y) - 1, s.curve.price, ts + horizon])

    def settle(mint: str, ts: float, price: float | None) -> None:
        keep = []
        for row, p0, deadline in open_labels.get(mint, []):
            if ts > deadline:
                y[row] = 0
            elif price is not None and price >= p0 * (1 + up):
                y[row] = 1
            elif price is not None and price <= p0 * (1 - down):
                y[row] = 0
            else:
                keep.append([row, p0, deadline])
        if keep:
            open_labels[mint] = keep
        else:
            open_labels.pop(mint, None)
            if remaining_cp.get(mint, 0) <= 0:          # nothing left to snapshot or label: free memory
                tokens.pop(mint, None)

    end_ts = float("-inf")
    for e in sorted(events, key=lambda ev: ev.ts):
        end_ts = max(end_ts, e.ts)
        while due and due[0][0] <= e.ts:
            t, _, m = heapq.heappop(due)
            snapshot(m, t)
        if isinstance(e, Metadata) or (isinstance(e, Launch) and e.mint in tokens):
            # social links arrive after the launch: apply them from their own timestamp, as the live
            # engine does (recordings before 2026-10-03 stored them as a repeated Launch at launch time)
            s = tokens.get(e.mint)
            if s is not None and s.launch is not None:
                s.launch.twitter, s.launch.telegram, s.launch.website = e.twitter, e.telegram, e.website
            continue
        if isinstance(e, Launch):
            s = tokens[e.mint] = TokenState(e.mint, e, e.ts)
            s.on_launch(e)
            creators[e.creator].append(e.ts)
            symbols[e.symbol.upper()].append(e.ts)
            remaining_cp[e.mint] = len(checkpoints)
            for c in checkpoints:
                seq += 1
                heapq.heappush(due, (e.ts + c, seq, e.mint))
        elif isinstance(e, Trade):
            s = tokens.get(e.mint)
            if s is None:
                continue
            s.on_trade(e, en.bundle_window_s, en.sniper_window_s)
            if e.mint in open_labels:
                settle(e.mint, e.ts, s.curve.price)
        elif isinstance(e, Funding):
            if e.wallet not in funders and e.funder:
                fanout[e.funder] += 1
            funders[e.wallet] = (e.funder, e.funder_type)
        elif isinstance(e, Migration):
            s = tokens.get(e.mint)
            if s:
                s.migrated = True
    censored = set()
    for rows in open_labels.values():
        for row, _, deadline in rows:
            if deadline <= end_ts:
                y[row] = 0                               # watched the whole horizon: it never doubled
            else:
                censored.add(row)                        # horizon not fully observed: outcome unknown
    keep = [i for i in range(len(y)) if i not in censored and y[i] is not None]
    return [X[i] for i in keep], [y[i] for i in keep], [meta[i] for i in keep]


# --------------------------------------------------------------------------- model
def _sigmoid(s: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, s))))


def fit_temperature(logits: list[float], y: list[int]) -> float:
    """Temperature scaling: one number T so that sigmoid(logit / T) is calibrated (minimum log loss).
    T > 1 tames an overconfident model. Kelly sizing uses the probability directly, so this matters."""
    if len(y) < 30 or not 0 < sum(y) < len(y):
        return 1.0

    def loss(t: float) -> float:
        return -sum(yi * math.log(max(_sigmoid(z / t), 1e-9)) + (1 - yi) * math.log(max(1 - _sigmoid(z / t), 1e-9))
                    for z, yi in zip(logits, y))
    lo, hi = 0.5, 6.0                                     # golden-section search (loss is unimodal in T)
    g = (5 ** 0.5 - 1) / 2
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    fa, fb = loss(a), loss(b)
    for _ in range(40):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - g * (hi - lo)
            fa = loss(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + g * (hi - lo)
            fb = loss(b)
    return round((lo + hi) / 2, 3)


class LogisticModel:
    def __init__(self, features, mean, std, w, b, info=None, temp: float = 1.0):
        self.features, self.mean, self.std, self.w, self.b = list(features), mean, std, w, b
        self.info = info or {}
        self.temp = temp

    @staticmethod
    def _standardize(X, mean, std):
        return [[max(-5.0, min(5.0, (v - m) / s)) for v, m, s in zip(row, mean, std)] for row in X]

    @classmethod
    def fit(cls, X, y, features=FEATURES, l2=1e-3, epochs=40, lr=0.05, seed=7):
        n, d = len(X), len(features)
        mean = [sum(r[j] for r in X) / n for j in range(d)]
        std = [max((sum((r[j] - mean[j]) ** 2 for r in X) / n) ** 0.5, 1e-9) for j in range(d)]
        Z = cls._standardize(X, mean, std)
        w, b = [0.0] * d, 0.0
        base = sum(y) / n
        b = math.log(max(base, 1e-4) / max(1 - base, 1e-4))      # start at the base rate
        order = list(range(n))
        rng = random.Random(seed)
        for ep in range(epochs):
            rng.shuffle(order)
            step = lr / (1 + 0.15 * ep)
            for i in order:
                z = Z[i]
                s = b + sum(wj * zj for wj, zj in zip(w, z))
                p = 1 / (1 + math.exp(-max(-30.0, min(30.0, s))))
                g = p - y[i]
                b -= step * g
                for j in range(d):
                    w[j] -= step * (g * z[j] + l2 * w[j])
        return cls(features, mean, std, w, b)

    def logits_rows(self, X) -> list[float]:
        return [self.b + sum(wj * zj for wj, zj in zip(self.w, z)) for z in self._standardize(X, self.mean, self.std)]

    def predict_rows(self, X) -> list[float]:
        return [_sigmoid(s / self.temp) for s in self.logits_rows(X)]

    def predict(self, feats: dict) -> float:
        return self.predict_rows([[feats.get(k, 0.0) for k in self.features]])[0]

    def drivers(self, feats: dict, top: int = 5) -> list[tuple[str, float]]:
        """Which features pushed this prediction up or down the most (contribution in log-odds)."""
        contrib = []
        for k, m, sd, wk in zip(self.features, self.mean, self.std, self.w):
            z = max(-5.0, min(5.0, (feats.get(k, 0.0) - m) / sd))
            contrib.append((k, wk * z))
        contrib.sort(key=lambda kv: -abs(kv[1]))
        return [(k, round(c, 3)) for k, c in contrib[:top]]

    def to_dict(self) -> dict:
        return {"features": self.features, "mean": self.mean, "std": self.std, "w": self.w, "b": self.b,
                "temp": self.temp, "info": self.info}

    @classmethod
    def from_dict(cls, d: dict) -> "LogisticModel":
        return cls(d["features"], d["mean"], d["std"], d["w"], d["b"], d.get("info"), d.get("temp", 1.0))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1))

    @classmethod
    def load(cls, path: str | Path) -> "LogisticModel | None":
        path = Path(path)
        if not path.exists():
            return None
        try:
            m = cls.from_dict(json.loads(path.read_text()))
        except (ValueError, KeyError):
            return None
        return m if m.features == FEATURES else None     # stale model from an older feature set: ignore


class TreeModel:
    """Gradient-boosted trees: the sum of every tree's leaf value, through a sigmoid. Each tree is flat lists: a node
    with feature f >= 0 sends a row left when row[f] <= threshold (NaN: the trained default side), else it is a leaf."""
    kind = "trees"

    def __init__(self, features, trees, info=None):
        self.features, self.trees, self.info = list(features), trees, info or {}

    @classmethod
    def from_lightgbm(cls, booster, features=FEATURES, num_iteration=None, info=None) -> "TreeModel":
        trees = []
        for t in booster.dump_model(num_iteration=num_iteration)["tree_info"]:
            f, thr, left, right, val, dleft = [], [], [], [], [], []

            def walk(n) -> int:
                i = len(f)
                f.append(-1), thr.append(0.0), left.append(-1), right.append(-1), dleft.append(True)
                val.append(float(n.get("leaf_value", 0.0)))
                if "split_feature" in n:
                    assert n.get("decision_type", "<=") == "<="
                    f[i], thr[i], dleft[i] = int(n["split_feature"]), float(n["threshold"]), bool(n.get("default_left", True))
                    left[i] = walk(n["left_child"])
                    right[i] = walk(n["right_child"])
                return i
            walk(t["tree_structure"])
            trees.append({"f": f, "t": thr, "l": left, "r": right, "v": val, "d": dleft})
        return cls(features, trees, info)

    def raw_row(self, row) -> float:
        out = 0.0
        for t in self.trees:
            f, thr, left, right, dl = t["f"], t["t"], t["l"], t["r"], t["d"]
            i = 0
            while f[i] >= 0:
                x = row[f[i]]
                i = (left[i] if dl[i] else right[i]) if x != x else (left[i] if x <= thr[i] else right[i])
            out += t["v"][i]
        return out

    def predict_rows(self, X) -> list[float]:
        return [_sigmoid(self.raw_row(r)) for r in X]

    def predict(self, feats: dict) -> float:
        return _sigmoid(self.raw_row([feats.get(k, 0.0) for k in self.features]))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "features": self.features, "trees": self.trees, "info": self.info}

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), separators=(",", ":")))

    @classmethod
    def load(cls, path: str | Path) -> "TreeModel | None":
        path = Path(path)
        try:
            d = json.loads(path.read_text())
            m = cls(d["features"], d["trees"], d.get("info"))
        except (OSError, ValueError, KeyError):
            return None
        return m if d.get("kind") == cls.kind and m.features == FEATURES else None


def train_trees(events, params, train_frac: float = 0.7, log=print, source: dict | None = None) -> tuple[TreeModel, dict]:
    """The same walk-forward as train(): fit on the earliest slice, stop adding trees when the next slice stops
    improving, grade on the latest. Then refit on everything with that many trees."""
    try:
        import lightgbm as lgb
        import numpy as np                               # (lightgbm's own dependency: it takes arrays, not lists)
    except ImportError:
        raise SystemExit("train --kind trees needs the lightgbm package: .venv/bin/pip install lightgbm")
    from .sweep import split

    t0 = time.time()
    pr = params.sniper.predict
    embargo = max(pr.checkpoints_s) + pr.horizon_s
    tr_ev, rest = split(events, train_frac, embargo)
    val_ev, te_ev = split(rest, 0.5, embargo)
    (Xtr, ytr, _), (Xv, yv, _), (Xte, yte, _) = (build_dataset(e, params) for e in (tr_ev, val_ev, te_ev))
    log(f"samples: train {len(ytr)} (positives {sum(ytr)}), validate {len(yv)}, test {len(yte)} (positives {sum(yte)})")
    if len(ytr) < 500 or sum(ytr) < 50 or not yv:
        raise SystemExit("not enough labelled samples to train trees yet - record more data first")
    cfg = {"objective": "binary", "metric": "auc", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 200,
           "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "lambda_l2": 1.0, "verbose": -1,
           "num_threads": 4, "seed": 7}
    def data(X, y):
        return lgb.Dataset(np.asarray(X, dtype=np.float64), np.asarray(y, dtype=np.float64))
    held = lgb.train(cfg, data(Xtr, ytr), 2000, valid_sets=[data(Xv, yv)],
                     callbacks=[lgb.early_stopping(100, verbose=False)])
    n_trees = max(held.best_iteration, 1)
    holdout = TreeModel.from_lightgbm(held, num_iteration=n_trees)
    test_metrics = evaluate(yte, holdout.predict_rows(Xte)) if yte else {"n": 0}
    Xall, yall, _ = build_dataset(events, params)
    final = TreeModel.from_lightgbm(lgb.train(cfg, data(Xall, yall), n_trees))
    ts = [e.ts for e in events]
    src = source or {}
    final.info = {
        "kind": "trees", "source": "synthetic" if src.get("synthetic") else ("recorded" if src.get("files") else "unknown"),
        "files": [Path(f).name for f in src.get("files", [])],
        "data_start_ts": min(ts) if ts else None, "data_end_ts": max(ts) if ts else None, "embargo_s": embargo,
        "trained_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "label": f"+{pr.up_pct}% before -{pr.down_pct}% within {pr.horizon_s}s", "trees": n_trees,
        "n_train": len(ytr), "n_validate": len(yv), "n_test": len(yte), "test": test_metrics,
        "importance": sorted(([k, round(float(v))] for k, v in zip(FEATURES, held.feature_importance("gain"))),
                             key=lambda kv: -kv[1])[:12],
        "seconds": round(time.time() - t0, 1)}
    return final, final.info


# --------------------------------------------------------------------------- metrics
def auc(y, p) -> float:
    pairs = sorted(zip(p, y))
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return 0.5
    rank_sum, i = 0.0, 0
    while i < len(pairs):                                   # average ranks for ties
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if pairs[k][1] == 1)
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def evaluate(y, p) -> dict:
    n = len(y)
    if not n:
        return {"n": 0}
    base = sum(y) / n
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / n
    logloss = -sum(yi * math.log(max(pi, 1e-9)) + (1 - yi) * math.log(max(1 - pi, 1e-9)) for pi, yi in zip(p, y)) / n
    order = sorted(range(n), key=lambda i: -p[i])
    top = order[: max(1, n // 10)]
    top_rate = sum(y[i] for i in top) / len(top)
    bins = []
    for k in range(10):
        lo, hi = k / 10, (k + 1) / 10
        idx = [i for i in range(n) if lo <= p[i] < hi or (k == 9 and p[i] == 1.0)]
        if idx:
            bins.append({"lo": lo, "hi": hi, "n": len(idx), "predicted": sum(p[i] for i in idx) / len(idx),
                         "actual": sum(y[i] for i in idx) / len(idx)})
    return {"n": n, "base_rate": base, "auc": auc(y, p), "brier": brier,
            "brier_skill": 1 - brier / (base * (1 - base)) if 0 < base < 1 else 0.0,
            "logloss": logloss, "top_decile_rate": top_rate, "top_decile_lift": top_rate / base if base else 0.0,
            "calibration": bins}


def train(events, params, train_frac: float = 0.7, log=print, source: dict | None = None) -> tuple[LogisticModel, dict]:
    """Walk-forward in three slices by launch time: fit on the earliest, calibrate (temperature) on the
    next, grade on the latest - data the model and its calibration never saw. The splits are purged
    with an embargo of the longest checkpoint + horizon, so every training label is known before the
    first later decision. Then refit on everything for deployment, keeping the calibration.

    The deployment model has seen ALL of `events`; info records that window (data_start_ts/data_end_ts)
    and where the data came from, so replays of the same data refuse to use it (Engine) and only a
    model trained on recorded data can be promoted (`promote`)."""
    from .sweep import split

    t0 = time.time()
    pr = params.sniper.predict
    embargo = max(pr.checkpoints_s) + pr.horizon_s
    tr_ev, rest = split(events, train_frac, embargo)
    cal_ev, te_ev = split(rest, 0.5, embargo)
    Xtr, ytr, _ = build_dataset(tr_ev, params)
    Xcal, ycal, _ = build_dataset(cal_ev, params)
    Xte, yte, _ = build_dataset(te_ev, params)
    log(f"samples: train {len(ytr)} (positives {sum(ytr)}), calibrate {len(ycal)} (positives {sum(ycal)}), "
        f"test {len(yte)} (positives {sum(yte)})")
    if len(ytr) < 50 or sum(ytr) < 5:
        raise SystemExit("not enough labelled samples to train yet - record more data first")
    holdout = LogisticModel.fit(Xtr, ytr)
    raw_test = evaluate(yte, holdout.predict_rows(Xte)) if yte else {"n": 0}
    holdout.temp = fit_temperature(holdout.logits_rows(Xcal), ycal)
    test_metrics = evaluate(yte, holdout.predict_rows(Xte)) if yte else {"n": 0}
    train_metrics = evaluate(ytr, holdout.predict_rows(Xtr))
    Xall, yall, _ = build_dataset(events, params)          # deploy on all data; grade from the holdout only
    final = LogisticModel.fit(Xall, yall)
    final.temp = holdout.temp
    ts = [e.ts for e in events]
    src = source or {}
    final.info = {
        "source": "synthetic" if src.get("synthetic") else ("recorded" if src.get("files") else "unknown"),
        "files": [Path(f).name for f in src.get("files", [])],
        "data_start_ts": min(ts) if ts else None, "data_end_ts": max(ts) if ts else None,
        "embargo_s": embargo, "promoted": False,
        "trained_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "label": f"+{pr.up_pct}% before -{pr.down_pct}% within {pr.horizon_s}s",
        "n_train": len(ytr), "n_cal": len(ycal), "n_test": len(yte), "temperature": holdout.temp,
        "train": train_metrics, "test": test_metrics, "test_uncalibrated": {k: raw_test.get(k) for k in
                                                                            ("brier", "brier_skill", "logloss")},
        "weights": sorted(zip(FEATURES, final.w), key=lambda kv: -abs(kv[1])),
        "seconds": round(time.time() - t0, 1),
    }
    return final, final.info


def kelly(p: float, up_pct: float, down_pct: float, cost_pct: float) -> float:
    """Full-Kelly fraction for a bet that wins +up or loses -down (both net of round-trip costs)."""
    win = (up_pct - cost_pct) / 100
    loss = (down_pct + cost_pct) / 100
    if win <= 0 or loss <= 0:
        return 0.0
    b = win / loss
    return (b * p - (1 - p)) / b


def expected_value_pct(p: float, up_pct: float, down_pct: float, cost_pct: float) -> float:
    return p * (up_pct - cost_pct) - (1 - p) * (down_pct + cost_pct)


__all__ = ["LogisticModel", "build_dataset", "train", "evaluate", "auc", "kelly", "expected_value_pct", "Curve"]
