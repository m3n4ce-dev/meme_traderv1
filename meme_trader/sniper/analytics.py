"""Performance analytics: what's working, what isn't, and what the next trades could look like.

Pure python over the engine's closed-trade rows. Callout bags ($1 marketing buys) are reported on
their own and never mixed into the trading numbers. The projection is a block bootstrap of the
trades we actually made: it assumes the next trades look like the past ones, which is the most it
can honestly say. It is a range of outcomes, not a forecast.
"""
from __future__ import annotations

import math
import random
import re
import statistics
import time
from collections import defaultdict

PNL_BINS = [-100, -50, -25, -10, 0, 10, 25, 50, 100, 200, 500]       # % per trade
HOLD_BINS = [0, 15, 30, 60, 120, 300, 600, 1200]                        # seconds
SCORE_BINS = [0, 50, 60, 70, 80, 90]
P_BINS = [0.0, 0.1, 0.2, 0.3, 0.5, 0.7]
MIN_TRADES_MC = 10


def exit_key(reason: str) -> str:
    """'trailing stop -22% from peak (+80%)' -> 'trailing stop'; 'leader alpha sold 50%' -> 'leader sold'."""
    reason = reason or ""
    if reason.startswith("leader ") and " sold" in reason:
        return "leader sold"
    return re.split(r"\s[-+]|\d|\(", reason, maxsplit=1)[0].strip(" :-+") or reason or "?"


def _f(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def _pf(rows: list[dict]) -> float:
    gw = sum(c["pnl"] for c in rows if c["pnl"] > 0)
    gl = -sum(c["pnl"] for c in rows if c["pnl"] <= 0)
    return gw / gl if gl else (float("inf") if gw else 0.0)


def _group_row(key, rows: list[dict]) -> dict:
    n = len(rows)
    pnl = sum(c["pnl"] for c in rows)
    return {"key": key, "n": n, "win_rate": sum(c["pnl"] > 0 for c in rows) / n if n else 0.0,
            "pnl_sol": _f(pnl), "avg_pnl_pct": _f(sum(c["pnl_pct"] for c in rows) / n if n else 0.0, 2),
            "profit_factor": _pf(rows), "reached_2x": sum(c.get("peak_gain_pct", 0) >= 100 for c in rows) / n if n else 0.0}


def _group(rows: list[dict], keyfn) -> list[dict]:
    g: dict = defaultdict(list)
    for c in rows:
        g[keyfn(c)].append(c)
    return sorted((_group_row(k, v) for k, v in g.items()), key=lambda r: -r["pnl_sol"])


def _bucket(x: float, edges: list[float]) -> str:
    """Label of the [lo, hi) bucket containing x; the last bucket is open-ended."""
    for lo, hi in zip(edges, edges[1:]):
        if lo <= x < hi:
            return f"{lo:g}-{hi:g}"
    return f"{edges[-1]:g}+" if x >= edges[-1] else f"<{edges[0]:g}"


def _buckets(rows: list[dict], key: str, edges: list[float]) -> list[dict]:
    order = [f"<{edges[0]:g}"] + [f"{lo:g}-{hi:g}" for lo, hi in zip(edges, edges[1:])] + [f"{edges[-1]:g}+"]
    g: dict = defaultdict(list)
    for c in rows:
        v = c.get(key)
        if v is not None:
            g[_bucket(v, edges)].append(c)
    return [_group_row(k, g[k]) for k in order if g.get(k)]


def _hist(values: list[float], edges: list[float]) -> list[dict]:
    labels = [f"<{edges[0]:g}"] + [f"{lo:g}..{hi:g}" for lo, hi in zip(edges, edges[1:])] + [f"{edges[-1]:g}+"]
    counts = [0] * len(labels)
    for v in values:
        if v < edges[0]:
            counts[0] += 1
            continue
        for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
            if lo <= v < hi:
                counts[i + 1] += 1
                break
        else:
            counts[-1] += 1
    return [{"bin": b, "n": n} for b, n in zip(labels, counts)]


def _quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = (len(s) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def _streaks(rows: list[dict]) -> tuple[int, int]:
    best = worst = cur_w = cur_l = 0
    for c in rows:
        if c["pnl"] > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_w, cur_l = 0, cur_l + 1
        best, worst = max(best, cur_w), max(worst, cur_l)
    return best, worst


def drawdown(equity_hist: list, observed_max_pct: float | None = None) -> dict:
    """equity_hist: [(ts, equity_sol)]. Max and current peak-to-trough drawdown, plus a chartable series.
    The history may be a rolling chart buffer; observed_max_pct (tracked on every update for the whole
    session) keeps an older, larger drawdown from disappearing when it scrolls out."""
    peak, max_dd, series = 0.0, float(observed_max_pct or 0.0), []
    for ts, eq in equity_hist:
        peak = max(peak, eq)
        dd = (1 - eq / peak) * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        series.append((ts, eq, dd))
    step = max(1, len(series) // 300)
    pts = series[::step]
    if series and pts[-1] is not series[-1]:
        pts.append(series[-1])
    cur = series[-1][2] if series else 0.0
    return {"max_pct": _f(max_dd, 2), "current_pct": _f(cur, 2),
            "series": [{"ts": t, "equity": _f(e), "dd_pct": _f(d, 2)} for t, e, d in pts]}


def edge_confidence(pnls: list[float], sims: int = 1000, seed: int = 11) -> dict | None:
    """Bootstrap the mean P&L per trade: how sure can we be the edge is above zero?"""
    n = len(pnls)
    if n < MIN_TRADES_MC:
        return None
    rng = random.Random(seed)
    sims = max(200, min(sims, 2_000_000 // n))          # bounded work however long the history gets
    means = sorted(sum(rng.choices(pnls, k=n)) / n for _ in range(sims))
    return {"mean_sol": _f(sum(pnls) / n), "lo_sol": _f(means[int(0.05 * sims)]), "hi_sol": _f(means[int(0.95 * sims) - 1]),
            "p_positive": sum(m > 0 for m in means) / sims}


def monte_carlo(pnls: list[float], equity_now: float, start_sol: float, max_drawdown_pct: float,
                horizon: int = 100, sims: int = 1000, seed: int = 7) -> dict | None:
    """Block-bootstrap the next `horizon` trades from the ones we made (blocks of 5 keep hot and cold
    streaks together). Percentile fan, chance of a profit, chance of hitting the kill switch."""
    n = len(pnls)
    if n < MIN_TRADES_MC:
        return None
    rng = random.Random(seed)
    block = 5 if n >= 30 else 1
    kill_level = start_sol * (1 - max_drawdown_pct / 100)
    marks = sorted(set([0] + list(range(0, horizon + 1, max(1, horizon // 25))) + [horizon]))
    at_mark: dict[int, list[float]] = {m: [] for m in marks}
    finals, max_dds, kills = [], [], 0
    for _ in range(sims):
        eq = peak = equity_now
        worst = 0.0
        killed = False
        at_mark[0].append(eq)
        k = 0
        while k < horizon:
            i = rng.randrange(n)
            for j in range(block):
                if k >= horizon:
                    break
                if not killed:
                    eq += pnls[(i + j) % n]
                    peak = max(peak, eq)
                    worst = max(worst, (1 - eq / peak) * 100 if peak > 0 else 0.0)
                    if eq <= kill_level:
                        killed = True          # the engine stops trading here: equity freezes
                k += 1
                if k in at_mark:
                    at_mark[k].append(eq)
        kills += killed
        finals.append(eq)
        max_dds.append(worst)
    fan = [{"trade": m, **{f"p{q}": _f(_quantile(at_mark[m], q / 100)) for q in (5, 25, 50, 75, 95)}} for m in marks]
    return {
        "horizon": horizon, "sims": sims, "block": block, "start_equity": _f(equity_now),
        "fan": fan, "p_profit": sum(f > equity_now for f in finals) / sims, "p_kill_switch": kills / sims,
        "median_final": _f(_quantile(finals, 0.5)), "p5_final": _f(_quantile(finals, 0.05)),
        "p95_final": _f(_quantile(finals, 0.95)), "median_max_dd_pct": _f(_quantile(max_dds, 0.5), 1),
        "p90_max_dd_pct": _f(_quantile(max_dds, 0.9), 1), "kill_level": _f(kill_level),
    }


def live_calibration(rows: list[dict]) -> list[dict]:
    """Model P(2x) at entry vs how often the trade actually reached 2x (peak). Approximate: the model's
    label also requires no -30% first, so realized rates should sit at or above the prediction."""
    out = []
    for lo, hi in zip(P_BINS, P_BINS[1:] + [1.01]):
        g = [c for c in rows if c.get("p") is not None and lo <= c["p"] < hi]
        if g:
            out.append({"bin": f"{lo:.0%}-{min(hi, 1):.0%}", "n": len(g),
                        "predicted": sum(c["p"] for c in g) / len(g),
                        "realized_2x": sum(c.get("peak_gain_pct", 0) >= 100 for c in g) / len(g)})
    return out


def kpis(rows: list[dict], start_sol: float, fee_pct: float = 0.0) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = [c for c in rows if c["pnl"] > 0]
    losses = [c for c in rows if c["pnl"] <= 0]
    pcts = [c["pnl_pct"] for c in rows]
    avg_win = sum(c["pnl_pct"] for c in wins) / len(wins) if wins else 0.0
    avg_loss = sum(c["pnl_pct"] for c in losses) / len(losses) if losses else 0.0
    payoff = avg_win / -avg_loss if avg_loss < 0 else (float("inf") if wins else 0.0)
    wr = len(wins) / n
    sd = statistics.pstdev(pcts) if n > 1 else 0.0
    mean_pct = sum(pcts) / n
    holds = [c["closed"] - c["opened"] for c in rows]
    best_streak, worst_streak = _streaks(rows)
    runs = [c for c in rows if c.get("peak_gain_pct", 0) >= 20]
    capture = [max(-2.0, min(1.0, c["pnl_pct"] / c["peak_gain_pct"])) for c in runs]
    kelly = wr - (1 - wr) / payoff if payoff and math.isfinite(payoff) else None
    span_h = max((max(c["closed"] for c in rows) - min(c["opened"] for c in rows)) / 3600, 1e-9)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "win_rate": wr,
        "pnl_sol": _f(sum(c["pnl"] for c in rows)), "return_pct": _f(sum(c["pnl"] for c in rows) / start_sol * 100, 2),
        "expectancy_sol": _f(sum(c["pnl"] for c in rows) / n), "expectancy_pct": _f(mean_pct, 2),
        "avg_win_pct": _f(avg_win, 2), "avg_loss_pct": _f(avg_loss, 2), "payoff": payoff,
        "breakeven_win_rate": 1 / (1 + payoff) if payoff and math.isfinite(payoff) else None,
        "profit_factor": _pf(rows), "sharpe_per_trade": _f(mean_pct / sd, 3) if sd else None,
        "sqn": _f(math.sqrt(min(n, 100)) * mean_pct / sd, 2) if sd else None,
        "kelly_full": _f(kelly, 3), "best_pct": _f(max(pcts), 1), "worst_pct": _f(min(pcts), 1),
        "median_pct": _f(statistics.median(pcts), 1),
        "avg_hold_s": _f(sum(holds) / n, 1), "median_hold_s": _f(statistics.median(holds), 1),
        "win_streak": best_streak, "loss_streak": worst_streak,
        "capture_pct": _f(sum(capture) / len(capture) * 100, 1) if capture else None,
        "left_on_table_pct": _f(sum(max(c["peak_gain_pct"] - c["pnl_pct"], 0) for c in wins) / len(wins), 1) if wins else None,
        "winner_mae_p90": _f(_quantile([c.get("mae_pct", 0.0) for c in wins], 0.1), 1) if wins else None,
        "loser_mae_median": _f(_quantile([c.get("mae_pct", 0.0) for c in losses], 0.5), 1) if losses else None,
        "fees_est_sol": _f(sum(c["cost"] + c["proceeds"] for c in rows) * fee_pct / 100),
        "volume_sol": _f(sum(c["cost"] for c in rows)), "trades_per_hour": _f(n / span_h, 2),
        "initials_rate": sum(bool(c.get("initials")) for c in rows) / n,
    }


def insights(k: dict, trades: list[dict], by_source: list, by_exit: list, by_hour: list, gates: list,
             edge: dict | None, mc: dict | None) -> list[dict]:
    """Plain-English highlights, most important first. Facts only - no advice the numbers can't back."""
    out: list[dict] = []
    n = k.get("n", 0)
    if not n:
        return [{"level": "info", "text": "No closed trades yet. Highlights appear after the first exits."}]
    if n < 30:
        out.append({"level": "warn", "text": f"Only {n} closed trade{'s' * (n != 1)}: every number here is noisy. "
                                             f"Treat it as a direction, not a verdict, until ~100 trades."})
    if edge:
        lvl = "good" if edge["p_positive"] >= 0.9 else ("bad" if edge["p_positive"] <= 0.3 else "info")
        out.append({"level": lvl, "text": f"Expectancy {edge['mean_sol']:+.4f} SOL per trade; above zero in "
                                          f"{edge['p_positive']:.0%} of bootstrap resamples (90% range "
                                          f"{edge['lo_sol']:+.4f} to {edge['hi_sol']:+.4f})."})
    if k.get("breakeven_win_rate") is not None:
        gap = k["win_rate"] - k["breakeven_win_rate"]
        out.append({"level": "good" if gap > 0 else "bad",
                    "text": f"Win rate {k['win_rate']:.0%} vs {k['breakeven_win_rate']:.0%} needed at a "
                            f"{k['payoff']:.2f} payoff ratio ({gap * 100:+.0f} points)."})
    if len(by_source) > 1:
        best, worst = by_source[0], by_source[-1]
        out.append({"level": "info", "text": f"Best strategy: {best['key']} ({best['pnl_sol']:+.3f} SOL over {best['n']}). "
                                             f"Weakest: {worst['key']} ({worst['pnl_sol']:+.3f} SOL over {worst['n']})."})
    if by_exit:
        top = by_exit[0]
        bottom = by_exit[-1]
        if top["pnl_sol"] > 0:
            out.append({"level": "good", "text": f"Exit that made the most: '{top['key']}' ({top['pnl_sol']:+.3f} SOL, {top['n']} trades)."})
        if bottom["pnl_sol"] < 0:
            out.append({"level": "bad", "text": f"Exit that cost the most: '{bottom['key']}' ({bottom['pnl_sol']:+.3f} SOL, {bottom['n']} trades)."})
    if k.get("capture_pct") is not None:
        out.append({"level": "info", "text": f"Trades that ran 20%+ kept {k['capture_pct']:.0f}% of their peak gain on average."})
    if k.get("winner_mae_p90") is not None and k["wins"] >= 10:
        out.append({"level": "info", "text": f"90% of winners never dipped below {k['winner_mae_p90']:.0f}% after entry; "
                                             f"the median loser bottomed at {k['loser_mae_median'] or 0:.0f}%."})
    hi = [c for c in trades if c.get("p") is not None and c["p"] >= 0.3]
    lo = [c for c in trades if c.get("p") is not None and c["p"] < 0.3]
    if len(hi) >= 5 and len(lo) >= 5:
        wh = sum(c["pnl"] > 0 for c in hi) / len(hi)
        wl = sum(c["pnl"] > 0 for c in lo) / len(lo)
        out.append({"level": "good" if wh > wl else "warn",
                    "text": f"Model check: trades with P(2x) >= 30% won {wh:.0%} ({len(hi)}) vs {wl:.0%} below ({len(lo)})."})
    if n >= 50 and len(by_hour) >= 4:
        b = max(by_hour, key=lambda r: r["pnl_sol"])
        w = min(by_hour, key=lambda r: r["pnl_sol"])
        out.append({"level": "info", "text": f"Best UTC hour {b['key']}:00 ({b['pnl_sol']:+.3f} SOL); "
                                             f"worst {w['key']}:00 ({w['pnl_sol']:+.3f} SOL)."})
    ref = next((g for g in gates if g["gate"] == "(bought)"), None)
    if ref and ref["n"] >= 20:
        base = ref["win_first_pct"]
        rejects = [g for g in gates if g["gate"] != "(bought)" and g["n"] >= 20]
        costly = [g for g in rejects if g["win_first_pct"] > base * 1.2 + 2]
        if costly:
            g = max(costly, key=lambda g: g["win_first_pct"] - base)
            out.append({"level": "warn", "text": f"Gate '{g['gate']}' may be too strict: {g['win_first_pct']:.0f}% of the "
                                                 f"{g['n']} tokens it rejected would have doubled before -30%, vs "
                                                 f"{base:.0f}% of the tokens we bought."})
        useful = [g for g in rejects if g["win_first_pct"] < base / 2]
        if useful:
            g = max(useful, key=lambda g: g["n"])
            out.append({"level": "good", "text": f"Gate '{g['gate']}' is earning its keep: only {g['win_first_pct']:.0f}% of "
                                                 f"its {g['n']} rejects doubled before -30%, vs {base:.0f}% of our buys."})
    if mc:
        out.append({"level": "bad" if mc["p_kill_switch"] >= 0.2 else "info",
                    "text": f"Next {mc['horizon']} trades if they look like these: median equity {mc['median_final']:.3f} SOL "
                            f"(5-95%: {mc['p5_final']:.3f} to {mc['p95_final']:.3f}), profit in {mc['p_profit']:.0%} "
                            f"of paths, kill switch in {mc['p_kill_switch']:.0%}."})
    return out


def compute(closed: list[dict], equity_hist: list, start_sol: float, max_drawdown_pct: float,
            gate_audit: list | None = None, model_card: dict | None = None, fee_pct: float = 0.0,
            horizon: int = 100, sims: int = 1000, observed_max_dd_pct: float | None = None) -> dict:
    trades = [c for c in closed if c.get("source") != "callout"]
    bags = [c for c in closed if c.get("source") == "callout"]
    k = kpis(trades, start_sol, fee_pct)
    by_source = _group(trades, lambda c: c["source"].split(":")[0])
    by_exit = _group(trades, lambda c: exit_key(c.get("exit", "")))
    by_hour = sorted(_group(trades, lambda c: time.gmtime(c["opened"]).tm_hour), key=lambda r: r["key"])
    by_day = sorted(_group(trades, lambda c: time.strftime("%Y-%m-%d", time.gmtime(c["closed"]))), key=lambda r: r["key"])
    by_score = _buckets(trades, "score", SCORE_BINS)
    by_p = _buckets(trades, "p", P_BINS)
    pnls = [c["pnl"] for c in trades]
    equity_now = equity_hist[-1][1] if equity_hist else start_sol + sum(c["pnl"] for c in closed)
    edge = edge_confidence(pnls)
    mc = monte_carlo(pnls, equity_now, start_sol, max_drawdown_pct, horizon, sims)
    gates = gate_audit or []
    return {
        "generated_at": time.time(), "kpis": k, "drawdown": drawdown(equity_hist, observed_max_dd_pct),
        "by_source": by_source, "by_exit": by_exit, "by_hour": by_hour, "by_day": by_day,
        "by_score": by_score, "by_p": by_p, "live_calibration": live_calibration(trades),
        "pnl_hist": _hist([c["pnl_pct"] for c in trades], PNL_BINS),
        "hold_hist": _hist([c["closed"] - c["opened"] for c in trades], HOLD_BINS),
        "scatter": [{"hold_s": round(c["closed"] - c["opened"], 1), "pnl_pct": round(c["pnl_pct"], 1),
                     "peak_pct": round(c.get("peak_gain_pct", 0), 1), "source": c["source"].split(":")[0],
                     "symbol": c.get("symbol", ""), "mint": c.get("mint", "")} for c in trades[-400:]],
        "edge": edge, "monte_carlo": mc, "gate_audit": gates, "model": model_card,
        "callouts": {"n": len(bags), "pnl_sol": _f(sum(c["pnl"] for c in bags)),
                     "avg_pnl_pct": _f(sum(c["pnl_pct"] for c in bags) / len(bags), 1) if bags else None},
        "insights": insights(k, trades, by_source, by_exit, by_hour, gates, edge, mc),
    }
