from _paths import DATA, OUT, ROOT  # noqa: F401,E402  (repo root, data/, outputs: see _paths.py)
import glob, json, statistics as st
S = OUT
days = {f[-15:-5]: json.load(open(f)) for f in sorted(glob.glob(S + "style-2026-10-0*.json"))}
keys = set.intersection(*(set(d) for d in days.values()))
res = []
for k in keys:
    per = [days[d][k] for d in sorted(days)]
    n = sum(p[0] for p in per)
    avg = sum(p[0] * p[1] for p in per) / n
    res.append((k, n, round(avg, 2), min(p[1] for p in per), sum(p[1] > 0 for p in per), per))
print("days:", sorted(days), " rules x exits:", len(res))
print("\nall rules: share with a positive average over the 4 days:", round(100 * sum(r[2] > 0 for r in res) / len(res)), "%")
print("\nTOP 12 by the WORST day (every day >= 15 trades):")
for r in sorted([r for r in res if min(p[0] for p in r[5]) >= 15], key=lambda r: -r[3])[:12]:
    print(f"{r[0]:62s} n={r[1]:4d} avg {r[2]:+6.2f}% worst day {r[3]:+6.2f}% days+ {r[4]}/4  per day {[p[1] for p in r[5]]} n {[p[0] for p in r[5]]}")
def show(sub):
    for r in sorted([r for r in res if sub in r[0]], key=lambda r: r[0]):
        print(f"{r[0]:62s} n={r[1]:4d} avg {r[2]:+6.2f}% worst {r[3]:+6.2f}% days+ {r[4]}/4 {[p[1] for p in r[5]]}")
print("\nYOUR STYLE, centre (50+ buyers/min, 10-35% off the high, up 20%+ in a minute, curve 50-70%):")
show("b50 dip0.10-0.35 chg60>=0.2 curve50-70")
print("\nTHE BOTS' STYLE (at the high, 0-3% off):")
show("b50 dip0.00-0.03 chg60>=0.2 curve50-70")
# by dimension: average over everything else
for dim, vals in (("dip", ["dip0.00-0.03", "dip0.05-0.25", "dip0.10-0.35", "dip0.15-0.45"]), ("buyers", ["b30 ", "b50 ", "b80 "]),
                  ("chg60", ["chg60>=0.0 ", "chg60>=0.2 ", "chg60>=0.5 "]), ("curve", ["curve40-60", "curve50-70", "curve60-80"]),
                  ("exit", ["E1", "E2", "E3", "E4", "E5"])):
    print(f"\n{dim}: " + "  ".join(f"{v.strip()} {st.fmean(r[2] for r in res if v in r[0]):+.2f}% ({sum(r[2] > 0 for r in res if v in r[0])}/{sum(1 for r in res if v in r[0])} positive)" for v in vals))
