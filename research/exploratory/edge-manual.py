"""The owner's manual entries vs the bots' graduation entries: what the coin looked like at the decision, and what it
did after the exit. From the recordings (Oct 4-6)."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import asyncio, bisect, glob, json, statistics as st, sys, time
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper.feeds import FileFeed
from meme_trader.sniper.events import Launch, Trade, Migration
from meme_trader.sniper.tracker import TokenState
D = DATA + ""
P = config.load(config.ROOT / "config/params.yaml")
rows = []
for f in sorted(glob.glob(D + "trades-2026-10-0[456].jsonl")):
    for l in open(f):
        r = json.loads(l)
        if r.get("mode") == "paper" and r.get("source") in ("manual", "late", "sniper"):
            r["decide"] = r["opened"] - (r.get("entry_delay_s") or 0)
            rows.append(r)
mints = {r["mint"] for r in rows}
by_mint = {}
for r in rows:
    by_mint.setdefault(r["mint"], []).append(r)
tok, snaps, path = {}, {}, {}
async def go():
    for f in ("feed-2026-10-04.jsonl.gz", "feed-2026-10-05.jsonl.gz", "feed-2026-10-06.jsonl.gz"):
        async for e in FileFeed(D + f).events():
            m = getattr(e, "mint", None)
            if m not in mints:
                continue
            if isinstance(e, Launch) and m not in tok:
                s = tok[m] = TokenState(m, e, e.ts); s.on_launch(e)
                continue
            if isinstance(e, Migration) and m in tok:
                tok[m].migrated = True
                continue
            if not isinstance(e, Trade):
                continue
            if m not in tok:
                tok[m] = TokenState(m, None, e.ts)
            s = tok[m]
            for r in by_mint[m]:                       # snapshot each entry at its decision moment
                k = (m, r["decide"])
                if k not in snaps and e.ts >= r["decide"] and s.price_known:
                    w60 = s.window(r["decide"], 60)
                    px = [t[1] for t in s.trades]
                    tt = [t[0] for t in s.trades]
                    def ago(sec):
                        i = bisect.bisect_right(tt, r["decide"] - sec) - 1
                        return px[i] if i >= 0 else None
                    p60, p300 = ago(60), ago(300)
                    snaps[k] = {"age_min": round((r["decide"] - s.created_ts) / 60, 1) if s.launch else None,
                                "curve_pct": round(s.curve.progress * 100), "mcap_sol": round(s.market_cap_sol),
                                "migrated": s.migrated, "buyers_60s": len({t[4] for t in w60 if t[2] == "buy"}),
                                "chg_60s": round((s.curve.price / p60 - 1) * 100) if p60 else None,
                                "chg_5m": round((s.curve.price / p300 - 1) * 100) if p300 else None,
                                "off_high_pct": round((1 - s.curve.price / s.peak_price) * 100) if s.peak_price else None,
                                "holders": len([v for v in s.holders.values() if v > 0])}
            s.on_trade(e, P.sniper.entry.bundle_window_s, P.sniper.entry.sniper_window_s)
            for r in by_mint[m]:
                if r["closed"] <= e.ts <= r["closed"] + 1800:
                    path.setdefault((m, r["closed"]), []).append(s.curve.price)
                if (m, r["closed"], "px") not in path and e.ts >= r["closed"]:
                    path[(m, r["closed"], "px")] = s.curve.price
asyncio.run(go())
out = []
for r in rows:
    sn = snaps.get((r["mint"], r["decide"]))
    after = path.get((r["mint"], r["closed"]), [])
    px = path.get((r["mint"], r["closed"], "px"))
    a = {"max_after_pct": round((max(after) / px - 1) * 100) if after and px else None,
         "end_after_pct": round((after[-1] / px - 1) * 100) if after and px else None}
    out.append({"src": r["source"], "sym": r["symbol"], "ts": r["opened"], "pnl_pct": round(r["pnl_pct"], 1),
                "held_s": round(r["closed"] - r["opened"]), "peak": round(r.get("peak_gain_pct") or 0), **(sn or {}), **a})
json.dump(out, open(OUT + "manual.json", "w"))
real = [o for o in out if o["src"] == "manual" and o["ts"] >= time.mktime((2026, 10, 4, 20, 0, 0, 0, 0, -1))]
def summ(name, xs):
    def med(k):
        v = [x[k] for x in xs if x.get(k) is not None]
        return round(st.median(v), 1) if v else None
    print(f"{name:22s} n={len(xs):3d} pnl med {med('pnl_pct')} | age_min {med('age_min')} curve {med('curve_pct')} mcap {med('mcap_sol')} "
          f"buyers60 {med('buyers_60s')} chg60 {med('chg_60s')} chg5m {med('chg_5m')} off_high {med('off_high_pct')} holders {med('holders')} "
          f"held {med('held_s')} | migrated {sum(1 for x in xs if x.get('migrated'))} | after exit: max {med('max_after_pct')} end {med('end_after_pct')}")
summ("you (29)", real)
summ("  your winners", [x for x in real if x["pnl_pct"] > 0])
summ("  your losers", [x for x in real if x["pnl_pct"] <= 0])
for src in ("late", "sniper"):
    xs = [o for o in out if o["src"] == src]
    summ(f"bots: {src}", xs)
    summ(f"  {src} winners", [x for x in xs if x["pnl_pct"] > 0])
print("\nyour 29, one per line:")
for x in sorted(real, key=lambda x: x["ts"]):
    print(f'{x["sym"][:10]:10s} {x["pnl_pct"]:+7.1f}% held {x["held_s"]:5d}s peak {x["peak"]:+5d} | age {x.get("age_min")} curve {x.get("curve_pct")} mcap {x.get("mcap_sol")} mig {x.get("migrated")} b60 {x.get("buyers_60s")} chg60 {x.get("chg_60s")} chg5m {x.get("chg_5m")} offhigh {x.get("off_high_pct")} | after: max {x.get("max_after_pct")} end {x.get("end_after_pct")}')
