"""Offline: does a stronger model (gradient-boosted trees) find more than the live straight-line P(2x) model?
Train on Oct 3-5 recordings, grade on Oct 6 (never seen: true out of sample)."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import json, sys, time, gc
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper.sweep import load_events
from meme_trader.sniper.predictor import build_dataset, LogisticModel, evaluate, auc, fit_temperature
from meme_trader.sniper.features import FEATURES
D = DATA + ""
P = config.load(config.ROOT / "config/params.yaml")
t0 = time.time()

def ds(files):
    ev = load_events({"files": [D + f for f in files]})
    X, y, meta = build_dataset(ev, P)
    del ev; gc.collect()
    return X, y, meta

Xtr, ytr, mtr = ds(["feed-2026-10-03.jsonl.gz", "feed-2026-10-04.jsonl.gz", "feed-2026-10-05.jsonl.gz"])
print(f"train {len(ytr)} pos {sum(ytr)}  ({time.time()-t0:.0f}s)", flush=True)
Xte, yte, mte = ds(["feed-2026-10-06.jsonl.gz"])
print(f"test (Oct 6) {len(yte)} pos {sum(yte)}  ({time.time()-t0:.0f}s)", flush=True)

def top(y, p, frac):
    idx = sorted(range(len(p)), key=lambda i: -p[i])[:max(1, int(len(p) * frac))]
    return round(sum(y[i] for i in idx) / len(idx), 4), len(idx)

def report(name, y, p):
    r = {"auc": round(auc(y, p), 4), "base": round(sum(y) / len(y), 4)}
    for f in (0.10, 0.02, 0.005):
        r[f"top{f:.1%}"] = top(y, p, f)
    print(name, json.dumps(r), flush=True)
    return r

# the live kind of model, refit on the same 3 days
n = len(ytr); cut = int(n * .85)
lm = LogisticModel.fit(Xtr[:cut], ytr[:cut])
lm.temp = fit_temperature(lm.logits_rows(Xtr[cut:]), ytr[cut:])
out = {"logistic": report("logistic", yte, lm.predict_rows(Xte))}
live = LogisticModel.load(D + "model.json")
out["live_model"] = report("live model.json", yte, live.predict_rows(Xte))

import numpy as np, lightgbm as lgb
A, B = np.array(Xtr, dtype=np.float32), np.array(ytr)
m = lgb.LGBMClassifier(n_estimators=2000, learning_rate=0.03, num_leaves=31, min_child_samples=200,
                       subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, verbose=-1, n_jobs=4)
m.fit(A[:cut], B[:cut], eval_set=[(A[cut:], B[cut:])], eval_metric="auc",
      callbacks=[lgb.early_stopping(100, verbose=False)])
pg = m.predict_proba(np.array(Xte, dtype=np.float32))[:, 1].tolist()
out["gbm"] = report(f"gbm ({m.best_iteration_} trees)", yte, pg)
imp = sorted(zip(FEATURES, m.booster_.feature_importance("gain")), key=lambda kv: -kv[1])[:12]
out["gbm_top_features"] = [[k, round(float(v))] for k, v in imp]
cal = []
for lo in (0, .1, .2, .3, .4, .5):
    ii = [i for i, x in enumerate(pg) if lo <= x < lo + .1]
    if ii:
        cal.append([lo, len(ii), round(sum(pg[i] for i in ii) / len(ii), 3), round(sum(yte[i] for i in ii) / len(ii), 3)])
out["gbm_calibration_bins"] = cal
print("calibration [lo, n, predicted, actual]", cal)
print("top features", out["gbm_top_features"])
json.dump(out, open(OUT + "gbm_vs_logistic.json", "w"), indent=1)
print(f"done {time.time()-t0:.0f}s")
