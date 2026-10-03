"""Research harness: evaluate ONE written-down strategy honestly, on data it was never tuned on.

    python -m meme_trader.sniper research data                    what's recorded and how clean it is
    python -m meme_trader.sniper research eval graduation-v1      development data: the full report
    python -m meme_trader.sniper research freeze graduation-v1    lock the rules; only LATER data is the holdout
    python -m meme_trader.sniper research final graduation-v1     the holdout, judged once by the policy's gates
    python -m meme_trader.sniper research log                     every experiment run so far

A policy (research/policies/<name>.yaml) is a strategy's thesis, its exact settings (applied on top of
config/params.example.yaml - never your local params.yaml, so results are reproducible) and pass/fail
gates declared in advance. Its hash covers every setting that can change a trade.

The protocol:
  * `eval` runs on development data and reports net results after all modelled costs, per-day results with
    day-block bootstrap uncertainty, how much depends on the biggest winners, sensitivity to fill slippage,
    a profit-vs-size curve, a no-filter baseline, a random-entry baseline and one ablation per filter.
  * Every run is appended to data/research/experiments.jsonl, so the number of variants tried on the same
    data is known: the best of many tries is optimistically biased.
  * `freeze` stamps the policy with its hash and the time. Data recorded after that is the holdout. Once
    frozen, `eval` only reads data from before the freeze; changing a setting means a new version.
  * `final` shows nothing until the holdout has the policy's minimum days and trades - no peeking - and
    then judges it once against the gates. Later calls print the stored verdict.
"""
from __future__ import annotations

import calendar
import gzip
import hashlib
import json
import math
import random
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from ..config import EXAMPLE, ROOT, ConfigError, load
from ..journal import DATA
from .curve import CURVE_TOKENS, INITIAL_V_TOKENS
from .events import Health, Launch, Tick, Trade, loads

POLICY_DIR = ROOT / "research" / "policies"
# settings that can't change a trade (endpoints, UI, notifications, the AI operator) stay out of the hash
NOT_HASHED = ("feed", "agent", "chat", "notify")
SLIPPAGES = (0.0, 1.5, 5.0, 8.0)
SCAN_INTERVALS = (1.0, 3.0)          # timing sensitivity: when the scanner happens to look
SIZES_USD = (5, 10, 50, 100, 250)
ABLATIONS = {
    "no net-flow filter": ["late.min_net_flow_sol=-1000000"],
    "no buyer-count filter": ["late.min_buyers=0"],
    "no buy/sell-ratio filter": ["late.min_buy_sell_ratio=0"],
    "no near-high filter": ["late.min_near_high=0"],
    "Mayhem agent counted as demand": ["market.non_organic_wallets=[]"],
}


def registry_path() -> Path:
    return DATA / "research" / "experiments.jsonl"


# ------------------------------------------------------------------ policies
def policy_path(name: str) -> Path:
    p = Path(name)
    return p if p.suffix in (".yaml", ".yml") and p.exists() else POLICY_DIR / f"{name}.yaml"


def load_policy(name: str) -> dict:
    path = policy_path(name)
    if not path.exists():
        raise ConfigError(f"no policy {path} (see research/policies/)")
    pol = yaml.safe_load(path.read_text()) or {}
    pol["_path"] = str(path)
    pol.setdefault("name", path.stem)
    pol.setdefault("overrides", {})
    pol.setdefault("gates", {})
    return pol


def policy_params(pol: dict, extra: list[str] | None = None):
    """config/params.example.yaml + the policy's overrides (+ a variant's), validated."""
    from .__main__ import apply_overrides

    params = load(EXAMPLE)
    sets = [f"{k}={json.dumps(v)}" for k, v in (pol.get("overrides") or {}).items()] + list(extra or [])
    return apply_overrides(params, sets) if sets else params


def policy_hash(params) -> str:
    sn = {k: v for k, v in params["sniper"].items() if k not in NOT_HASHED}
    return hashlib.sha256(json.dumps(sn, sort_keys=True, default=str).encode()).hexdigest()[:12]


def signature(pol: dict) -> str:
    """What freezing locks: every trading setting AND the yardstick - the gates and the operating costs - so
    neither the strategy nor the pass mark can move after the holdout has been seen."""
    judged = json.dumps({"gates": pol.get("gates") or {}, "ops": pol.get("ops_cost_usd_per_day") or 0}, sort_keys=True)
    return hashlib.sha256((policy_hash(policy_params(pol)) + judged).encode()).hexdigest()[:12]


def freeze(name: str, now: float | None = None) -> dict:
    pol = load_policy(name)
    if pol.get("frozen_at"):
        raise ConfigError(f"{pol['name']} is already frozen ({pol['frozen_at']}). A change means a new version: "
                          f"copy it to a new name and edit that.")
    h = signature(pol)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    path = Path(pol["_path"])
    text = path.read_text()
    for key, val in (("frozen_at", at), ("frozen_hash", h)):
        text, n = re.subn(rf"(?m)^{key}:.*$", f'{key}: "{val}"', text)
        if not n:
            text += f"\n{key}: {val}\n"
    path.write_text(text)
    log_experiment({"kind": "freeze", "policy": pol["name"], "hash": h, "frozen_at": at})
    return {"policy": pol["name"], "frozen_at": at, "hash": h}


def frozen_ts(pol: dict) -> float | None:
    at = pol.get("frozen_at")
    if not at:
        return None
    if hasattr(at, "timestamp"):                 # YAML turns an unquoted ISO time into a datetime
        return float(at.timestamp())
    return float(calendar.timegm(time.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ")))


# ------------------------------------------------------------------ data
def feed_files(files: list[str] | None = None) -> list[Path]:
    if files:
        return [Path(f) for f in files]
    return sorted(DATA.glob("feed-*.jsonl*"))


def _lines(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        for line in f:
            if line.strip():
                yield line


def _progress(v_tokens: float) -> float:
    return min(max((INITIAL_V_TOKENS - v_tokens) / CURVE_TOKENS, 0.0), 1.0) * 100


def load_events(files: list[Path], start: float | None = None, end: float | None = None,
                min_progress_pct: float | None = None, launched_after: float | None = None) -> tuple[list, dict]:
    """Events from recordings, time-filtered, plus data-quality stats.

    min_progress_pct: keep the trades of tokens whose curve ever got that far (a graduation strategy can't
    touch the rest). A dropped event that would have started one of the engine's 1-second ticks becomes a
    Tick at the same moment, so scans and time-based exits run on exactly the full replay's clock.
    launched_after: drop tokens launched before this (the holdout must not inherit earlier tokens)."""
    stats = {"files": [p.name for p in files], "events": 0, "launches": 0, "trades": 0, "non_organic": 0,
             "chain_ts": 0, "lags": [], "fee_bps": Counter(), "health": 0, "degraded_min": 0, "sol_usd": [],
             "first": None, "last": None}
    from .tracker import MAYHEM_AGENT

    events, peak, born = [], defaultdict(float), {}
    for path in files:
        for line in _lines(path):
            try:
                e = loads(line)
            except (ValueError, KeyError, TypeError):
                continue
            if (start is not None and e.ts < start) or (end is not None and e.ts >= end):
                continue
            stats["events"] += 1
            stats["first"] = e.ts if stats["first"] is None else min(stats["first"], e.ts)
            stats["last"] = e.ts if stats["last"] is None else max(stats["last"], e.ts)
            if isinstance(e, Launch):
                stats["launches"] += 1
                born.setdefault(e.mint, e.ts)
            elif isinstance(e, Trade):
                stats["trades"] += 1
                stats["non_organic"] += e.trader == MAYHEM_AGENT
                if e.chain_ts:
                    stats["chain_ts"] += 1
                    if len(stats["lags"]) < 200_000:
                        stats["lags"].append(e.ts - e.chain_ts)
                if e.fee_bps >= 0:
                    stats["fee_bps"][(e.fee_bps, e.creator_fee_bps)] += 1
                if e.pool == "pump" and e.v_tokens > 0:
                    peak[e.mint] = max(peak[e.mint], _progress(e.v_tokens))
            elif isinstance(e, Health):
                stats["health"] += 1
                stats["degraded_min"] += bool(e.degraded)
                if e.sol_usd > 0:
                    stats["sol_usd"].append(e.sol_usd)
            events.append(e)
    events.sort(key=lambda e: e.ts)
    if min_progress_pct is not None or launched_after is not None:
        def keep(e) -> bool:
            m = getattr(e, "mint", None)
            if m is None or isinstance(e, Launch):          # launches stay: serial-deployer context
                return True
            if launched_after is not None and born.get(m, -1e18) < launched_after:
                return False                                # a token from before the cut: not the holdout's
            if isinstance(e, Trade) and min_progress_pct is not None:
                return peak.get(m, 0.0) >= min_progress_pct
            return True
        kept, last_tick = [], -1e18
        for e in events:
            ticks = e.ts - last_tick >= 1                   # Engine._handle: a tick when >= 1 s since the last
            if ticks:
                last_tick = e.ts
            if keep(e):
                kept.append(e)
            elif ticks:
                kept.append(Tick(e.ts))
        events = kept
    stats["kept_events"] = len(events)
    return events, stats


def quality_summary(st: dict) -> dict:
    lags = sorted(st["lags"])
    q = (lambda p: round(lags[int(p * (len(lags) - 1))], 1)) if lags else (lambda p: None)
    span_h = (st["last"] - st["first"]) / 3600 if st["first"] else 0.0
    fees = st["fee_bps"].most_common(3)
    return {"files": st["files"], "span_hours": round(span_h, 1), "launches": st["launches"], "trades": st["trades"],
            "non_organic_pct": round(100 * st["non_organic"] / max(st["trades"], 1), 1),
            "chain_time_pct": round(100 * st["chain_ts"] / max(st["trades"], 1), 1),
            "lag_p50_s": q(0.5), "lag_p90_s": q(0.9), "lag_p99_s": q(0.99),
            "fee_bps_top": [f"{a}+{b} bps x{n}" for (a, b), n in fees],
            "health_minutes": st["health"], "degraded_minutes": st["degraded_min"],
            "sol_usd": round(sum(st["sol_usd"]) / len(st["sol_usd"]), 2) if st["sol_usd"] else None}


# ------------------------------------------------------------------ running variants
_W: dict = {}


def _init_worker(events: list) -> None:
    _W["events"] = events


def _run_variant(job: tuple) -> dict:
    import asyncio

    from .__main__ import run_backtest
    from .sweep import MemoryFeed

    label, pol, extra = job
    params = policy_params(pol, extra)
    eng = asyncio.run(run_backtest(params, MemoryFeed(_W["events"])))
    trades = [{k: c.get(k) for k in ("mint", "symbol", "opened", "closed", "cost", "pnl", "pnl_pct", "exit",
                                     "peak_gain_pct", "source")} for c in eng.book.closed]
    return {"label": label, "extra": extra, "trades": trades, "hash": policy_hash(params),
            "sol_usd": eng.sol_price.usd, "launches": eng.stats["launches"]}


def run_variants(pol: dict, events: list, jobs_list: list[tuple], jobs: int = 0) -> dict[str, dict]:
    import multiprocessing as mp
    import os

    n = jobs or max(1, min((os.cpu_count() or 2) - 1, len(jobs_list), 10))
    if n == 1:
        _init_worker(events)
        return {j[0]: _run_variant(j) for j in jobs_list}
    ctx = mp.get_context("fork")             # workers inherit the loaded events instead of re-reading them
    _init_worker(events)
    with ctx.Pool(n) as pool:
        return {r["label"]: r for r in pool.imap_unordered(_run_variant, jobs_list)}


# ------------------------------------------------------------------ statistics
def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def block_bootstrap(trades: list[dict], span: tuple[float, float], block_s: float, sims: int = 5000,
                    seed: int = 7) -> dict | None:
    """Resample whole time blocks (days, or hours while there are few days): trades in one block share a
    market, so they are not independent draws. Returns the mean P&L per block with a 90% interval."""
    if span[1] <= span[0]:
        return None
    nblocks = max(1, math.ceil((span[1] - span[0]) / block_s))
    sums = [0.0] * nblocks
    for t in trades:
        sums[min(int((t["opened"] - span[0]) // block_s), nblocks - 1)] += t["pnl"]
    if nblocks < 3:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(sums) for _ in range(nblocks)) / nblocks for _ in range(sims))
    return {"blocks": nblocks, "block_hours": round(block_s / 3600, 1), "mean_sol": round(sum(sums) / nblocks, 5),
            "lo90_sol": round(means[int(0.05 * sims)], 5), "hi90_sol": round(means[int(0.95 * sims)], 5),
            "p_positive": round(sum(m > 0 for m in means) / sims, 3)}


def winner_dependence(trades: list[dict]) -> dict:
    pnls = sorted((t["pnl"] for t in trades), reverse=True)
    total = sum(pnls)
    gross = sum(p for p in pnls if p > 0)
    return {"total_sol": round(total, 4), **{f"ex_top{k}_sol": round(total - sum(pnls[:k]), 4) for k in (1, 3, 5, 10)},
            "top5_share_of_gross_profit": round(sum(p for p in pnls[:5] if p > 0) / gross, 3) if gross else None}


def basic(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    return {"trades": n, "pnl_sol": round(sum(t["pnl"] for t in trades), 4),
            "per_trade_sol": round(sum(t["pnl"] for t in trades) / n, 5) if n else None,
            "win_rate": round(len(wins) / n, 3) if n else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "cost_sol": round(sum(t["cost"] or 0 for t in trades), 3),
            "return_on_cost_pct": round(100 * sum(t["pnl"] for t in trades) / max(sum(t["cost"] or 0 for t in trades),
                                                                                    1e-9), 2) if n else None}


def random_baseline(rule_pnl: float, n: int, pool: list[dict], sims: int = 4000, seed: int = 11) -> dict | None:
    """Would picking the same number of trades at random from the no-filter baseline do as well?"""
    if n == 0 or len(pool) < n:
        return None
    rng = random.Random(seed)
    xs = [t["pnl"] for t in pool]
    tot = sorted(sum(rng.sample(xs, n)) for _ in range(sims))
    return {"n": n, "pool": len(pool), "median_sol": round(tot[sims // 2], 4),
            "p95_sol": round(tot[int(0.95 * sims)], 4),
            "rule_percentile": round(sum(x < rule_pnl for x in tot) / sims, 3)}


def breakeven_slippage(points: list[tuple[float, float]]) -> float | None:
    """Per-side slippage % at which net P&L crosses zero (linear between measured points)."""
    pts = sorted(points)
    for (s0, p0), (s1, p1) in zip(pts, pts[1:]):
        if p0 > 0 >= p1:
            return round(s0 + (s1 - s0) * p0 / (p0 - p1), 2)
    if pts and all(p <= 0 for _, p in pts):
        return 0.0
    return None


# ------------------------------------------------------------------ evaluation
def evaluate(pol: dict, events: list, quality: dict, quick: bool = False, jobs: int = 0) -> dict:
    base_params = policy_params(pol)
    slip0 = float(base_params["sniper"]["execution"]["paper_latency_slippage_pct"])
    usd0 = float(base_params["sniper"]["sizing"]["base_usd"])
    jl = [("policy", pol, [])]
    if not quick:
        jl += [(f"slippage {s:g}%", pol, [f"execution.paper_latency_slippage_pct={s}"])
               for s in SLIPPAGES if s != slip0]
        cap = base_params["sniper"]["capital"]     # bigger trades need a proportionally bigger budget and loss limit
        jl += [(f"size ${u}", pol, [f"sizing.base_usd={u}", f"sizing.max_usd={u}",
                                     f"capital.starting_sol={max(1.0, u / usd0) * float(cap['starting_sol'])}",
                                     f"capital.daily_loss_limit_sol={u / usd0 * float(cap['daily_loss_limit_sol'])}"])
               for u in SIZES_USD if u != usd0]
        jl += [(f"scan every {i:g}s", pol, [f"late.scan_interval_s={i}"]) for i in SCAN_INTERVALS]
        jl += [("baseline: no momentum filter", pol, ["late.entry_mode=window"])]
        jl += [(f"ablation: {k}", pol, v) for k, v in ABLATIONS.items()]
    res = run_variants(pol, events, jl, jobs)
    main = res["policy"]
    trades = main["trades"]
    first = min((e.ts for e in events), default=0.0)
    last = max((e.ts for e in events), default=0.0)
    days = (last - first) / 86400
    sol_usd = quality.get("sol_usd") or main["sol_usd"]
    ops_usd = float(pol.get("ops_cost_usd_per_day") or 0)
    out = {"policy": pol["name"], "hash": main["hash"], "frozen": bool(pol.get("frozen_at")), "data": quality,
           "days": round(days, 2), "sol_usd": sol_usd, "result": basic(trades)}
    r = out["result"]
    r["pnl_usd"] = round(r["pnl_sol"] * sol_usd, 2)
    r["ops_cost_sol"] = round(ops_usd * days / sol_usd, 4)
    r["net_after_ops_sol"] = round(r["pnl_sol"] - r["ops_cost_sol"], 4)
    per_day = defaultdict(lambda: [0, 0.0])
    for t in trades:
        d = per_day[_day(t["opened"])]
        d[0] += 1
        d[1] += t["pnl"]
    out["per_day"] = {k: {"trades": v[0], "pnl_sol": round(v[1], 4)} for k, v in sorted(per_day.items())}
    block_s = 86400 if days >= 5 else 4 * 3600
    out["uncertainty"] = block_bootstrap(trades, (first, last), block_s)
    out["winners"] = winner_dependence(trades)
    ex = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = " ".join(str(t["exit"]).split(" ")[:2])
        ex[k][0] += 1
        ex[k][1] += t["pnl"]
    out["by_exit"] = {k: {"n": v[0], "pnl_sol": round(v[1], 4)} for k, v in sorted(ex.items(), key=lambda x: -x[1][1])}
    if not quick:
        slip = [(slip0, r["pnl_sol"])] + [(float(x["extra"][0].split("=")[1]), basic(x["trades"])["pnl_sol"])
                                          for k, x in res.items() if k.startswith("slippage")]
        out["slippage"] = {f"{s:g}%": p for s, p in sorted(slip)}
        out["breakeven_slippage_pct"] = breakeven_slippage(slip)
        size = [(usd0, r)] + [(float(x["extra"][0].split("=")[1]), basic(x["trades"]))
                              for k, x in res.items() if k.startswith("size")]
        out["size_curve"] = {f"${u:g}": {"trades": b["trades"], "pnl_sol": b["pnl_sol"],
                                         "return_on_cost_pct": b["return_on_cost_pct"]} for u, b in sorted(size)}
        out["timing"] = {"every 2s (policy)": r["pnl_sol"], **{k.removeprefix("scan ").replace("every", "every"):
                         basic(x["trades"])["pnl_sol"] for k, x in res.items() if k.startswith("scan every")}}
        win = res["baseline: no momentum filter"]["trades"]
        out["baseline_no_filter"] = basic(win)
        out["random_entry"] = random_baseline(r["pnl_sol"], r["trades"], win)
        out["ablations"] = {k.removeprefix("ablation: "): basic(x["trades"]) for k, x in res.items()
                            if k.startswith("ablation")}
    return out


def gates_check(pol: dict, rep: dict) -> dict:
    g = pol.get("gates") or {}
    r = rep["result"]
    checks = {
        "enough days": (rep["days"] >= g.get("min_days", 14), f"{rep['days']:.1f} of {g.get('min_days', 14)}"),
        "enough trades": (r["trades"] >= g.get("min_trades", 150), f"{r['trades']} of {g.get('min_trades', 150)}"),
    }
    u = rep.get("uncertainty")
    checks["positive with confidence"] = (bool(u) and u["p_positive"] >= g.get("min_p_positive", 0.9),
                                          f"P(mean > 0) = {u['p_positive'] if u else 'n/a'}")
    checks["net of operating costs"] = (r["net_after_ops_sol"] > 0, f"{r['net_after_ops_sol']:+.4f} SOL")
    ex3 = rep["winners"]["ex_top3_sol"]
    checks["not just 3 lucky trades"] = (ex3 >= g.get("min_pnl_ex_top3_sol", 0.0), f"{ex3:+.4f} SOL without the top 3")
    s = g.get("min_breakeven_slippage_pct", 5)
    be = rep.get("breakeven_slippage_pct")
    shown = be if be is not None else "> tested"
    checks["survives worse fills"] = (be is None or be >= s, f"break-even slippage {shown}% (needs {s}%)")
    return {"pass": all(ok for ok, _ in checks.values()),
            "checks": {k: {"ok": ok, "value": v} for k, (ok, v) in checks.items()}}


# ------------------------------------------------------------------ registry
def _git() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def log_experiment(rec: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "git": _git(), **rec}
    with path.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def experiments() -> list[dict]:
    path = registry_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def variants_tried(files: list[str]) -> int:
    """Distinct settings evaluated on any of these recordings (the multiple-testing count)."""
    fs = set(files)
    return len({h for e in experiments() if e.get("kind") == "eval" and fs & set(e.get("files") or [])
                for h in e.get("hashes", [])})


# ------------------------------------------------------------------ commands
def cmd_data(files: list[str] | None = None) -> dict:
    out = {}
    for p in feed_files(files):
        _, st = load_events([p])
        out[p.name] = quality_summary(st)
    return out


def _span_for(pol: dict, holdout: bool) -> tuple[float | None, float | None]:
    ft = frozen_ts(pol)
    if holdout:
        if ft is None:
            raise ConfigError(f"{pol['name']} isn't frozen yet: run `research freeze {pol['name']}` first")
        return ft, None
    return None, ft


def _check_frozen_hash(pol: dict) -> str:
    h = signature(pol)
    if pol.get("frozen_hash") and h != pol["frozen_hash"]:
        raise ConfigError(f"{pol['name']} changed after it was frozen (signature {h} != {pol['frozen_hash']}): "
                          f"settings, gates or costs. Its holdout result would mean nothing: make a new version.")
    return h


def min_progress(pol: dict) -> float:
    return float(policy_params(pol)["sniper"]["late"]["min_curve_pct"]) - 5


def cmd_eval(name: str, files: list[str] | None = None, quick: bool = False, jobs: int = 0) -> dict:
    pol = load_policy(name)
    _check_frozen_hash(pol)
    start, end = _span_for(pol, holdout=False)
    paths = feed_files(files)
    events, st = load_events(paths, start, end, min_progress_pct=min_progress(pol))
    rep = evaluate(pol, events, quality_summary(st), quick=quick, jobs=jobs)
    rep["variants_tried_on_this_data"] = variants_tried([p.name for p in paths]) + 1
    log_experiment({"kind": "eval", "policy": pol["name"], "files": [p.name for p in paths], "quick": quick,
                    "hashes": [rep["hash"]], "result": rep["result"], "uncertainty": rep.get("uncertainty")})
    return rep


def final_path(pol: dict) -> Path:
    return DATA / "research" / f"final-{pol['name']}-{pol.get('frozen_hash')}.json"


def cmd_final(name: str, files: list[str] | None = None, jobs: int = 0) -> dict:
    pol = load_policy(name)
    _check_frozen_hash(pol)
    stored = final_path(pol)
    if stored.exists():
        return {"stored": True, **json.loads(stored.read_text())}
    start, _ = _span_for(pol, holdout=True)
    g = pol.get("gates") or {}
    paths = feed_files(files)
    events, st = load_events(paths, start, None, min_progress_pct=min_progress(pol), launched_after=start)
    q = quality_summary(st)
    days = q["span_hours"] / 24
    if days < g.get("min_days", 14):
        return {"verdict": None, "progress": f"holdout has {days:.1f} of {g.get('min_days', 14)} days - no results "
                                             f"shown until then (looking early would make it development data)",
                "data": q}
    rep = evaluate(pol, events, q, quick=False, jobs=jobs)
    if rep["result"]["trades"] < g.get("min_trades", 150):
        return {"verdict": None, "progress": f"{rep['result']['trades']} of {g.get('min_trades', 150)} trades so far"
                                             f" - no results shown until then", "data": q}
    rep["gates"] = gates_check(pol, rep)
    rep["verdict"] = "PASS" if rep["gates"]["pass"] else "FAIL"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps(rep, indent=1, default=str))
    log_experiment({"kind": "final", "policy": pol["name"], "hash": rep["hash"], "verdict": rep["verdict"],
                    "result": rep["result"], "files": [p.name for p in paths]})
    return rep


# ------------------------------------------------------------------ printing
def _fmt_sol(x) -> str:
    return "–" if x is None else f"{x:+.4f}"


def print_report(rep: dict) -> None:
    d, r = rep["data"], rep["result"]
    print(f"\n=== {rep['policy']}  (hash {rep['hash']}{', FROZEN' if rep['frozen'] else ''}) ===")
    print(f"data: {', '.join(d['files'])} | {d['span_hours']} h | {d['launches']} launches, {d['trades']} trades "
          f"({d['non_organic_pct']}% non-organic)")
    lag = f"lag p50 {d['lag_p50_s']}s / p99 {d['lag_p99_s']}s" if d["lag_p50_s"] is not None else "no chain times"
    print(f"      {lag} | health records {d['health_minutes']} min ({d['degraded_minutes']} degraded) | "
          f"fees {', '.join(d['fee_bps_top']) or 'not recorded'}")
    print(f"\nRESULT  {r['trades']} trades | net {_fmt_sol(r['pnl_sol'])} SOL "
          f"(${r['pnl_usd']:+.2f} at ${rep['sol_usd']:.0f}) "
          f"| per trade {_fmt_sol(r['per_trade_sol'])} | win {r['win_rate']} | PF {r['profit_factor']} "
          f"| return on cost {r['return_on_cost_pct']}%")
    print(f"        after operating costs {_fmt_sol(r['net_after_ops_sol'])} SOL. SOL P&L is already the result versus "
          f"simply holding the SOL.")
    u = rep.get("uncertainty")
    print("UNCERTAINTY " + (f"{u['blocks']} blocks of {u['block_hours']} h: mean {u['mean_sol']:+.4f} SOL/block, "
                            f"90% interval [{u['lo90_sol']:+.4f}, {u['hi90_sol']:+.4f}], "
                            f"P(mean > 0) = {u['p_positive']}"
                            if u else "not enough independent blocks yet"))
    w = rep["winners"]
    print(f"WINNERS  total {w['total_sol']:+.4f} | without best 1 {w['ex_top1_sol']:+.4f} | 3 {w['ex_top3_sol']:+.4f} "
          f"| 5 {w['ex_top5_sol']:+.4f} | 10 {w['ex_top10_sol']:+.4f} "
          f"| top 5 = {w['top5_share_of_gross_profit']} of gross profit")
    print("PER DAY  " + "  ".join(f"{k}: {v['trades']} / {v['pnl_sol']:+.3f}" for k, v in rep["per_day"].items()))
    print("BY EXIT  " + "  ".join(f"{k} {v['n']}x {v['pnl_sol']:+.3f}" for k, v in rep["by_exit"].items()))
    if "slippage" in rep:
        print("SLIPPAGE " + "  ".join(f"{k}: {v:+.3f}" for k, v in rep["slippage"].items())
              + f"  -> break-even {rep['breakeven_slippage_pct']}% per side")
        print("SIZE     " + "  ".join(f"{k}: {v['trades']}t {v['pnl_sol']:+.3f} ({v['return_on_cost_pct']}%)"
                                     for k, v in rep["size_curve"].items()))
        print("TIMING   candidate scan " + "  ".join(f"{k}: {v:+.3f}" for k, v in rep["timing"].items())
              + "  (a real edge shouldn't care much when the scanner looks)")
        b = rep["baseline_no_filter"]
        print(f"BASELINE no momentum filter: {b['trades']} trades {b['pnl_sol']:+.4f} SOL "
              f"({b['return_on_cost_pct']}% on cost)")
        rb = rep.get("random_entry")
        if rb:
            print(f"RANDOM   {rb['n']} random picks from those {rb['pool']}: median {rb['median_sol']:+.4f}, 95th pct "
                  f"{rb['p95_sol']:+.4f} -> the rules beat {rb['rule_percentile']:.0%} of random picks")
        for k, v in rep["ablations"].items():
            print(f"ABLATION {k:<34} {v['trades']:>4} trades {v['pnl_sol']:+.4f} SOL")
    if "variants_tried_on_this_data" in rep:
        print(f"\n{rep['variants_tried_on_this_data']} setting(s) evaluated on this data so far: the best of many "
              f"tries looks better than it is.")
    if rep.get("gates"):
        print(f"\nVERDICT {rep['verdict']}")
        for k, v in rep["gates"]["checks"].items():
            print(f"  {'PASS' if v['ok'] else 'FAIL'}  {k}: {v['value']}")


def main(args) -> None:
    if args.rcmd == "data":
        for name, q in cmd_data(args.file).items():
            lag = f"lag p50 {q['lag_p50_s']}s p99 {q['lag_p99_s']}s" if q["lag_p50_s"] is not None else "no chain times"
            print(f"{name}: {q['span_hours']} h, {q['launches']} launches, {q['trades']} trades, "
                  f"{q['non_organic_pct']}% non-organic, {lag}, health {q['health_minutes']} min "
                  f"({q['degraded_minutes']} degraded), fees {', '.join(q['fee_bps_top']) or '-'}")
    elif args.rcmd == "eval":
        rep = cmd_eval(args.policy, args.file, args.quick, args.jobs)
        print_report(rep)
        if args.json:
            Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
    elif args.rcmd == "freeze":
        f = freeze(args.policy)
        print(f"{f['policy']} frozen at {f['frozen_at']} (hash {f['hash']}). Data recorded from now on is its "
              f"holdout; `research final {f['policy']}` judges it once there is enough.")
    elif args.rcmd == "final":
        rep = cmd_final(args.policy, args.file, args.jobs)
        if rep.get("verdict") is None:
            print(rep["progress"])
        else:
            print_report(rep)
    elif args.rcmd == "log":
        for e in experiments()[-40:]:
            r = e.get("result") or {}
            print(time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["ts"])), e.get("kind"), e.get("policy"),
                  e.get("hash") or ",".join(e.get("hashes") or []), e.get("verdict") or "",
                  f"{r.get('trades', '')} trades {r.get('pnl_sol', '')} SOL" if r else "",
                  ",".join(e.get("files") or []))


def add_parser(sub) -> None:
    rs = sub.add_parser("research", help="evaluate a written-down strategy honestly (see research.py)")
    rsub = rs.add_subparsers(dest="rcmd", required=True)
    d = rsub.add_parser("data", help="what's recorded and how clean it is")
    d.add_argument("--file", nargs="+")
    for name in ("eval", "final", "freeze"):
        p = rsub.add_parser(name)
        p.add_argument("policy", help="name in research/policies/ (or a path)")
        if name != "freeze":
            p.add_argument("--file", nargs="+", help="recordings (default: data/feed-*)")
            p.add_argument("--jobs", type=int, default=0)
        if name == "eval":
            p.add_argument("--quick", action="store_true", help="the policy only, no variants")
            p.add_argument("--json", help="also write the report here")
    rsub.add_parser("log", help="every experiment so far")

