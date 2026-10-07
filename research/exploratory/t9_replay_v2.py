"""T9 replay, version 2 (2026-10-07, after a sixth review found the first version inventing exits).

Established pump.fun coins on PumpSwap (the wallet recorder's universe: 2+ days old, $5k+ liquidity), 10-04..10-06,
read from the sealed recordings as ONE series per pool, so a position open at midnight gets the next day's first
eligible exit. Fills and exits go through meme_trader/sniper/replay_exec.py, the same rules as the forward test:
a fill is a recorded trade at or after its time, inside what the recording observed; otherwise the trade is
`censored` (pool quiet), `pending` (the recording ended) or `execution_unmeasured` (a recording gap), never a price.

    python research/exploratory/t9_replay_v2.py                 # writes $MT_EXPLORE_DIR/t9_replay_v2*.json[l]

Signals as in the first version: checked every 30 s of a pool's trading once it has 65 min of history (in the
recording, not per day); the 5-min return is against the last price at or before t-300; the volume surge is the last
5 minutes' SOL volume against the preceding 60 minutes' (t-3900..t-300) divided by 12. One signal per pool and rule
per 2 h. Returns are per trade, before cost; costs are applied in the summary. Every number is exploratory.
"""
from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import bisect, gzip, hashlib, itertools, json, statistics as st, sys, time  # noqa: E401
from collections import defaultdict
sys.path.insert(0, ROOT)
from meme_trader.sniper.replay_exec import CLOSED, Coverage, simulate  # noqa: E402

W = DATA + "wallets/"
FILES = ["trades-2026-10-04.jsonl.gz", "trades-2026-10-05.jsonl.gz", "trades-2026-10-06.jsonl.gz"]
GAP_S = 120                      # no trade in ANY pool this long: the recorder wasn't observing
DELAYS = (5, 30, 60)
COSTS = (0.012, 0.024)
EPISODE_S = 6 * 3600             # a pool's signals closer than this belong to one revival episode

t_start = time.time()
pools = defaultdict(list)
inputs, all_t = [], []
for f in FILES:
    h, n = hashlib.sha256(), 0
    with open(W + f, "rb") as raw:
        for b in iter(lambda: raw.read(1 << 23), b""):
            h.update(b)
    with gzip.open(W + f, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("px") and r.get("sol"):
                pools[r["pool"]].append((r["t"], r["px"], r["side"] == "buy", r["sol"]))
                n += 1
    inputs.append({"file": f, "sha256": h.hexdigest(), "trades": n})
    print(f, n, "trades", flush=True)
for p in pools.values():
    p.sort()
TS = {k: [x[0] for x in p] for k, p in pools.items()}
PX = {k: [x[1] for x in p] for k, p in pools.items()}
every = sorted(t for v in TS.values() for t in v)
gaps = [(a, b) for a, b in zip(every, every[1:]) if b - a > GAP_S]
try:
    for line in open(W + "pauses.jsonl"):
        g = json.loads(line)
        gaps.append((g["start"], g["end"]))
except OSError:
    pass
gaps.sort()
COV = Coverage(every[0], every[-1], gaps)
del every
print("coverage", time.strftime("%m-%d %H:%M", time.gmtime(COV.start)), "->", time.strftime("%m-%d %H:%M", time.gmtime(COV.end)),
      "| gaps", len(gaps), "totalling", round(sum(b - a for a, b in gaps) / 60), "min", flush=True)


def signals(p):
    ts = [x[0] for x in p]
    cum = [0.0]
    for x in p:
        cum.append(cum[-1] + x[3])
    out, last = [], -1e18
    for i, (t, px, buy, sol) in enumerate(p):
        if t - last < 30 or t - p[0][0] < 3900:
            continue
        last = t
        j5 = bisect.bisect_left(ts, t - 300)
        j15 = bisect.bisect_left(ts, t - 900)
        j65 = bisect.bisect_left(ts, t - 3900)
        if j5 == 0 or j15 == 0:
            continue
        v5 = cum[i + 1] - cum[j5]
        v60 = cum[j5] - cum[j65]
        out.append((t, px / p[j5 - 1][1] - 1, px / p[j15 - 1][1] - 1, v5 / (v60 / 12) if v60 > 0 else 0.0))
    return out


SIG = {k: signals(p) for k, p in pools.items()}
print("signals computed", round(time.time() - t_start), "s", flush=True)

RULES = {f"momentum: +{r5:.0%} in 5 min, volume x{sg}": (lambda s, r5=r5, sg=sg: s[1] >= r5 and s[3] >= sg)
         for r5, sg in itertools.product((0.15, 0.20, 0.30, 0.40), (3, 4, 6))}
RULES.update({f"dip: {r15:.0%} in 15 min, volume x{sg}": (lambda s, r15=r15, sg=sg: s[2] <= r15 and s[3] >= sg)
              for r15, sg in itertools.product((-0.15, -0.25, -0.40), (0, 2))})
EXITS = {"hold 30m": dict(hold=1800), "hold 60m": dict(hold=3600), "hold 120m": dict(hold=7200),
         "stop 30, hold 60m": dict(hold=3600, stop=0.3), "trail 30 after +50, 120m": dict(hold=7200, trail=(0.5, 0.3)),
         "+20/-10/30m": dict(hold=1800, take=0.2, stop=0.1), "+50/-20/60m": dict(hold=3600, take=0.5, stop=0.2)}

rows = []
for rn, rule in RULES.items():
    for pool, sigs in SIG.items():
        last = -1e18
        for s in sigs:
            if s[0] - last < 7200 or not rule(s):
                continue
            last = s[0]
            for d in DELAYS:
                for xn, x in EXITS.items():
                    o = simulate(TS[pool], PX[pool], s[0], d, x["hold"], 0.0, COV, stop=x.get("stop"),
                                 take=x.get("take"), trail=x.get("trail"))
                    rows.append({"rule": rn, "exit": xn, "delay": d, "pool": pool, "signal_t": s[0],
                                 "signal_day": time.strftime("%Y-%m-%d", time.gmtime(s[0])), "ret5": round(s[1], 4),
                                 "surge": round(s[3], 2), "status": o.status, "entry_t": o.entry_t, "entry_px": o.entry_px,
                                 "exit_why": o.exit_why, "exit_trigger_t": o.exit_trigger_t, "exit_t": o.exit_t,
                                 "exit_px": o.exit_px, "ret_gross": o.ret, "mark_gross": o.mark_ret, "why": o.why})
print("simulated", len(rows), "rule x exit x delay outcomes", round(time.time() - t_start), "s", flush=True)

# episodes: a pool's signals (any rule) closer than EPISODE_S apart are one revival
ep_of, sig_ts = {}, defaultdict(set)
for r in rows:
    sig_ts[r["pool"]].add(r["signal_t"])
for pool, tset in sig_ts.items():
    ts = sorted(tset)
    k, prev = 0, None
    for t in ts:
        if prev is not None and t - prev > EPISODE_S:
            k += 1
        ep_of[(pool, t)] = f"{pool[:8]}#{k}"
        prev = t
for r in rows:
    r["episode"] = ep_of[(r["pool"], r["signal_t"])]


def summary(group, cost):
    closed = [r for r in group if r["status"] == CLOSED]
    rets = [r["ret_gross"] - cost for r in closed]
    by_ep = defaultdict(float)
    for r, v in zip(closed, rets):
        by_ep[r["episode"]] += v
    by_day = defaultdict(list)
    for r, v in zip(closed, rets):
        by_day[r["signal_day"]].append(v)
    tot = sum(rets)
    eps = sorted(by_ep.values(), reverse=True)
    status = defaultdict(int)
    for r in group:
        status[r["status"]] += 1
    unm = [r for r in group if r["status"] != CLOSED and r["entry_t"] is not None]
    at_mark = sum((r["mark_gross"] if r["mark_gross"] is not None else -1.0) - cost for r in unm)
    return {"signals": len(group), "status": dict(status), "closed": len(closed),
            "coverage": round(len(closed) / len(group), 3) if group else None,
            "mean": round(st.fmean(rets), 4) if rets else None, "median": round(st.median(rets), 4) if rets else None,
            "sum": round(tot, 3), "won": round(sum(v > 0 for v in rets) / len(rets), 3) if rets else None,
            "episodes": len(by_ep), "top_episode_share": round(eps[0] / tot, 2) if tot > 0 and eps else None,
            "top3_episode_share": round(sum(eps[:3]) / tot, 2) if tot > 0 and eps else None,
            "sum_without_top_episode": round(tot - eps[0], 3) if eps else None,
            "sum_without_top3_episodes": round(tot - sum(eps[:3]), 3) if eps else None,
            "per_day_mean": {d: round(st.fmean(v), 4) for d, v in sorted(by_day.items())},
            "per_day_n": {d: len(v) for d, v in sorted(by_day.items())},
            "leave_one_day_out_min_sum": round(min(tot - sum(v) for v in by_day.values()), 3) if by_day else None,
            "unmeasured_with_entry": len(unm), "sum_if_unmeasured_at_mark": round(tot + at_mark, 3),
            "sum_if_unmeasured_recover_zero": round(tot + sum(-1.0 - cost for _ in unm), 3)}


groups = defaultdict(list)
for r in rows:
    groups[(r["rule"], r["exit"], r["delay"])].append(r)
table = []
for (rn, xn, d), g in groups.items():
    for c in COSTS:
        table.append({"rule": rn, "exit": xn, "delay": d, "cost": c, **summary(g, c)})
out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "inputs": inputs,
       "coverage": {"start": COV.start, "end": COV.end, "gaps": gaps}, "rules": list(RULES), "exits": EXITS,
       "delays": DELAYS, "costs": COSTS, "episode_s": EPISODE_S, "table": table}
json.dump(out, open(OUT + "t9_replay_v2.json", "w"), indent=1)
with open(OUT + "t9_replay_v2_trades.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")

primary = [t for t in table if t["rule"] == "momentum: +40% in 5 min, volume x4" and t["exit"] == "hold 60m"
           and t["cost"] == 0.012]
print("\nprimary-like (+40% in 5 min, x4 volume, hold 60m, 1.2% cost):")
for t in sorted(primary, key=lambda t: t["delay"]):
    print(f"  delay {t['delay']:>2}s: signals {t['signals']} closed {t['closed']} ({t['status']}) mean {t['mean']} "
          f"median {t['median']} sum {t['sum']} | episodes {t['episodes']} top {t['top_episode_share']} "
          f"top3 {t['top3_episode_share']} w/o top3 {t['sum_without_top3_episodes']} | per day {t['per_day_mean']}")
ok = [t for t in table if t["cost"] == 0.012 and t["closed"] >= 15 and len(t["per_day_mean"]) == 3]
print(f"\n{len(ok)} rule x exit x delay combinations with 15+ closed trades on all 3 days (1.2% cost); "
      f"positive mean: {sum(t['mean'] > 0 for t in ok)}; positive every day: "
      f"{sum(min(t['per_day_mean'].values()) > 0 for t in ok)}; positive without top 3 episodes: "
      f"{sum((t['sum_without_top3_episodes'] or 0) > 0 for t in ok)}")
for t in sorted(ok, key=lambda t: -min(t["per_day_mean"].values()))[:12]:
    print(f"  {t['rule']:38s} | {t['exit']:24s} {t['delay']:>2}s n={t['closed']:4d} cov {t['coverage']:.2f} "
          f"mean {100 * t['mean']:+6.1f}% med {100 * t['median']:+6.1f}% w/o top3 ep {t['sum_without_top3_episodes']:+7.2f} "
          f"| per day {[round(100 * v, 1) for v in t['per_day_mean'].values()]}")
print("done", round(time.time() - t_start), "s")
