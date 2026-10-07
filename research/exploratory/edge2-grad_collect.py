"""Graduation runners: for one recorded day, every coin's moments as its curve first passes 45/55/65/75% (features +
whether today's graduation rule would buy it then), the rule's own first pick per coin (scanned every 2 s), and each
candidate coin's path (price, curve %, creator's sold tokens) for 30 min after, for exit replays."""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import asyncio, json, pickle, sys, time
from collections import defaultdict, deque
sys.path.insert(0, ROOT)
from meme_trader import config
from meme_trader.sniper.feeds import FileFeed
from meme_trader.sniper.events import Launch, Trade, Migration, Metadata
from meme_trader.sniper.tracker import TokenState
from meme_trader.sniper.features import FEATURES, extract
from meme_trader.sniper.strategy import evaluate_late_entry
D = DATA + ""
OUT = OUT
P = config.load(ROOT + "/config/params.yaml")
L, en = P.sniper.late, P.sniper.entry
DAY = sys.argv[1]
LEVELS = (45, 55, 65, 75)
tok, nxt, last_scan = {}, {}, {}
creators, symbols = defaultdict(deque), defaultdict(deque)
snaps, rule_pick, rec, path = [], {}, {}, {}

def ctx(s, now):
    c = creators[s.creator]
    while c and c[0] < now - 86400: c.popleft()
    y = symbols[s.symbol.upper()]
    while y and y[0] < now - 3600: y.popleft()
    return {"creator_launches": len(c), "symbol_dupes": max(len(y) - 1, 0)}

def rule_ok(s, now, cx):
    ok, _ = evaluate_late_entry(s, now, L, {"max_bundle_pct": en.max_bundle_pct, "max_early_sold_ratio": en.max_early_sold_ratio,
                                            "creator_launches": cx["creator_launches"],
                                            "max_creator_launches_24h": en.max_creator_launches_24h,
                                            "max_cluster_pct": en.funding.max_cluster_pct})
    return ok

async def go():
    n = 0
    async for e in FileFeed(D + DAY).events():
        n += 1
        if n % 300000 == 0:
            for m in [m for m, s in tok.items() if e.ts - s.created_ts > 5400 and rec.get(m, 0) < e.ts]:
                del tok[m]
        if isinstance(e, (Metadata,)) or (isinstance(e, Launch) and e.mint in tok):
            s = tok.get(e.mint)
            if s is not None and s.launch is not None:
                s.launch.twitter, s.launch.telegram, s.launch.website = e.twitter, e.telegram, e.website
            continue
        if isinstance(e, Launch):
            s = tok[e.mint] = TokenState(e.mint, e, e.ts); s.on_launch(e)
            creators[e.creator].append(e.ts); symbols[e.symbol.upper()].append(e.ts)
            nxt[e.mint] = 0
            continue
        if isinstance(e, Migration):
            if e.mint in tok:
                tok[e.mint].migrated = True
                if e.mint in rec and e.ts <= rec[e.mint]:
                    path[e.mint].append((e.ts, tok[e.mint].curve.price, 100.0, tok[e.mint].dev_sold))
            continue
        if not isinstance(e, Trade) or e.mint not in tok:
            continue
        s = tok[e.mint]
        s.on_trade(e, en.bundle_window_s, en.sniper_window_s)
        m, now = e.mint, e.ts
        cp = s.curve.progress * 100
        if m in rec and now <= rec[m]:
            path[m].append((now, s.curve.price, cp, s.dev_sold))
        if s.migrated or not s.price_known or s.launch is None:
            continue
        i = nxt.get(m, 0)
        if i < len(LEVELS) and cp >= LEVELS[i]:
            while i < len(LEVELS) and cp >= LEVELS[i]:
                i += 1
            nxt[m] = i
            cx = ctx(s, now)
            f = extract(s, now, cx)
            snaps.append((m, now, LEVELS[i - 1], [f[k] for k in FEATURES], rule_ok(s, now, cx)))
            if m not in rec:
                path[m] = [(now, s.curve.price, cp, s.dev_sold)]
            rec[m] = now + 1800
        if m not in rule_pick and L.min_curve_pct <= cp <= L.max_curve_pct and now - last_scan.get(m, -9) >= L.get("scan_interval_s", 2):
            last_scan[m] = now
            cx = ctx(s, now)
            if rule_ok(s, now, cx):
                rule_pick[m] = (now, [extract(s, now, cx)[k] for k in FEATURES])
                if m not in rec:
                    path[m] = [(now, s.curve.price, cp, s.dev_sold)]
                rec[m] = max(rec.get(m, 0), now + 1800)
asyncio.run(go())
pickle.dump({"snaps": snaps, "rule": rule_pick, "path": path, "dev_at": None}, open(OUT + f"grad-{DAY[5:15]}.pkl", "wb"))
print(DAY, "snapshots", len(snaps), "rule picks", len(rule_pick), "coins with paths", len(path))
