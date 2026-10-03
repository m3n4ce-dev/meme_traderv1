"""A/B experiments: run named setting variants over many market samples in parallel and report how
each did across samples: the average, the spread, and how often it beat the baseline on the same
sample. A variant that wins on average but loses in most samples is luck, not edge.

    python -m meme_trader.sniper compare --synthetic 1500 --seeds 1-8 \\
        --variant "late: late.enabled=true" --variant "ladder: exit.profile=ladder"

    python -m meme_trader.sniper compare --file data/feed-* --variant "gate: predict.min_p=0.2"
        (each recorded file - one UTC day - is one sample)

'base' (your current settings) always runs first and is the yardstick.
"""
from __future__ import annotations

import asyncio
import copy
import math
import statistics
import time


def parse_variant(text: str) -> tuple[str, list[str]]:
    """'late: late.enabled=true exit.stop_loss_pct=25' -> ('late', ['late.enabled=true', 'exit.stop_loss_pct=25'])"""
    name, _, rest = text.partition(":")
    sets = rest.split()
    if not name.strip() or not all("=" in s for s in sets):
        raise SystemExit(f"bad --variant {text!r}: use 'name: key=value key=value'")
    return name.strip(), sets


def parse_seeds(text: str) -> list[int]:
    """'1-4,9' -> [1, 2, 3, 4, 9]"""
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        elif part.strip():
            out.append(int(part))
    return out


def _max_dd(equity_hist) -> float:
    peak, worst = 0.0, 0.0
    for _, eq in equity_hist:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (1 - eq / peak) * 100)
    return worst


def run_one(params, sets: list[str], sample: dict) -> dict:
    """One backtest. Streams the market (regenerated from its seed, or read from disk) so memory
    stays flat however many run at once."""
    from .__main__ import _sim_leaders, apply_overrides, run_backtest
    from .feeds import FileFeed, SyntheticFeed

    p = apply_overrides(copy.deepcopy(params), sets)
    if sample.get("files"):
        feed = FileFeed(*sample["files"])
    else:
        feed = SyntheticFeed(seed=sample["seed"], speed=0, launches=sample["synthetic"], start_ts=1_780_000_000)
        _sim_leaders(p, feed)
    t0 = time.time()
    eng = asyncio.run(run_backtest(p, feed))
    s = eng.summary()
    return {"pnl": s["realized_pnl_sol"], "pf": s["profit_factor"], "win": s["win_rate"], "n": s["closed"],
            "dd": max(eng.book.max_dd_pct, _max_dd(eng.book.equity_hist)), "seconds": time.time() - t0,
            "by_source": {k: v["realized_pnl_sol"] for k, v in s["by_source"].items()}}


def _job(args):
    params, name, sets, key, sample = args
    return name, key, run_one(params, sets, sample)


def compare(params, variants: list[tuple[str, list[str]]], samples: list[tuple[str, dict]], jobs: int = 1,
            log=print) -> dict:
    variants = [("base", [])] + [v for v in variants if v[0] != "base"]
    tasks = [(params, name, sets, key, sample) for key, sample in samples for name, sets in variants]
    log(f"{len(variants)} variants x {len(samples)} samples = {len(tasks)} backtests on {jobs} process(es)")
    results: dict[str, dict[str, dict]] = {name: {} for name, _ in variants}
    t0 = time.time()
    if jobs > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futs = [pool.submit(_job, t) for t in tasks]
            for i, f in enumerate(as_completed(futs), 1):
                name, key, r = f.result()
                results[name][key] = r
                log(f"  [{i}/{len(tasks)}] {name:<12} {key:<22} P&L {r['pnl']:+.3f}  n={r['n']}  ({time.time() - t0:.0f}s)")
    else:
        for i, t in enumerate(tasks, 1):
            name, key, r = _job(t)
            results[name][key] = r
            log(f"  [{i}/{len(tasks)}] {name:<12} {key:<22} P&L {r['pnl']:+.3f}  n={r['n']}  ({time.time() - t0:.0f}s)")
    return {"variants": variants, "samples": [k for k, _ in samples], "results": results}


def summarize(res: dict) -> list[dict]:
    base = res["results"]["base"]
    rows = []
    for name, sets in res["variants"]:
        rs = res["results"][name]
        keys = [k for k in res["samples"] if k in rs]
        pnls = [rs[k]["pnl"] for k in keys]
        pfs = sorted(min(r["pf"], 99.0) for r in rs.values())
        diffs = [rs[k]["pnl"] - base[k]["pnl"] for k in keys if k in base]
        rows.append({
            "variant": name, "sets": sets, "samples": len(keys),
            "mean_pnl": statistics.mean(pnls) if pnls else 0.0,
            "sd_pnl": statistics.stdev(pnls) if len(pnls) > 1 else 0.0,
            "min_pnl": min(pnls, default=0.0), "max_pnl": max(pnls, default=0.0),
            "median_pf": pfs[len(pfs) // 2] if pfs else 0.0,
            "win_rate": statistics.mean(r["win"] for r in rs.values()) if rs else 0.0,
            "trades": statistics.mean(r["n"] for r in rs.values()) if rs else 0.0,
            "worst_dd": max((r["dd"] for r in rs.values()), default=0.0),
            "profitable": sum(p > 0 for p in pnls),
            "beat_base": sum(d > 0 for d in diffs) if name != "base" else None,
            "mean_diff": statistics.mean(diffs) if diffs and name != "base" else None,
            "diff_t": _t(diffs) if name != "base" else None,
        })
    return rows


def _t(diffs: list[float]) -> float | None:
    """Paired t statistic of variant - base across samples (|t| > ~2.4 with 8 samples is unlikely to be noise)."""
    if len(diffs) < 3:
        return None
    sd = statistics.stdev(diffs)
    return statistics.mean(diffs) / (sd / math.sqrt(len(diffs))) if sd > 0 else None


def report(res: dict, synthetic: bool) -> str:
    rows = summarize(res)
    n = len(res["samples"])
    lines = ["", f"=== compare: {n} sample(s) {'(SYNTHETIC market - tests logic, not real profitability)' if synthetic else ''} ===",
             f"{'variant':<14}{'mean P&L':>10}{'sd':>8}{'min':>8}{'max':>8}{'med PF':>8}{'win%':>6}{'trades':>8}"
             f"{'worst DD':>9}{'profit':>8}{'beat base':>11}{'t':>6}"]
    for r in rows:
        beat = "-" if r["beat_base"] is None else f"{r['beat_base']}/{r['samples']}"
        t = "-" if r["diff_t"] is None else f"{r['diff_t']:+.1f}"
        lines.append(f"{r['variant']:<14}{r['mean_pnl']:>+10.3f}{r['sd_pnl']:>8.3f}{r['min_pnl']:>+8.3f}{r['max_pnl']:>+8.3f}"
                     f"{r['median_pf']:>8.2f}{r['win_rate'] * 100:>5.0f}%{r['trades']:>8.1f}{r['worst_dd']:>8.1f}%"
                     f"{r['profitable']:>5}/{r['samples']:<2}{beat:>11}{t:>6}")
    lines += ["", "beat base = samples where the variant made more than base on the SAME market; t = paired t-statistic",
              "(roughly: |t| above 2.4 with 8 samples, or 2.0 with 30, is unlikely to be noise)."]
    for r in rows:
        if r["sets"]:
            lines.append(f"  {r['variant']}: {' '.join(r['sets'])}")
    best = [r for r in rows if r["variant"] != "base" and r["beat_base"] is not None and r["samples"] >= 3
            and r["beat_base"] >= 0.75 * r["samples"] and (r["diff_t"] or 0) > 2]
    if best:
        b = max(best, key=lambda r: r["mean_diff"])
        lines.append(f"\nCONSISTENT WINNER: {b['variant']} (+{b['mean_diff']:.3f} SOL per sample vs base, beat it in "
                     f"{b['beat_base']}/{b['samples']}).")
    else:
        lines.append("\nNo variant beat base consistently enough (>= 75% of samples and t > 2) to call it an edge.")
    return "\n".join(lines)
