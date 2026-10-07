"""The owner's style as a rule, on one recorded day: half-full curve, heavy buying right now, bought on a dip (not at the
top). Every 2 s per coin in the curve band, the moment is logged; afterwards each rule in a grid takes the first matching
moment per coin and is traded with the buy and every sell landing 2.5 s late, after fees."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import asyncio, bisect, itertools, json, statistics as st, sys
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper.feeds import FileFeed
from meme_trader.sniper.events import Launch, Trade, Migration
from meme_trader.sniper.tracker import TokenState
D = DATA + ""
S = OUT
P = config.load(config.ROOT / "config/params.yaml")
DAY = sys.argv[1]
DELAY = 2.5
ex = P.sniper.execution
COST = 2 * (ex.curve_fee_pct + ex.platform_fee_pct + ex.paper_latency_slippage_pct) / 100
en = P.sniper.entry
tok, last_check, rows, rec, path = {}, {}, [], {}, {}
async def go():
    n = 0
    async for e in FileFeed(D + DAY).events():
        n += 1
        if n % 200000 == 0:                                   # forget coins long past
            for m in [m for m, s in tok.items() if e.ts - s.created_ts > 2700 and rec.get(m, 0) < e.ts]:
                del tok[m]
        if isinstance(e, Launch):
            if e.mint not in tok:
                s = tok[e.mint] = TokenState(e.mint, e, e.ts); s.on_launch(e)
            continue
        if isinstance(e, Migration):
            if e.mint in tok:
                tok[e.mint].migrated = True
            continue
        if not isinstance(e, Trade) or e.mint not in tok:
            continue
        s = tok[e.mint]
        s.on_trade(e, en.bundle_window_s, en.sniper_window_s)
        m, now = e.mint, e.ts
        if m in rec and now <= rec[m]:
            path[m].append((now, s.curve.price))
        if s.migrated or not s.price_known or now - last_check.get(m, -9) < 2:
            continue
        cp = s.curve.progress * 100
        if not 40 <= cp <= 80:
            continue
        last_check[m] = now
        w = s.window(now, 60)
        buyers = len({t[4] for t in w if t[2] == "buy"})
        p0 = None
        for t in reversed(s.trades):
            if t[0] <= now - 60:
                p0 = t[1]; break
        if p0 is None and s.launch is not None:
            p0 = s.launch.v_sol / s.launch.v_tokens if s.launch.v_tokens else None
        px = s.curve.price
        rows.append((m, now, cp, buyers, (px / p0 - 1) if p0 else 0.0, 1 - px / s.peak_price if s.peak_price else 0.0,
                     len([v for v in s.holders.values() if v > 0])))
        if m not in rec:
            path[m] = [(now, px)]
        rec[m] = now + 900
asyncio.run(go())
end = max((p[-1][0] for p in path.values()), default=0)

def at(pth, t):
    i = bisect.bisect_right([x[0] for x in pth], t) - 1
    return pth[max(i, 0)][1]

def run(pth, t0, tp, sl, T, arm, trail):
    """decided at t0; the buy lands DELAY s later; each sell lands DELAY s after its trigger"""
    seg = [x for x in pth if x[0] >= t0]
    p0 = at(pth, t0 + DELAY)
    peak = p0
    for t, px in seg:
        if t < t0 + DELAY:
            continue
        if t - t0 > T:
            return at(pth, t0 + T + DELAY) / p0 - 1 - COST
        g = px / p0 - 1
        peak = max(peak, px)
        if (tp is not None and g >= tp) or (sl is not None and g <= -sl) or \
                (trail is not None and peak / p0 - 1 >= arm and px <= peak * (1 - trail)):
            return at(pth, t + DELAY) / p0 - 1 - COST
    return seg[-1][1] / p0 - 1 - COST if seg else -COST

EXITS = {"E1 +40/-25/2m": (0.4, 0.25, 120, 0, None), "E2 trail20@+20,stop30,3m": (None, 0.3, 180, 0.2, 0.2),
         "E3 +30/-20/90s": (0.3, 0.2, 90, 0, None), "E4 +50/-30/5m": (0.5, 0.3, 300, 0, None),
         "E5 trail25@+30,stop30,10m": (None, 0.3, 600, 0.3, 0.25)}
GRID = {"buyers": (30, 50, 80), "dip": ((0.0, 0.03), (0.05, 0.25), (0.10, 0.35), (0.15, 0.45)), "chg60": (0.0, 0.2, 0.5),
        "curve": ((40, 60), (50, 70), (60, 80))}
rows.sort(key=lambda r: r[1])
out = {}
for b, dip, c60, cv in itertools.product(*GRID.values()):
    seen, ent = set(), []
    for m, now, cp, buyers, chg, off, hold in rows:
        if m in seen or buyers < b or not dip[0] <= off <= dip[1] or chg < c60 or not cv[0] <= cp <= cv[1]:
            continue
        if now + 900 > end:
            continue
        seen.add(m); ent.append((m, now))
    if len(ent) < 10:
        continue
    for xn, g in EXITS.items():
        r = [run(path[m], t, *g) for m, t in ent]
        out[f"b{b} dip{dip[0]:.2f}-{dip[1]:.2f} chg60>={c60} curve{cv[0]}-{cv[1]} | {xn}"] = \
            [len(r), round(100 * st.fmean(r), 2), round(100 * st.median(r), 1), round(100 * sum(x > 0 for x in r) / len(r))]
json.dump(out, open(S + f"style-{DAY[5:15]}.json", "w"))
print(DAY, "rows", len(rows), "rules", len(out), "done")
