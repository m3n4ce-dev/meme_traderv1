"""Which half-full coins graduate? Train on earlier days, test on a later unseen one. Trades are replayed with the buy
and every sell landing 2.5 s late, 3.5% fees round trip."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import bisect, json, pickle, statistics as st, sys
import numpy as np, lightgbm as lgb
sys.path.insert(0, ROOT)
from meme_trader.sniper.predictor import auc
E = OUT
DELAY, COST = 2.5, 0.035
days = {d: pickle.load(open(E + f"grad-2026-10-0{d}.pkl", "rb")) for d in (3, 4, 5, 6)}

def at(p, t):
    ts = [x[0] for x in p]
    i = bisect.bisect_right(ts, t) - 1
    return p[max(i, 0)]

def trade(p, t0, kind):
    """p: [(ts, price, curve %, dev_sold)]. Decided at t0; buy lands +DELAY; each sell lands +DELAY after its trigger."""
    e = at(p, t0 + DELAY); p0, dev0 = e[1], e[3]
    if p0 <= 0:
        return None
    peak, last_high = p0, t0 + DELAY
    T = 600 if kind == "bot" else 1800
    for ts, px, cp, dev in p:
        if ts < t0 + DELAY:
            continue
        g = px / p0 - 1
        if px > peak:
            peak, last_high = px, ts
        why = None
        if cp >= 94: why = "graduation"
        elif kind == "bot" and dev > dev0: why = "dev sold"
        elif g <= (-0.15 if kind != "hold" else -0.25): why = "stop"
        elif kind == "bot" and ts - last_high >= 45 and ts - (t0 + DELAY) >= 45: why = "stall"
        elif kind == "run" and peak / p0 - 1 >= 0.3 and px <= peak * 0.75: why = "trail"
        elif ts - t0 > T: why = "time"
        if why:
            return at(p, ts + DELAY)[1] / p0 - 1 - COST, why
    return p[-1][1] / p0 - 1 - COST, "end"

def label(p, t0):
    """1 = reaches 94% of the curve (or graduates) within 600 s of the late fill, before falling 15% from it"""
    p0 = at(p, t0 + DELAY)[1]
    for ts, px, cp, dev in p:
        if ts < t0 + DELAY: continue
        if ts - t0 > 600: return 0
        if cp >= 94: return 1
        if px <= p0 * 0.85: return 0
    return 0

def rows(d):
    D = days[d]; out = []
    for m, ts, lvl, f, rok in D["snaps"]:
        p = D["path"].get(m)
        if p and p[-1][0] >= ts + 60:
            out.append((m, ts, lvl, f, rok, label(p, ts)))
    return out

def summ(name, res):
    r = [x[0] for x in res if x]
    if len(r) < 8:
        print(f"   {name:38s} n={len(r)} (too few)"); return
    why = {}
    for x in res:
        if x: why[x[1]] = why.get(x[1], 0) + 1
    print(f"   {name:38s} n={len(r):4d} avg {100*st.fmean(r):+6.1f}% med {100*st.median(r):+6.1f}% won {100*sum(v>0 for v in r)/len(r):3.0f}%  "
          f"graduated {100*why.get('graduation',0)/len(r):3.0f}%")

CFG = {"objective": "binary", "metric": "auc", "learning_rate": 0.03, "num_leaves": 15, "min_data_in_leaf": 50,
       "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "lambda_l2": 1.0, "verbose": -1, "num_threads": 4, "seed": 7}
for train, test in (((3, 4), 5), ((3, 4, 5), 6)):
    tr = [r for d in train for r in rows(d)]
    te = rows(test)
    X = np.array([r[3] for r in tr], dtype=np.float32); y = np.array([r[5] for r in tr], dtype=np.float32)
    cut = int(len(y) * .85)
    b = lgb.train(CFG, lgb.Dataset(X[:cut], y[:cut]), 2000, valid_sets=[lgb.Dataset(X[cut:], y[cut:])],
                  callbacks=[lgb.early_stopping(100, verbose=False)])
    pt = b.predict(np.array([r[3] for r in te], dtype=np.float32), num_iteration=b.best_iteration)
    yt = [r[5] for r in te]
    print(f"\n### test Oct {test} (trained on Oct {train}): {len(te)} moments, {sum(yt)} reached 94% ({100*sum(yt)/max(len(yt),1):.1f}%), "
          f"AUC {auc(yt, pt.tolist()):.3f}, {b.best_iteration} trees")
    D = days[test]
    order = sorted(range(len(te)), key=lambda i: te[i][1])
    def first(pred):
        seen, out = set(), []
        for i in order:
            m = te[i][0]
            if m in seen or not pred(i): continue
            seen.add(m); out.append((m, te[i][1]))
        return out
    rule = [(m, v[0]) for m, v in D["rule"].items() if m in D["path"]]
    pr = b.predict(np.array([v[1] for m, v in D["rule"].items() if m in D["path"]], dtype=np.float32), num_iteration=b.best_iteration) if rule else []
    qs = {q: float(np.quantile(pt, q)) for q in (0.8, 0.9, 0.95, 0.98)}
    sets = {"every coin at 55%": first(lambda i: te[i][2] == 55),
            "today's graduation rule": rule}
    for q, thr in qs.items():
        sets[f"model top {100-int(q*100)}% (p>={thr:.2f})"] = first(lambda i, thr=thr: pt[i] >= thr)
    for q in (0.8, 0.9):
        thr = qs[q]
        sets[f"rule + model top {100-int(q*100)}%"] = [x for x, pp in zip(rule, pr) if pp >= thr]
    for kind in ("bot", "hold", "run"):
        print(f"  exits: {kind}  ({'stop 15, stall 45 s, dev sold, curve 94%, 10 min' if kind=='bot' else 'stop 25, curve 94%, 30 min' if kind=='hold' else 'stop 15, trail 25 after +30, curve 94%, 30 min'})")
        for name, picks in sets.items():
            summ(name, [trade(D["path"][m], t, kind) for m, t in picks])
