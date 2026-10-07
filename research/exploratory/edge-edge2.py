"""Offline edge test. Train on Oct 3-5, grade on Oct 6 (unseen). For every labelled moment also keep what a
simple bracket trade would have made from there (+100% take, -30% stop at the price seen, else sell at 10 min),
so model picks can be scored in % after fees, one buy per coin."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import inspect, json, pickle, sys, time, gc, os, statistics as st
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper import predictor as pm
from meme_trader.sniper.sweep import load_events
S = OUT
D = DATA + ""
P = config.load(config.ROOT / "config/params.yaml")

src = inspect.getsource(pm.build_dataset)
def sub(a, b):
    global src
    assert src.count(a) == 1, a
    src = src.replace(a, b)
sub("def build_dataset(", "def build_dataset_r(")
sub("    meta: list = []\n", "    meta: list = []\n    R: dict = {}\n    LAST: dict = {}\n")
sub("open_labels[mint].append([len(y) - 1, s.curve.price, ts + horizon])",
    "open_labels[mint].append([len(y) - 1, s.curve.price, ts + horizon]); LAST[len(y) - 1] = s.curve.price")
sub("""            if ts > deadline:
                y[row] = 0
            elif price is not None and price >= p0 * (1 + up):
                y[row] = 1
            elif price is not None and price <= p0 * (1 - down):
                y[row] = 0
            else:
                keep.append([row, p0, deadline])""",
"""            if ts > deadline:
                y[row] = 0; R[row] = LAST[row] / p0 - 1
            elif price is not None and price >= p0 * (1 + up):
                y[row] = 1; R[row] = up
            elif price is not None and price <= p0 * (1 - down):
                y[row] = 0; R[row] = price / p0 - 1
            else:
                keep.append([row, p0, deadline])
                if price is not None:
                    LAST[row] = price""")
sub("""                y[row] = 0                               # watched the whole horizon: it never doubled""",
"""                y[row] = 0; R[row] = LAST[row] / _ - 1""")
sub("for row, _, deadline in rows:", "for row, _, deadline in rows:  # _ = p0")
sub("return [X[i] for i in keep], [y[i] for i in keep], [meta[i] for i in keep]",
    "return [X[i] for i in keep], [y[i] for i in keep], [meta[i] for i in keep], [R[i] for i in keep]")
ns = dict(vars(pm)); exec(src, ns); build = ns["build_dataset_r"]

def ds(name, files):
    f = S + name + ".pkl"
    if os.path.exists(f):
        return pickle.load(open(f, "rb"))
    ev = load_events({"files": [D + x for x in files]})
    out = build(ev, P)
    del ev; gc.collect()
    pickle.dump(out, open(f, "wb"))
    return out

t0 = time.time()
Xtr, ytr, mtr, rtr = ds("train", ["feed-2026-10-03.jsonl.gz", "feed-2026-10-04.jsonl.gz", "feed-2026-10-05.jsonl.gz"])
Xte, yte, mte, rte = ds("test", ["feed-2026-10-06.jsonl.gz"])
print(f"train {len(ytr)}  test {len(yte)}  ({time.time()-t0:.0f}s)", flush=True)
ex = P.sniper.execution
cost = 2 * (ex.curve_fee_pct + ex.platform_fee_pct + ex.paper_latency_slippage_pct) / 100
print(f"round-trip cost {cost:.1%}", flush=True)

def trades(p, thr, meta, rets):
    """One buy per coin: its first moment at or above thr."""
    seen, out = set(), []
    for i in sorted(range(len(p)), key=lambda i: meta[i][1]):
        m = meta[i][0]
        if m in seen or p[i] < thr:
            continue
        seen.add(m)
        out.append(rets[i] - cost)
    return out

def table(name, p):
    print(f"\n{name}: one buy per coin on Oct 6, bracket +100%/-30%/10 min, after {cost:.1%} fees", flush=True)
    rows = []
    for thr in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
        t = trades(p, thr, mte, rte)
        if len(t) < 10:
            continue
        r = {"min_p": thr, "coins": len(t), "avg_pct": round(100 * st.fmean(t), 1), "median_pct": round(100 * st.median(t), 1),
             "won_pct": round(100 * sum(x > 0 for x in t) / len(t)), "doubled_pct": round(100 * sum(x > .9 for x in t) / len(t))}
        rows.append(r); print(r, flush=True)
    return rows

out = {"cost": cost}
live = pm.LogisticModel.load(D + "model.json")
pl = live.predict_rows(Xte)
out["auc_live"] = round(pm.auc(yte, pl), 4)
out["live"] = table("live model", pl)

import numpy as np, lightgbm as lgb
A, B = np.array(Xtr, dtype=np.float32), np.array(ytr, dtype=np.float32)
cut = int(len(B) * .85)
bst = lgb.train({"objective": "binary", "metric": "auc", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 200,
                 "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "lambda_l2": 1.0, "verbose": -1,
                 "num_threads": 4},
                lgb.Dataset(A[:cut], B[:cut]), 2000, valid_sets=[lgb.Dataset(A[cut:], B[cut:])],
                callbacks=[lgb.early_stopping(100, verbose=False)])
pg = bst.predict(np.array(Xte, dtype=np.float32), num_iteration=bst.best_iteration).tolist()
out["auc_gbm"] = round(pm.auc(yte, pg), 4)
print(f"\nAUC on Oct 6: live {out['auc_live']}  gbm {out['auc_gbm']} ({bst.best_iteration} trees)", flush=True)
out["gbm"] = table("gbm", pg)
cal = []
for lo in (0, .1, .2, .3, .4, .5):
    ii = [i for i, x in enumerate(pg) if lo <= x < lo + .1]
    if ii:
        cal.append([lo, len(ii), round(st.fmean(pg[i] for i in ii), 3), round(st.fmean(yte[i] for i in ii), 3)])
out["gbm_cal"] = cal
print("gbm calibration [lo, n, predicted, actual]", cal)
imp = sorted(zip(pm.FEATURES, bst.feature_importance("gain")), key=lambda kv: -kv[1])[:12]
out["gbm_features"] = [[k, round(float(v))] for k, v in imp]
print("gbm top features", out["gbm_features"])
json.dump(out, open(S + "edge2.json", "w"), indent=1)
print(f"done {time.time()-t0:.0f}s")
