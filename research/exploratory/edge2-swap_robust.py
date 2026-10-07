"""Established pump.fun coins on PumpSwap (2+ days old, $5k+ liquidity; the wallet study's recording): do simple rules
make money where fees are ~0.3% a side and minutes, not seconds, decide? Each rule is judged day by day (Oct 4, 5, 6).
Entries and exits fill at the first trade at least DELAY s after the signal; costs: fee + impact per side."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import bisect, gzip, itertools, json, statistics as st, sys
from collections import defaultdict
W = DATA + "wallets/"
COST = float(sys.argv[1]) if len(sys.argv) > 1 else 0.012        # round trip: 0.3% fee + ~0.3% impact, per side
DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
days = {"10-04": "trades-2026-10-04.jsonl.gz", "10-05": "trades-2026-10-05.jsonl.gz", "10-06": "trades-2026-10-06.jsonl.gz"}
series = {}
for d, f in days.items():
    pools = defaultdict(list)
    op = gzip.open if f.endswith(".gz") else open
    with op(W + f, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("px") and r.get("sol"):
                pools[r["pool"]].append((r["t"], r["px"], r["side"] == "buy", r["sol"]))
    for p in pools.values():
        p.sort()
    series[d] = pools
    print(d, "pools", len(pools), "trades", sum(len(v) for v in pools.values()), flush=True)

def signals(p):
    """every 30 s of a pool's trading: (t, ret 5 min, ret 15 min, volume surge, buy share 5 min)"""
    ts = [x[0] for x in p]
    cum = [0.0]
    for x in p:
        cum.append(cum[-1] + x[3])
    out, last = [], -1e18
    for i, (t, px, buy, sol) in enumerate(p):
        if t - last < 30 or t - p[0][0] < 3900:
            continue
        last = t
        j5 = bisect.bisect_left(ts, t - 300); j15 = bisect.bisect_left(ts, t - 900); j65 = bisect.bisect_left(ts, t - 3900)
        if j5 == 0 or j15 == 0:
            continue
        px5, px15 = p[j5 - 1][1], p[j15 - 1][1]
        v5 = cum[i + 1] - cum[j5]
        v60 = cum[j5] - cum[j65]
        surge = v5 / (v60 / 12) if v60 > 0 else 0.0
        b5 = sum(x[3] for x in p[j5:i + 1] if x[2]) / v5 if v5 > 0 else 0.5
        out.append((t, px / px5 - 1, px / px15 - 1, surge, b5))
    return out

def fill(p, ts, t):
    i = bisect.bisect_left(ts, t)
    return (p[i][0], p[i][1]) if i < len(p) else None

def run(p, ts, t0, tp, sl, T, arm, trail):
    e = fill(p, ts, t0 + DELAY)
    if not e:
        return None
    te, p0 = e
    peak = p0
    for t, px, _, _ in p[bisect.bisect_left(ts, te):]:
        g = px / p0 - 1
        peak = max(peak, px)
        hit = t - te >= T or (tp is not None and g >= tp) or (sl is not None and g <= -sl) or \
              (trail is not None and peak / p0 - 1 >= arm and px <= peak * (1 - trail))
        if hit:
            x = fill(p, ts, t + DELAY)
            return (x[1] if x else px) / p0 - 1 - COST
    return p[-1][1] / p0 - 1 - COST

SIG = {d: {pool: signals(p) for pool, p in pools.items()} for d, pools in series.items()}
TS = {d: {pool: [x[0] for x in p] for pool, p in pools.items()} for d, pools in series.items()}
RULES = {}
for r5, sg, bs in itertools.product((0.15, 0.20, 0.30, 0.40), (3, 4, 6), (0.0,)):
    RULES[f"momentum: +{r5:.0%} in 5 min, volume x{sg}, buys>={bs:.0%}"] = lambda s, r5=r5, sg=sg, bs=bs: s[1] >= r5 and s[3] >= sg and s[4] >= bs
EXITS = {"hold 30m": (None, None, 1800, 0, None), "hold 60m": (None, None, 3600, 0, None),
         "hold 120m": (None, None, 7200, 0, None), "stop 30, hold 60m": (None, 0.3, 3600, 0, None),
         "trail 30 after +50, 120m": (None, None, 7200, 0.5, 0.3)}
res = []
for rn, rule in RULES.items():
    for xn, x in EXITS.items():
        per = {}
        for d in days:
            out = []
            for pool, sigs in SIG[d].items():
                p, ts = series[d][pool], TS[d][pool]
                last = -1e18
                for s in sigs:
                    if s[0] - last >= 7200 and rule(s):
                        last = s[0]
                        r = run(p, ts, s[0], *x)
                        if r is not None:
                            out.append(r)
            per[d] = out
        n = sum(len(v) for v in per.values())
        if min(len(v) for v in per.values()) < 15:
            continue
        allr = [r for v in per.values() for r in v]
        trim = sorted(allr)[:-3]
        res.append((rn, xn, n, st.fmean(allr), [round(100 * st.fmean(v), 1) for v in per.values()],
                    [len(v) for v in per.values()], st.median(allr), sum(r > 0 for r in allr) / len(allr), st.fmean(trim)))
res.sort(key=lambda r: -min(r[4]))
print(f"\nround-trip cost {COST:.1%}, fills {DELAY:.0f} s late; {len(res)} rule x exit combinations with 15+ trades every day")
print("share with a positive average:", f"{100 * sum(r[3] > 0 for r in res) / max(len(res), 1):.0f}%",
      "| positive on all 3 days:", sum(1 for r in res if min(r[4]) > 0))
print("\nbest by their WORST day:")
for r in res[:10]:
    print(f"  {r[0]:44s} | {r[1]:24s} n={r[2]:5d} avg {100*r[3]:+5.1f}% w/o best 3 {100*r[8]:+5.1f}% med {100*r[6]:+5.1f}% won {100*r[7]:3.0f}% | per day {r[4]} n {r[5]}")
