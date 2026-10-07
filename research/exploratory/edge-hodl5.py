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


import os
DELAY = float(os.environ.get('DELAY', '2.5'))
import asyncio, numpy as np, lightgbm as lgb
from meme_trader.sniper.feeds import FileFeed
from meme_trader.sniper.events import Launch, Trade, Migration
from meme_trader.sniper.tracker import TokenState
ex = P.sniper.execution
COST = 2 * (ex.curve_fee_pct + ex.platform_fee_pct + ex.paper_latency_slippage_pct) / 100
t0 = time.time()
Xtr, ytr, _, _ = ds("train34", ["feed-2026-10-03.jsonl.gz", "feed-2026-10-04.jsonl.gz"])
Xte, yte, mte, rte = ds("test5", ["feed-2026-10-05.jsonl.gz"])
print(f"train {len(ytr)} test {len(yte)} ({time.time()-t0:.0f}s)", flush=True)
A, B = np.array(Xtr, dtype=np.float32), np.array(ytr, dtype=np.float32)
cut = int(len(B) * .85)
bst = lgb.train({"objective": "binary", "metric": "auc", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 200,
                 "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "lambda_l2": 1.0, "verbose": -1,
                 "num_threads": 4, "seed": 7},
                lgb.Dataset(A[:cut], B[:cut]), 2000, valid_sets=[lgb.Dataset(A[cut:], B[cut:])],
                callbacks=[lgb.early_stopping(100, verbose=False)])
pg = bst.predict(np.array(Xte, dtype=np.float32), num_iteration=bst.best_iteration)
print("AUC Oct 5", round(pm.auc(yte, pg.tolist()), 4), flush=True)
def picks(p, thr):
    seen, out = set(), []
    for i in sorted(range(len(p)), key=lambda i: mte[i][1]):
        m, ts = mte[i][0], mte[i][1]
        if m not in seen and p[i] >= thr:
            seen.add(m); out.append((m, ts))
    return out
SETS = {f"gbm>={t}": picks(pg, t) for t in (0.3, 0.35, 0.4)}
want = {}
for name, ps in SETS.items():
    for m, ts in ps:
        want.setdefault(m, set()).add(ts)
print({k: len(v) for k, v in SETS.items()}, flush=True)
# price paths: every trade price for 30 min after each pick moment
H = 3600
paths = {}                               # (mint, ts) -> [(t, price)], first element = entry price at ts
tok = {}
async def collect():
    async for e in FileFeed(D + "feed-2026-10-05.jsonl.gz").events():
        if isinstance(e, Launch):
            if e.mint in want and e.mint not in tok:
                s = tok[e.mint] = TokenState(e.mint, e, e.ts); s.on_launch(e)
        elif isinstance(e, Trade) and e.mint in tok:
            s = tok[e.mint]
            for ts in want[e.mint]:
                k = (e.mint, ts)
                if e.ts >= ts and k not in paths and s.price_known:
                    paths[k] = [(ts, s.curve.price)]          # the price at the pick moment (before this trade)
            s.on_trade(e, P.sniper.entry.bundle_window_s, P.sniper.entry.sniper_window_s)
            for ts in want[e.mint]:
                k = (e.mint, ts)
                if k in paths and e.ts <= ts + H:
                    paths[k].append((e.ts, s.curve.price))
        elif isinstance(e, Migration) and e.mint in tok:
            tok[e.mint].migrated = True
asyncio.run(collect())
print(f"paths {len(paths)} ({time.time()-t0:.0f}s)", flush=True)
END = max(p[-1][0] for p in paths.values())

import bisect
def at(path, t):
    """price of the last trade at or before t"""
    i = bisect.bisect_right([x[0] for x in path], t) - 1
    return path[max(i, 0)][1]
def run(path, tp, sl, T, arm, trail, delay=DELAY):
    """decided at path[0][0]; buy lands `delay` s later at that price; each sell lands `delay` s after its trigger"""
    t0_ = path[0][0]
    p0 = at(path, t0_ + delay)
    peak = p0
    last_t = t0_
    for t, px in path[1:]:
        if t < t0_ + delay:
            continue
        if t - t0_ > T:
            return at(path, t0_ + T + delay) / p0 - 1 - COST
        g = px / p0 - 1
        peak = max(peak, px)
        hit = (tp is not None and g >= tp) or (sl is not None and g <= -sl) or \
              (trail is not None and peak / p0 - 1 >= arm and px <= peak * (1 - trail))
        if hit:
            return at(path, t + delay) / p0 - 1 - COST
    return path[-1][1] / p0 - 1 - COST
FIXED = {"C: stop 40, trail 25 after +30, out 30m": (None, 0.4, 1800, 0.3, 0.25),
         "H1: stop 50, trail 40 after +100, out 60m": (None, 0.5, 3600, 1.0, 0.4),
         "H2: stop 50, trail 30 after +50, out 60m": (None, 0.5, 3600, 0.5, 0.3),
         "H3: stop 60, no trail, out 60m": (None, 0.6, 3600, 0, None),
         "H4: no stop, hold 60m": (None, None, 3600, 0, None),
         "H5: stop 40, take +200%, out 60m": (2.0, 0.4, 3600, 0, None)}
out = {}
for name, ps in SETS.items():
    ks = [k for k in ps if k in paths and paths[k][0][0] + 3600 <= END]
    for fx, g in FIXED.items():
        r = [run(paths[k], *g) for k in ks]
        hrs = {}
        for k, x in zip(ks, r):
            hrs.setdefault(int((k[1] // 21600)), []).append(x)
        row = {"se": round(100 * st.pstdev(r) / len(r) ** .5, 1), "coins": len(r), "avg": round(100 * st.fmean(r), 1), "median": round(100 * st.median(r), 1),
               "won": round(100 * sum(x > 0 for x in r) / len(r)), "by_6h_avg": [round(100 * st.fmean(v), 1) for _, v in sorted(hrs.items())]}
        out[f"{name} | {fx}"] = row
        print(name, "|", fx, row, flush=True)
json.dump(out, open(S + "hodl5.json", "w"), indent=1)
print(f"done {time.time()-t0:.0f}s")
