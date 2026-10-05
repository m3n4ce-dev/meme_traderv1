"""Is a strategy's edge real? Its forward trades (paper or live, never mixed) judged the way a skeptical
trading group would: the margin of error, how much rests on a few big winners, day by day, and whether the
settings stayed the same long enough to count as one test. Measurement only: nothing here tunes anything."""
from __future__ import annotations

import random
import statistics
import time
from collections import defaultdict

SOURCES = {"late": "Graduation plays", "sniper": "Sniper", "copy": "Copy trading", "callout": "Callouts", "manual": "Your own trades"}
MIN_N = 30


def check(trades: list[dict], source: str, boots: int = 4000, seed: int = 7) -> dict:
    rows = sorted((t for t in trades if (t.get("source") or "").split(":")[0] == source and t.get("cost")),
                  key=lambda t: t["opened"])
    out = {"source": source, "label": SOURCES.get(source, source), "n": len(rows)}
    if not rows:
        return {**out, "level": "none", "verdict": "No trades yet.", "caveats": [], "by_day": []}
    rets = [t["pnl"] / t["cost"] for t in rows]
    pnl, stake = sum(t["pnl"] for t in rows), sum(t["cost"] for t in rows)
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(rets, k=len(rets))) for _ in range(boots))
    lo, hi = means[int(0.05 * boots)], means[int(0.95 * boots) - 1]           # 90% range of the mean trade
    best = sorted(rows, key=lambda t: -t["pnl"])
    without3 = sum(t["pnl"] for t in best[3:])
    days: dict[str, list] = defaultdict(list)
    for t in rows:
        days[time.strftime("%Y-%m-%d", time.localtime(t["opened"]))].append(t)
    by_day = [{"day": d, "n": len(v), "pnl": sum(t["pnl"] for t in v),
               "ret_pct": sum(t["pnl"] for t in v) / sum(t["cost"] for t in v) * 100,
               "win_rate": sum(t["pnl"] > 0 for t in v) / len(v)} for d, v in sorted(days.items())]
    configs = len({t.get("config") for t in rows if t.get("config")})
    span_d = (rows[-1]["closed"] - rows[0]["opened"]) / 86400
    out.update(pnl_sol=pnl, staked_sol=stake, ret_pct=pnl / stake * 100, mean_pct=statistics.fmean(rets) * 100,
               median_pct=statistics.median(rets) * 100, win_rate=sum(r > 0 for r in rets) / len(rets),
               lo_pct=lo * 100, hi_pct=hi * 100, p_not_positive=sum(m <= 0 for m in means) / boots,
               without_best3_sol=without3, best3_share=(pnl - without3) / pnl if pnl > 0 else None,
               best=[{"symbol": t.get("symbol"), "pnl": t["pnl"], "pnl_pct": t.get("pnl_pct")} for t in best[:3]],
               by_day=by_day, days_up=sum(d["pnl"] > 0 for d in by_day), configs=configs, span_days=span_d,
               first=rows[0]["opened"], last=rows[-1]["closed"])
    if len(rows) < MIN_N:
        level, verdict = "early", f"Too early to say: {len(rows)} trades. Wait for at least {MIN_N}."
    elif hi < 0:
        level, verdict = "bad", "Losing, and not by bad luck: even the hopeful end of the range is below zero."
    elif lo > 0 and without3 > 0 and len(rows) >= 100 and span_d >= 7:
        level, verdict = "good", "Looks real so far: positive even without its three best trades, over a week or more. Keep it running unchanged."
    elif lo > 0 and without3 > 0:
        level, verdict = "promising", (f"Promising: positive even without its three best trades, but that's {len(rows)} trades over "
                                       f"{_span(span_d)}. Run it unchanged for a week and 100+ trades before trusting it.")
    elif lo > 0:
        level, verdict = "fragile", (f"Positive, but it rests on a few big winners: without its best three trades it's "
                                     f"{without3:+.2f} SOL. Normal for memecoins, but it needs more trades before anyone should trust it.")
    else:
        level, verdict = "unclear", "Can't tell yet: the range includes zero."
    caveats = []
    if pnl > 0 and (pnl - without3) / pnl > 0.5:
        caveats.append(f"Its best three trades made {(pnl - without3) / pnl:.0%} of the profit: one missed runner changes the picture.")
    if span_d < 7:
        caveats.append(f"Only {_span(span_d)} of trades: memecoin markets change week to week.")
    if configs > 3:
        caveats.append(f"The settings changed {configs - 1} times in this period, so it isn't one consistent test.")
    if rows[0].get("mode", "").startswith("paper"):
        caveats.append("Paper trades: fees, delay and slippage are modelled, not real fills.")
    return {**out, "level": level, "verdict": verdict, "caveats": caveats}


def _span(days: float) -> str:
    return f"{days * 24:.0f} hours" if days < 1 else f"{days:.1f} days"


def report(trades: list[dict], mode: str) -> dict:
    present = [s for s in SOURCES if any((t.get("source") or "").split(":")[0] == s for t in trades)]
    return {"generated": time.time(), "mode": mode, "n": len(trades), "strategies": [check(trades, s) for s in present]}


def as_text(rep: dict, include_manual: bool = False) -> str:
    """Plain text you can paste into a group chat: the bots' strategies (your own trades only if asked).
    Paper results are labelled paper."""
    lines = [f"Edge check · {rep['mode']} trades · {time.strftime('%Y-%m-%d', time.localtime(rep['generated']))}"]
    for s in rep["strategies"]:
        if s["source"] == "manual" and not include_manual:
            continue
        lines.append("")
        if not s["n"] or "ret_pct" not in s:
            lines.append(f"{s['label']}: {s['verdict']}")
            continue
        lines.append(f"{s['label']}: {s['n']} trades over {_span(s['span_days'])}, {s['pnl_sol']:+.3f} SOL on {s['staked_sol']:.2f} SOL staked "
                     f"({s['ret_pct']:+.1f}% per SOL), won {s['win_rate']:.0%}")
        lines.append(f"  average trade {s['mean_pct']:+.1f}% (90% range {s['lo_pct']:+.1f}% to {s['hi_pct']:+.1f}%), median {s['median_pct']:+.1f}%")
        lines.append(f"  without its best 3 trades: {s['without_best3_sol']:+.3f} SOL · days up: {s['days_up']} of {len(s['by_day'])}")
        lines.append(f"  verdict: {s['verdict']}")
        lines.extend(f"  note: {c}" for c in s["caveats"])
    return "\n".join(lines)


def exit_whatifs(trades: list[dict], source: str = "late", cost_pct: float = 7.0) -> dict:
    """Rough what-ifs for other exits on a strategy's past trades, from each trade's recorded peak: "sell all at +X%"
    and "bank part at +X%, the rest as traded". Approximate (peaks are sampled, ~7% round trip assumed) and in-sample:
    a hint for the team, which the exit lab then tests forward."""
    rows = [t for t in trades if (t.get("source") or "").split(":")[0] == source and t.get("cost")]
    if not rows:
        return {}
    def total(rule):
        xs = [rule(t) for t in rows]
        return {"mean_pct": round(statistics.fmean(xs), 1), "won_pct": round(sum(x > 0 for x in xs) / len(xs) * 100),
                "total_sol": round(sum(x / 100 * t["cost"] for x, t in zip(xs, rows)), 2)}
    pk = lambda t: t.get("peak_gain_pct") or 0
    out = {"trades": len(rows), "as traded": total(lambda t: t["pnl_pct"])}
    for tp in (30, 50, 100):
        out[f"sell all at +{tp}%"] = total(lambda t, tp=tp: (tp - cost_pct) if pk(t) >= tp else t["pnl_pct"])
    for frac, tp in ((.5, 30), (.5, 50), (1 / 3, 30)):
        out[f"bank {'half' if frac == .5 else 'a third'} at +{tp}%, rest as traded"] = total(
            lambda t, f=frac, tp=tp: (f * (tp - cost_pct) + (1 - f) * t["pnl_pct"]) if pk(t) >= tp else t["pnl_pct"])
    return out
