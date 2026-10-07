"""Static performance report: one self-contained HTML file (inline SVG, no scripts) plus trades.csv.

    python -m meme_trader.sniper report                      # paper/live trades in data/trades-*.jsonl
    python -m meme_trader.sniper report --backtest --file data/feed-*

Made to be shared: it opens anywhere, and says plainly where its numbers come from.
"""
from __future__ import annotations

import csv
import html
import json
import math
import time
from pathlib import Path

from .analytics import compute

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--surface-2:#f3f2ee;--ink:#0b0b0b;--ink-2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--series:#2a78d6;--series-soft:rgba(42,120,214,.14);
--good:#006300;--good-mark:#0ca30c;--bad:#d03b3b;--warn:#b07a00;--accent:#4a3aa7}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--surface-2:#232321;
--ink:#fff;--ink-2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
--series:#3987e5;--series-soft:rgba(57,135,229,.18);--good:#0ca30c;--good-mark:#0ca30c;--bad:#e66767;--warn:#fab219;--accent:#9085e9}}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px;display:grid;gap:16px}
h1{margin:0;font-size:24px}h2{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-2)}
.sub{color:var(--ink-2)}.muted{color:var(--muted)}.num,table{font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi,.panel{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:12px 14px;min-width:0}
.kpi .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.kpi .v{font-size:22px;font-weight:650}
.kpi .s{color:var(--ink-2);font-size:12px}.up{color:var(--good)}.down{color:var(--bad)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:800px){.two{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:right;padding:5px 6px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:500;font-size:12px}
.ins{list-style:none;margin:0;padding:0;display:grid;gap:6px}.ins li{padding:8px 10px;border-radius:8px;background:var(--surface-2);border-left:3px solid var(--axis)}
.ins .good{border-left-color:var(--good-mark)}.ins .bad{border-left-color:var(--bad)}.ins .warn{border-left-color:var(--warn)}
svg{width:100%;height:auto;display:block}svg text{fill:var(--muted);font-size:11px}
.note{font-size:12px;color:var(--muted)}.tablewrap{overflow-x:auto}
"""


def _n(v, nd=2, signed=False, pct=False, inf="∞") -> str:
    if v is None:
        return "–"
    if isinstance(v, str):
        return inf if v == "Infinity" else v
    if isinstance(v, float) and math.isinf(v):
        return inf
    s = f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"
    return s + ("%" if pct else "")


def _cls(v) -> str:
    return "" if not isinstance(v, (int, float)) or v == 0 else ("up" if v > 0 else "down")


def _scale(lo, hi, a, b):
    span = (hi - lo) or 1.0
    return lambda v: a + (v - lo) / span * (b - a)


def svg_equity(series: list[dict], w=1000, h=260) -> str:
    if len(series) < 2:
        return '<p class="muted">Not enough history for a chart yet.</p>'
    pad_l, pad_r, pad_t, pad_b = 54, 10, 10, 22
    ts = [p["ts"] for p in series]
    eq = [p["equity"] for p in series]
    lo, hi = min(eq), max(eq)
    pad = (hi - lo) * 0.05 if hi - lo > 1e-9 else 0.01
    lo, hi = lo - pad, hi + pad
    x = _scale(ts[0], ts[-1], pad_l, w - pad_r)
    y = _scale(lo, hi, h - pad_b, pad_t)
    pts = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in zip(ts, eq))
    peak, under = eq[0], []
    for t, v in zip(ts, eq):            # shade the gap between running peak and equity = drawdown
        peak = max(peak, v)
        under.append((x(t), y(peak), y(v)))
    dd = " ".join(f"{a:.1f},{b:.1f}" for a, b, _ in under) + " " + " ".join(f"{a:.1f},{c:.1f}" for a, _, c in reversed(under))
    grid = "".join(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.2f}</text>'
                   for v in [lo + (hi - lo) * i / 4 for i in range(5)])
    t0, t1 = time.strftime("%m-%d %H:%M", time.gmtime(ts[0])), time.strftime("%m-%d %H:%M", time.gmtime(ts[-1]))
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Equity curve">{grid}'
            f'<polygon points="{dd}" fill="var(--bad)" opacity=".16"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--series)" stroke-width="2"/>'
            f'<text x="{pad_l}" y="{h - 4}">{t0} UTC</text><text x="{w - pad_r}" y="{h - 4}" text-anchor="end">{t1} UTC</text></svg>')


def svg_fan(mc: dict, w=1000, h=240) -> str:
    fan = mc["fan"]
    pad_l, pad_r, pad_t, pad_b = 54, 10, 10, 22
    lo = min(p["p5"] for p in fan + [{"p5": mc["kill_level"]}])
    hi = max(p["p95"] for p in fan)
    x = _scale(0, fan[-1]["trade"], pad_l, w - pad_r)
    y = _scale(lo, hi, h - pad_b, pad_t)

    def band(a, b):
        top = " ".join(f"{x(p['trade']):.1f},{y(p[b]):.1f}" for p in fan)
        bot = " ".join(f"{x(p['trade']):.1f},{y(p[a]):.1f}" for p in reversed(fan))
        return f"{top} {bot}"
    med = " ".join(f"{x(p['trade']):.1f},{y(p['p50']):.1f}" for p in fan)
    grid = "".join(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.2f}</text>'
                   for v in [lo + (hi - lo) * i / 4 for i in range(5)])
    kill = (f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(mc["kill_level"]):.1f}" y2="{y(mc["kill_level"]):.1f}" '
            f'stroke="var(--bad)" stroke-dasharray="4 4"/><text x="{w - pad_r}" y="{y(mc["kill_level"]) - 4:.1f}" '
            f'text-anchor="end">kill switch</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Projected equity range">{grid}'
            f'<polygon points="{band("p5", "p95")}" fill="var(--series)" opacity=".14"/>'
            f'<polygon points="{band("p25", "p75")}" fill="var(--series)" opacity=".26"/>'
            f'<polyline points="{med}" fill="none" stroke="var(--series)" stroke-width="2"/>{kill}'
            f'<text x="{pad_l}" y="{h - 4}">now</text><text x="{w - pad_r}" y="{h - 4}" text-anchor="end">'
            f'+{fan[-1]["trade"]} trades</text></svg>')


def svg_hist(hist: list[dict], w=500, h=200, signed_bins=True) -> str:
    if not any(b["n"] for b in hist):
        return '<p class="muted">No trades yet.</p>'
    pad_b, pad_t = 34, 18
    m = max(b["n"] for b in hist)
    bw = w / len(hist)
    bars = []
    for i, b in enumerate(hist):
        bh = (h - pad_b - pad_t) * b["n"] / m
        neg = signed_bins and (b["bin"].startswith("<") or b["bin"].startswith("-"))
        label = b["bin"].split("..")[0]                 # lower edge keeps narrow bars legible
        color = "var(--bad)" if neg else "var(--good-mark)" if signed_bins else "var(--series)"
        bars.append(f'<rect x="{i * bw + 2:.1f}" y="{h - pad_b - bh:.1f}" width="{bw - 4:.1f}" height="{bh:.1f}" '
                    f'rx="2" fill="{color}" opacity=".85"><title>{html.escape(b["bin"])}: {b["n"]}</title></rect>'
                    f'<text x="{i * bw + bw / 2:.1f}" y="{h - pad_b + 13}" text-anchor="middle">{html.escape(label)}</text>'
                    + (f'<text x="{i * bw + bw / 2:.1f}" y="{h - pad_b - bh - 3:.1f}" text-anchor="middle">{b["n"]}</text>' if b["n"] else ""))
    return f'<svg viewBox="0 0 {w} {h}" role="img">{"".join(bars)}</svg>'


def _table(rows: list[dict], cols: list[tuple[str, str, callable]]) -> str:
    if not rows:
        return '<p class="muted">Nothing yet.</p>'
    head = "".join(f"<th>{html.escape(c[0])}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{c[2](r)}</td>" for c in cols) + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


GROUP_COLS = [
    ("", "key", lambda r: html.escape(str(r["key"]))),
    ("Trades", "n", lambda r: r["n"]),
    ("Win", "win_rate", lambda r: f"{r['win_rate']:.0%}"),
    ("Avg", "avg", lambda r: f'<span class="{_cls(r["avg_pnl_pct"])}">{_n(r["avg_pnl_pct"], 1, True, True)}</span>'),
    ("PF", "pf", lambda r: _n(r["profit_factor"], 2)),
    ("Hit 2x", "x2", lambda r: f"{r['reached_2x']:.0%}"),
    ("P&L SOL", "pnl", lambda r: f'<span class="{_cls(r["pnl_sol"])}">{_n(r["pnl_sol"], 3, True)}</span>'),
]


def render(a: dict, title: str, source: str) -> str:
    k = a["kpis"]
    kp = []

    def card(label, value, sub="", cls=""):
        kp.append(f'<div class="kpi"><div class="l">{label}</div><div class="v num {cls}">{value}</div>'
                  f'<div class="s">{sub}</div></div>')
    if k.get("n"):
        card("Net P&L", f"{_n(k['pnl_sol'], 3, True)} SOL", f"{_n(k['return_pct'], 1, True, True)} on start", _cls(k["pnl_sol"]))
        card("Trades", k["n"], f"{k['wins']} wins · {k['losses']} losses")
        card("Win rate", f"{k['win_rate']:.0%}", f"break-even {_n((k.get('breakeven_win_rate') or 0) * 100, 0)}%")
        card("Profit factor", _n(k["profit_factor"]), f"payoff {_n(k['payoff'])}")
        card("Expectancy", f"{_n(k['expectancy_pct'], 1, True, True)}", f"{_n(k['expectancy_sol'], 4, True)} SOL / trade",
             _cls(k["expectancy_pct"]))
        card("Max drawdown", f"{_n(a['drawdown']['max_pct'], 1)}%", f"now {_n(a['drawdown']['current_pct'], 1)}%")
        card("Best / worst", f"{_n(k['best_pct'], 0, True)}% / {_n(k['worst_pct'], 0, True)}%", f"median {_n(k['median_pct'], 0, True)}%")
        card("Median hold", f"{_n(k['median_hold_s'], 0)}s", f"{_n(k['trades_per_hour'], 1)} trades/h")
        card("SQN", _n(k["sqn"]), "system quality (n capped at 100)")
        card("Fees (est.)", f"{_n(k['fees_est_sol'], 3)} SOL", f"on {_n(k['volume_sol'], 2)} SOL bought")
    ins = "".join(f'<li class="{i["level"]}">{html.escape(i["text"])}</li>' for i in a["insights"])
    mc = a.get("monte_carlo")
    mc_html = (svg_fan(mc) + f'<p class="note">{mc["sims"]} block-bootstrap paths of the next {mc["horizon"]} trades, '
               f'drawn from the trades above. Profit in {mc["p_profit"]:.0%} of paths, kill switch in '
               f'{mc["p_kill_switch"]:.0%}; median worst drawdown {mc["median_max_dd_pct"]}%. '
               f'A range of outcomes if the future looks like this sample, not a forecast.</p>') if mc else \
        '<p class="muted">Needs at least 10 closed trades.</p>'
    gate_cols = [("Decision", "", lambda r: html.escape(r["gate"])), ("Tokens", "", lambda r: r["n"]),
                 ("Doubled before -30%", "", lambda r: f"{r['win_first_pct']:.0f}%"),
                 ("-30% before doubling", "", lambda r: f"{r['loss_first_pct']:.0f}%"),
                 ("Neither", "", lambda r: f"{r['flat_pct']:.0f}%"),
                 ("Avg peak", "", lambda r: f"{r['avg_peak_x']:.2f}x")]
    model = a.get("model")
    model_html = '<p class="muted">No model trained (python -m meme_trader.sniper train).</p>'
    if model:
        model_html = (f'<p>Trained {html.escape(str(model.get("trained_at")))} on {model.get("n_train")} samples; '
                      f'label: {html.escape(str(model.get("label")))}. Held-out AUC <b>{_n(model.get("auc"), 3)}</b>, '
                      f'Brier skill {_n(model.get("brier_skill"), 3)}, top-decile hit rate '
                      f'{_n((model.get("top_decile_rate") or 0) * 100, 0)}% vs base {_n((model.get("base_rate") or 0) * 100, 0)}%.</p>')
    cal = a.get("live_calibration") or []
    cal_html = _table(cal, [("P(2x) at entry", "", lambda r: r["bin"]), ("Trades", "", lambda r: r["n"]),
                            ("Predicted", "", lambda r: f"{r['predicted']:.0%}"),
                            ("Reached 2x", "", lambda r: f"{r['realized_2x']:.0%}")]) if cal else ""
    co = a.get("callouts") or {}
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body><main>
<div><h1>{html.escape(title)}</h1><div class="sub">{html.escape(source)} · generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}</div></div>
<div class="kpis">{''.join(kp) or '<p class="muted">No closed trades.</p>'}</div>
<section class="panel"><h2>Highlights</h2><ul class="ins">{ins}</ul></section>
<section class="panel"><h2>Equity (SOL) · red = drawdown from the running peak</h2>{svg_equity(a['drawdown']['series'])}</section>
<section class="panel"><h2>Next {mc['horizon'] if mc else 100} trades: projected equity range</h2>{mc_html}</section>
<div class="two"><section class="panel"><h2>P&amp;L per trade (%)</h2>{svg_hist(a['pnl_hist'], signed_bins=True)}</section>
<section class="panel"><h2>Hold time (s)</h2>{svg_hist(a['hold_hist'], signed_bins=False)}</section></div>
<div class="two"><section class="panel"><h2>By strategy</h2>{_table(a['by_source'], GROUP_COLS)}</section>
<section class="panel"><h2>By exit</h2>{_table(a['by_exit'], GROUP_COLS)}</section></div>
<div class="two"><section class="panel"><h2>By entry score</h2>{_table(a['by_score'], GROUP_COLS)}</section>
<section class="panel"><h2>By model P(2x) at entry</h2>{_table(a['by_p'], GROUP_COLS)}{cal_html}</section></div>
<section class="panel"><h2>Gate audit: what the filters said no to</h2>{_table(a['gate_audit'], gate_cols)}
<p class="note">Every decision is followed for 10 minutes: had we bought at that moment, would the token have doubled
before falling 30%? "(bought)" is the same test on our own entries - the yardstick. A gate whose rejects double more
often than our buys is costing trades; one whose rejects rarely double is doing its job.</p></section>
<section class="panel"><h2>Prediction model</h2>{model_html}</section>
<section class="panel"><h2>Daily</h2>{_table(a['by_day'], GROUP_COLS)}</section>
<p class="note">Callout bags: {co.get('n', 0)} ($1 marketing buys, kept out of the numbers above), net
{_n(co.get('pnl_sol'), 4, True)} SOL. Fees are an estimate from the fee rate. Nothing here is financial advice:
most pump.fun tokens go to zero, and past results don't promise future ones.</p>
</main></body></html>"""


def write(closed: list[dict], equity_hist: list, start_sol: float, max_dd_pct: float, out: Path, title: str,
          source: str, gate_audit=None, model_card=None, fee_pct: float = 0.0) -> tuple[Path, Path]:
    a = compute(closed, equity_hist, start_sol, max_dd_pct, gate_audit, model_card, fee_pct=fee_pct, sims=2000)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(a, title, source))
    csv_path = out.with_suffix(".csv")
    cols = ["opened", "closed", "symbol", "mint", "source", "cost", "proceeds", "pnl", "pnl_pct", "peak_gain_pct",
            "mae_pct", "score", "p", "initials", "exit", "mode", "session", "config", "model"]
    with csv_path.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["opened_utc", "closed_utc"] + cols[2:])
        for c in closed:
            wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(c["opened"])),
                         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(c["closed"]))] + [c.get(k, "") for k in cols[2:]])
    return out, csv_path


def row_mode(c: dict) -> str:
    """paper / live / paper-synthetic (demo) ... Rows written before trades were tagged are 'unknown':
    they can't be told apart, so they're never mixed into paper or live results."""
    if c.get("mode"):
        return c["mode"]
    return "paper" if c.get("source") == "chains" else "unknown"   # (other chains only ever trade paper; 2026-10-05 rows)


def load_trades(data_dir: Path, days: int | None = None, mode: str | None = None,
                session: str | None = None) -> list[dict]:
    """Recorded closed trades, optionally only one mode ('all' = every mode) and/or one session."""
    files = sorted(data_dir.glob("trades-*.jsonl"))
    if days:
        files = files[-days:]
    from .pnl import upgrade
    rows = []
    for p in files:
        for line in p.read_text().splitlines():
            try:
                rows.append(upgrade(json.loads(line)))
            except ValueError:
                continue
    if mode and mode != "all":
        rows = [c for c in rows if row_mode(c) == mode]
    if session:
        rows = [c for c in rows if c.get("session") == session]
    return sorted(rows, key=lambda c: c["closed"])


def sessions(closed: list[dict]) -> list[tuple[str, float, int]]:
    """(session, its starting balance, trades) in order of first trade."""
    out: dict[str, list] = {}
    for c in closed:
        k = c.get("session") or "untagged"
        out.setdefault(k, [c.get("start_sol"), 0])[1] += 1
    return [(k, v[0], v[1]) for k, v in out.items()]


def equity_from_trades(closed: list[dict], start_sol: float) -> list[tuple[float, float]]:
    eq, out = start_sol, []
    if closed:
        out.append((closed[0]["opened"], start_sol))
    for c in closed:
        eq += c["pnl"]
        out.append((c["closed"], eq))
    return out
