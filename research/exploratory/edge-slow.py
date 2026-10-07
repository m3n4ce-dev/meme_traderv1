"""Retrain for what can be bought: label = +100% before -30% within 10 min, counted from the price DELAY s after the
moment (when a paper buy lands), not from the moment itself. Walk-forward: train Oct 3-4 -> grade Oct 5;
train Oct 3-5 -> grade Oct 6. Trades simulated with the buy and every sell landing DELAY s late."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import asyncio, bisect, gc, inspect, json, os, pickle, statistics as st, sys, time
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper import predictor as pm
from meme_trader.sniper.sweep import load_events
from meme_trader.sniper.feeds import FileFeed
from meme_trader.sniper.events import Launch, Trade, Migration
from meme_trader.sniper.tracker import TokenState
import numpy as np, lightgbm as lgb
S = OUT
D = DATA + ""
P = config.load(config.ROOT / "config/params.yaml")
DELAY = 2.5
CPS = [300, 600, 900, 1200, 1800]     # coins 5-30 minutes old
HOR = 1800                           # label: +100% before -30% within 30 min of the late fill
ex = P.sniper.execution
COST = 2 * (ex.curve_fee_pct + ex.platform_fee_pct + ex.paper_latency_slippage_pct) / 100

src = inspect.getsource(pm.build_dataset)
def sub(a, b):
    global src
    assert src.count(a) == 1, a
    src = src.replace(a, b)
sub("def build_dataset(", "def build_dataset_late(")
sub("open_labels[mint].append([len(y) - 1, s.curve.price, ts + horizon])",
    "open_labels[mint].append([len(y) - 1, None, ts + DELAY + horizon, ts + DELAY, s.curve.price])")
sub("""        for row, p0, deadline in open_labels.get(mint, []):
            if ts > deadline:""", """        for row, p0, deadline, start, lastp in open_labels.get(mint, []):
            if p0 is None:
                if ts < start:
                    keep.append([row, None, deadline, start, price if price is not None else lastp])
                    continue
                p0 = lastp                                 # the price when the buy lands
            if ts > deadline:""")
sub("                keep.append([row, p0, deadline])", "                keep.append([row, p0, deadline, start, lastp])")
sub("for row, _, deadline in rows:", "for row, _, deadline, *_rest in rows:")
ns = dict(vars(pm)); ns["DELAY"] = DELAY; exec(src, ns); build = ns["build_dataset_late"]

def ds(name, files):
    f = S + name + ".pkl"
    if os.path.exists(f):
        return pickle.load(open(f, "rb"))
    ev = load_events({"files": [D + x for x in files]})
    out = build(ev, P, checkpoints=CPS, horizon_s=HOR)
    del ev; gc.collect()
    pickle.dump(out, open(f, "wb"))
    return out

def at(path, t):
    i = bisect.bisect_right([x[0] for x in path], t) - 1
    return path[max(i, 0)][1]

def run(path, tp, sl, T, arm, trail):
    t0_ = path[0][0]
    p0 = at(path, t0_ + DELAY)
    peak = p0
    for t, px in path[1:]:
        if t < t0_ + DELAY:
            continue
        if t - t0_ > T:
            return at(path, t0_ + T + DELAY) / p0 - 1 - COST
        g = px / p0 - 1
        peak = max(peak, px)
        if (tp is not None and g >= tp) or (sl is not None and g <= -sl) or \
                (trail is not None and peak / p0 - 1 >= arm and px <= peak * (1 - trail)):
            return at(path, t + DELAY) / p0 - 1 - COST
    return path[-1][1] / p0 - 1 - COST

FIXED = {"S1: stop 30, trail 25 after +30, out 30m": (None, 0.3, 1800, 0.3, 0.25),
         "S2: stop 40, trail 30 after +50, out 60m": (None, 0.4, 3600, 0.5, 0.3),
         "S3: stop 20, trail 20 after +20, out 30m": (None, 0.2, 1800, 0.2, 0.2),
         "S4: +50% / -25% / 30m": (0.5, 0.25, 1800, 0, None),
         "bracket +100/-30/30m": (1.0, 0.3, 1800, 0, None)}
CFG = {"objective": "binary", "metric": "auc", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 200,
       "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "lambda_l2": 1.0, "verbose": -1,
       "num_threads": 4, "seed": 7}

def fold(train_name, train_files, test_name, test_file, out):
    t0 = time.time()
    Xtr, ytr, _ = ds(train_name, train_files)
    Xte, yte, mte = ds(test_name, [test_file])
    print(f"\n### {test_file}: train {len(ytr)} (base {sum(ytr)/len(ytr):.3f}) test {len(yte)} (base {sum(yte)/len(yte):.3f}) ({time.time()-t0:.0f}s)", flush=True)
    A, B = np.array(Xtr, dtype=np.float32), np.array(ytr, dtype=np.float32)
    cut = int(len(B) * .85)
    bst = lgb.train(CFG, lgb.Dataset(A[:cut], B[:cut]), 2000, valid_sets=[lgb.Dataset(A[cut:], B[cut:])],
                    callbacks=[lgb.early_stopping(100, verbose=False)])
    pg = bst.predict(np.array(Xte, dtype=np.float32), num_iteration=bst.best_iteration)
    print("AUC on the late label", round(pm.auc(yte, pg.tolist()), 4), f"({bst.best_iteration} trees)", flush=True)
    order = sorted(range(len(pg)), key=lambda i: mte[i][1])
    curve_at = {}
    def picks(thr):
        seen, o = set(), []
        for i in order:
            m, ts = mte[i][0], mte[i][1]
            if m not in seen and pg[i] >= thr:
                seen.add(m); o.append((m, ts)); curve_at[(m, ts)] = mte[i][2]
        return o
    sets = {f"slow>={t}": picks(t) for t in (0.1, 0.15, 0.2, 0.25, 0.3)}
    want = {}
    for ps in sets.values():
        for m, ts in ps:
            want.setdefault(m, set()).add(ts)
    print({k: len(v) for k, v in sets.items()}, flush=True)
    paths, tok = {}, {}
    async def collect():
        async for e in FileFeed(D + test_file).events():
            if isinstance(e, Launch):
                if e.mint in want and e.mint not in tok:
                    s = tok[e.mint] = TokenState(e.mint, e, e.ts); s.on_launch(e)
            elif isinstance(e, Trade) and e.mint in tok:
                s = tok[e.mint]
                for ts in want[e.mint]:
                    k = (e.mint, ts)
                    if e.ts >= ts and k not in paths and s.price_known:
                        paths[k] = [(ts, s.curve.price)]
                s.on_trade(e, P.sniper.entry.bundle_window_s, P.sniper.entry.sniper_window_s)
                for ts in want[e.mint]:
                    k = (e.mint, ts)
                    if k in paths and e.ts <= ts + 3600:
                        paths[k].append((e.ts, s.curve.price))
            elif isinstance(e, Migration) and e.mint in tok:
                tok[e.mint].migrated = True
    asyncio.run(collect())
    end = max(p[-1][0] for p in paths.values())
    for name, ps in sets.items():
        ks = [k for k in ps if k in paths and paths[k][0][0] + 3600 <= end]
        if len(ks) < 20:
            continue
        for fx, g in FIXED.items():
            r = [run(paths[k], *g) for k in ks]
            row = {"coins": len(r), "avg": round(100 * st.fmean(r), 1), "median": round(100 * st.median(r), 1),
                   "won": round(100 * sum(x > 0 for x in r) / len(r)),
                   "se": round(100 * st.pstdev(r) / len(r) ** .5, 1)}
            for lo, hi in ((0, 50), (50, 80), (80, 101)):
                rr = [x for k, x in zip(ks, r) if lo <= curve_at.get(k, 0) < hi]
                if len(rr) >= 15:
                    row[f"curve{lo}-{hi}"] = [len(rr), round(100 * st.fmean(rr), 1)]
            out[f"{test_file} | {name} | {fx}"] = row
            print(name, "|", fx, row, flush=True)
    return bst

out = {}
fold("slow_train34", ["feed-2026-10-03.jsonl.gz", "feed-2026-10-04.jsonl.gz"], "slow_test5", "feed-2026-10-05.jsonl.gz", out)
b = fold("slow_train345", ["feed-2026-10-03.jsonl.gz", "feed-2026-10-04.jsonl.gz", "feed-2026-10-05.jsonl.gz"],
         "slow_test6", "feed-2026-10-06.jsonl.gz", out)
b.save_model(S + "gbm_slow.txt")
json.dump(out, open(S + "slow.json", "w"), indent=1)
print("done")
