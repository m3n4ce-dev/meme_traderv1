"""Model picks on Oct 6 (unseen) + which exit makes them pay. Exits are chosen on the first half of the day and
checked on the second half, so a lucky grid point can't pass for an edge."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import json, pickle, sys, time, statistics as st, itertools
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper import predictor as pm
from meme_trader.sniper.feeds import FileFeed
from meme_trader.sniper.events import Launch, Trade, Migration
from meme_trader.sniper.tracker import TokenState
import asyncio, numpy as np, lightgbm as lgb
S = OUT
D = DATA + ""
P = config.load(config.ROOT / "config/params.yaml")
ex = P.sniper.execution
COST = 2 * (ex.curve_fee_pct + ex.platform_fee_pct + ex.paper_latency_slippage_pct) / 100
t0 = time.time()
Xtr, ytr, _, _ = pickle.load(open(S + "train.pkl", "rb"))
Xte, yte, mte, rte = pickle.load(open(S + "test.pkl", "rb"))
A, B = np.array(Xtr, dtype=np.float32), np.array(ytr, dtype=np.float32)
cut = int(len(B) * .85)
bst = lgb.train({"objective": "binary", "metric": "auc", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 200,
                 "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "lambda_l2": 1.0, "verbose": -1,
                 "num_threads": 4, "seed": 7},
                lgb.Dataset(A[:cut], B[:cut]), 2000, valid_sets=[lgb.Dataset(A[cut:], B[cut:])],
                callbacks=[lgb.early_stopping(100, verbose=False)])
bst.save_model(S + "gbm.txt")
pg = bst.predict(np.array(Xte, dtype=np.float32), num_iteration=bst.best_iteration)
pl = pm.LogisticModel.load(D + "model.json").predict_rows(Xte)

def picks(p, thr):
    seen, out = set(), []
    for i in sorted(range(len(p)), key=lambda i: mte[i][1]):
        m, ts = mte[i][0], mte[i][1]
        if m not in seen and p[i] >= thr:
            seen.add(m); out.append((m, ts))
    return out

SETS = {f"gbm>={t}": picks(pg, t) for t in (0.15, 0.2, 0.25)}
SETS.update({f"live>={t}": picks(pl, t) for t in (0.15, 0.25)})
want = {}
for name, ps in SETS.items():
    for m, ts in ps:
        want.setdefault(m, set()).add(ts)
print({k: len(v) for k, v in SETS.items()}, f"({time.time()-t0:.0f}s)", flush=True)

# price paths: every trade price for 30 min after each pick moment
H = 1800
paths = {}                               # (mint, ts) -> [(t, price)], first element = entry price at ts
tok = {}
async def collect():
    async for e in FileFeed(D + "feed-2026-10-06.jsonl.gz").events():
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

def run(path, tp, sl, T, arm, trail):
    """% after fees. tp/sl in fractions (None = off); trail from peak once up `arm`; out at T seconds."""
    t0_, p0 = path[0]
    peak = p0
    last = p0
    for t, px in path[1:]:
        if t - t0_ > T:
            return last / p0 - 1 - COST
        last = px
        g = px / p0 - 1
        if tp is not None and g >= tp:
            return tp - COST
        if sl is not None and g <= -sl:
            return g - COST
        peak = max(peak, px)
        if trail is not None and peak / p0 - 1 >= arm and px <= peak * (1 - trail):
            return px / p0 - 1 - COST
    return last / p0 - 1 - COST

GRID = [(tp, sl, T, arm, tr) for tp in (0.3, 0.5, 1.0, 2.0, None) for sl in (0.1, 0.15, 0.2, 0.3, 0.4)
        for T in (60, 180, 600, 1800) for arm, tr in ((0, None), (0.2, 0.15), (0.3, 0.25), (0.5, 0.3))]
mid = None
out = {"cost": COST}
for name, ps in SETS.items():
    ks = [k for k in ((m, ts) for m, ts in ps) if k in paths and paths[k][0][0] + 1800 <= END]
    ks.sort(key=lambda k: k[1])
    half = len(ks) // 2
    a, b = ks[:half], ks[half:]
    def score(keys, g):
        r = [run(paths[k], *g) for k in keys]
        return st.fmean(r), st.median(r), sum(x > 0 for x in r) / len(r)
    best = sorted(GRID, key=lambda g: -score(a, g)[0])[:5]
    base = (1.0, 0.3, 600, 0, None)
    res = {"coins": len(ks), "first_half": len(a), "second_half": len(b),
           "bracket_100_30_10m": {"first": [round(100 * x, 1) for x in score(a, base)], "second": [round(100 * x, 1) for x in score(b, base)]},
           "best_on_first_half": [{"exit": {"tp": g[0], "sl": g[1], "max_s": g[2], "trail_arm": g[3], "trail": g[4]},
                                   "first_avg_med_won": [round(100 * x, 1) for x in score(a, g)],
                                   "second_avg_med_won": [round(100 * x, 1) for x in score(b, g)]} for g in best]}
    out[name] = res
    print("\n", name, json.dumps(res, indent=None), flush=True)
json.dump(out, open(S + "exits.json", "w"), indent=1)
print(f"done {time.time()-t0:.0f}s")
