"""The biggest winners of 'jumped 40%+ in 5 min on 4x volume, hold 2 h, filled 60 s late': real moves or bad prints?"""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import bisect, gzip, json, statistics as st
from collections import defaultdict
W = DATA + "wallets/"
days = {"10-04": "trades-2026-10-04.jsonl.gz", "10-05": "trades-2026-10-05.jsonl.gz", "10-06": "trades-2026-10-06.jsonl.gz"}
DELAY, COST = 60.0, 0.012
out = []
for d, f in days.items():
    pools, sym = defaultdict(list), {}
    op = gzip.open if f.endswith(".gz") else open
    with op(W + f, "rt") as fh:
        for line in fh:
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("px") and r.get("sol"):
                pools[r["pool"]].append((r["t"], r["px"], r["side"] == "buy", r["sol"], r.get("usd") or 0)); sym[r["pool"]] = (r.get("sym"), r.get("mint"))
    for pool, p in pools.items():
        p.sort(); ts = [x[0] for x in p]; cum = [0.0]
        for x in p: cum.append(cum[-1] + x[3])
        last, lastsig = -1e18, -1e18
        for i, (t, px, buy, sol, usd) in enumerate(p):
            if t - last < 30 or t - p[0][0] < 3900: continue
            last = t
            j5 = bisect.bisect_left(ts, t - 300); j65 = bisect.bisect_left(ts, t - 3900)
            if j5 == 0: continue
            v5 = cum[i + 1] - cum[j5]; v60 = cum[j5] - cum[j65]
            surge = v5 / (v60 / 12) if v60 > 0 else 0
            if px / p[j5 - 1][1] - 1 >= 0.40 and surge >= 4 and t - lastsig >= 7200:
                lastsig = t
                k = bisect.bisect_left(ts, t + DELAY)
                if k >= len(p): continue
                te, p0 = p[k][0], p[k][1]
                kx = bisect.bisect_left(ts, te + 7200)
                seg = p[k:kx + 1]
                xk = bisect.bisect_left(ts, te + 7200 + DELAY)
                px_out = p[xk][1] if xk < len(p) else p[-1][1]
                prices = [x[1] for x in seg]
                usd_vol = sum(x[4] for x in seg)
                out.append({"day": d, "sym": sym[pool][0], "mint": sym[pool][1], "pnl": px_out / p0 - 1 - COST,
                            "trades_in_hold": len(seg), "usd_volume_in_hold": round(usd_vol), "max_in_hold": max(prices) / p0 - 1,
                            "median_trade_usd": round(st.median([x[4] for x in seg]), 1) if seg else 0,
                            "exit_trade_usd": round(p[xk][4] if xk < len(p) else p[-1][4], 1)})
out.sort(key=lambda r: -r["pnl"])
print("trades:", len(out), "avg", round(100 * st.fmean(r["pnl"] for r in out), 1), "%")
for r in out[:8]:
    print(f'{r["day"]} {str(r["sym"])[:10]:10s} pnl {100*r["pnl"]:+7.0f}%  peak {100*r["max_in_hold"]:+7.0f}%  trades in 2h {r["trades_in_hold"]:5d}  '
          f'volume ${r["usd_volume_in_hold"]:>9,}  median trade ${r["median_trade_usd"]}  exit trade ${r["exit_trade_usd"]}  {r["mint"]}')
