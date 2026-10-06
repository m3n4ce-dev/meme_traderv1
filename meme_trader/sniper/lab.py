"""The team's lab: experiments the bots run on the recorded market, one setting at a time.

An experiment replays the recordings twice with the research engine (research.py): once with the bot's current
graduation-play settings ("now"), once with one change. Same days, same coins, same costs and fill delay. The
verdict compares them block by block (6-hour blocks), because trades in the same hours share a market.

It's development, not proof: the result is in-sample, and every experiment tried is counted, because the best of
many tries always looks better than it is. A change that holds up becomes a proposal the owner can Apply, and a
candidate for a new frozen version (docs/RESEARCH.md). It never touches graduation-v1 or its holdout.

    python -m meme_trader.sniper lab-run <experiment id>     # what the bot starts, in a low-priority process
"""
from __future__ import annotations

import json
import math
import random
import secrets
from pathlib import Path

# what the team may test: the graduation play's own entry and exit rules (keys under sniper.late)
TESTABLE = {"late.min_net_flow_sol": (0.5, 20), "late.min_buyers": (3, 60), "late.min_buy_sell_ratio": (1.0, 5.0),
            "late.min_near_high": (0.5, 1.0), "late.min_curve_pct": (30, 84), "late.max_curve_pct": (56, 92),
            "late.max_age_s": (120, 3600), "late.flow_window_s": (10, 120), "late.stop_loss_pct": (5, 50),
            "late.stall_s": (15, 300), "late.max_hold_s": (60, 3600), "late.exit_curve_pct": (86, 99)}
# when nobody has proposed anything, the team works through these: one step either side of where it is now
AUTO_STEPS = {"late.min_net_flow_sol": 1.0, "late.min_buyers": 4, "late.min_buy_sell_ratio": 0.3, "late.min_near_high": 0.05,
              "late.min_curve_pct": 5, "late.max_age_s": 300, "late.stop_loss_pct": 5, "late.stall_s": 15,
              "late.exit_curve_pct": 2}
# how the live bot's orders land: replays copy these, so a test sees the same delays, failed sells and retries
# (graduation-v1 fills instantly with 3% slippage; the owner's bot waits 2.5 s and its sells can fail and retry)
EXECUTION = ("execution.paper_delay_s", "execution.paper_latency_slippage_pct", "execution.slippage_pct",
             "execution.sell_slippage_steps", "execution.urgent_sell_slippage_steps")
BLOCK_S = 6 * 3600
MAX_QUEUED = 6


def landed_like_bot(x: dict) -> bool:
    """Was this test replayed with the bot's own order landing (delay, failed sells, retries)? Tests queued before
    2026-10-05 13:00 filled instantly with 3% slippage: easier than what the bot faces, so their verdicts are weaker."""
    return "execution.paper_delay_s" in (x.get("baseline") or {})


class Lab:
    def __init__(self, path: Path | None):
        self.dir = Path(path) if path else None
        self.items: list[dict] = []
        if self.dir and (self.dir / "experiments.json").exists():
            try:
                self.items = json.loads((self.dir / "experiments.json").read_text())
            except (OSError, ValueError):
                self.items = []
        for x in self.items:                       # a run without its own service died with the bot: queue it again
            if x.get("status") == "running" and not x.get("unit"):   # (one in its own service is picked back up)
                x["status"] = "queued"

    def save(self) -> None:
        if not self.dir:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "experiments.json.tmp"
        tmp.write_text(json.dumps(self.items[-200:]))
        tmp.replace(self.dir / "experiments.json")

    def queued(self) -> list[dict]:
        return [x for x in self.items if x["status"] == "queued"]

    def running(self) -> dict | None:
        return next((x for x in self.items if x["status"] == "running"), None)

    def add(self, key: str, value, why: str, by: str, now: float, baseline: dict) -> tuple[dict | None, str]:
        """Queue a test of one setting. (experiment, '') or (None, why not)."""
        if key not in TESTABLE:
            return None, f"{key} isn't one the lab can test (graduation entry and exit settings only)"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None, "the value must be a number"
        lo, hi = TESTABLE[key]
        if not lo <= v <= hi:
            return None, f"{key} must be between {lo:g} and {hi:g}"
        v = int(v) if float(v).is_integer() and isinstance(baseline.get(key), int) else v
        if baseline.get(key) == v:
            return None, "that's already the current setting"
        if any(x["key"] == key and x["value"] == v and x["status"] in ("queued", "running") for x in self.items):
            return None, "that test is already queued"
        if len(self.queued()) >= MAX_QUEUED:
            return None, f"the lab has {MAX_QUEUED} tests waiting already"
        x = {"id": secrets.token_hex(4), "key": key, "value": v, "now": baseline.get(key), "why": str(why or "")[:300],
             "by": by, "status": "queued", "created": now, "started": None, "finished": None, "result": None,
             "baseline": baseline}
        self.items.append(x)
        self.save()
        return x, ""

    def auto_candidate(self, baseline: dict, now: float) -> tuple[str, float] | None:
        """The next one-step change nobody has tested lately (on this data)."""
        # (a test judged with instant fills, before replays landed orders like the bot, doesn't count: it's tried again)
        recent = {(x["key"], x["value"]) for x in self.items if now - x["created"] < 2 * 86400 and landed_like_bot(x)}
        options = []
        for key, step in AUTO_STEPS.items():
            cur = baseline.get(key)
            if cur is None:
                continue
            for v in (cur - step, cur + step):
                v = round(v, 3)
                lo, hi = TESTABLE[key]
                if lo <= v <= hi and (key, v) not in recent:
                    options.append((key, int(v) if isinstance(cur, int) else v))
        return random.Random(int(now // 3600)).choice(options) if options else None

    def started_today(self, now: float) -> int:
        return sum(1 for x in self.items if (x.get("started") or 0) > now - 86400)

    def tries(self) -> int:
        return sum(1 for x in self.items if x["status"] == "done")

    def view(self) -> dict:
        done = [{**x, "instant_fills": not landed_like_bot(x)} for x in self.items if x["status"] in ("done", "failed")][-20:][::-1]
        return {"running": self.running(), "queued": self.queued(), "done": done, "tries": self.tries()}


# ------------------------------------------------------------------ the run itself (a separate process)
def _blocks(trades: list[dict], t0: float, n: int) -> list[float]:
    sums = [0.0] * n
    for t in trades:
        sums[min(max(int((t["opened"] - t0) // BLOCK_S), 0), n - 1)] += t["pnl"]
    return sums


def compare(base: list[dict], var: list[dict], span: tuple[float, float], sims: int = 4000) -> dict:
    """The change against now, block by block: the difference in P&L per 6-hour block, resampled."""
    n = max(1, math.ceil((span[1] - span[0]) / BLOCK_S))
    b, v = _blocks(base, span[0], n), _blocks(var, span[0], n)
    diff = [y - x for x, y in zip(b, v)]
    rng = random.Random(7)
    means = sorted(sum(rng.choice(diff) for _ in range(n)) / n for _ in range(sims)) if n >= 3 else []

    def side(tr: list[dict]) -> dict:
        pnl = sum(t["pnl"] for t in tr)
        return {"trades": len(tr), "pnl_sol": round(pnl, 4), "per_trade_pct": round(sum(t["pnl_pct"] for t in tr) / len(tr), 2) if tr else None,
                "won_pct": round(sum(t["pnl"] > 0 for t in tr) / len(tr) * 100) if tr else None,
                "without_best3_sol": round(pnl - sum(sorted((t["pnl"] for t in tr), reverse=True)[:3]), 4)}
    p_better = sum(m > 0 for m in means) / len(means) if means else None
    better_blocks = sum(d > 1e-9 for d in diff)
    worse_blocks = sum(d < -1e-9 for d in diff)
    same = [(t["mint"], t["opened"], round(t["pnl"], 9)) for t in base] == [(t["mint"], t["opened"], round(t["pnl"], 9)) for t in var]
    if same:
        verdict = "no effect"                      # the change never touched a trade on these days
    elif p_better is None:
        verdict = "not enough data"
    elif p_better >= 0.9 and better_blocks > worse_blocks:
        verdict = "better"
    elif p_better <= 0.1 and worse_blocks > better_blocks:
        verdict = "worse"
    else:
        verdict = "no clear difference"
    return {"now": side(base), "change": side(var), "blocks": n, "better_blocks": better_blocks, "worse_blocks": worse_blocks,
            "diff_sol_per_block": round(sum(diff) / n, 5),
            "lo90": round(means[int(.05 * len(means))], 5) if means else None, "hi90": round(means[int(.95 * len(means))], 5) if means else None,
            "p_better": round(p_better, 3) if p_better is not None else None, "verdict": verdict,
            "hours": round((span[1] - span[0]) / 3600, 1)}


def free_mb() -> float | None:
    """Memory available on this machine (MB), from /proc/meminfo; None where that doesn't exist."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def _last_ts(path: Path) -> float:
    """The newest event time in a recording: its tail for a plain file, every line for a compressed one."""
    from . import research
    lines = []
    if not str(path).endswith(".gz"):
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().decode(errors="replace").splitlines()[1:]
    else:
        lines = list(research._lines(path))
    best = 0.0
    for line in lines:
        try:
            best = max(best, float(json.loads(line).get("ts") or 0))
        except (ValueError, TypeError, AttributeError):
            continue
    return best


def run(exp_id: str, data: Path, files: list[Path] | None = None, jobs: int = 1, hours: float = 24) -> dict:
    """One replay worker at a time: measured 2026-10-05, a replay of 3 days holds ~4.8 GB per worker."""
    from . import research

    lab = Lab(data / "lab")
    x = next((e for e in lab.items if e["id"] == exp_id), None)
    if x is None:
        raise SystemExit(f"no experiment {exp_id}")
    pol = research.load_policy("graduation-v1")          # its rules and costs, unfrozen: a development copy
    pol.pop("frozen_at", None)
    base_sets = [f"{k}={json.dumps(v)}" for k, v in (x.get("baseline") or {}).items() if k in TESTABLE or k in EXECUTION]
    # the last 24 hours of recordings (measured 2026-10-05: three days took over 30 minutes per pair of replays), and
    # only coins that got far enough up their curve for the graduation play to touch them
    files = files or research.feed_files()[-2:]
    last = _last_ts(files[-1]) if files else 0.0
    lo = min(float(x["baseline"].get("late.min_curve_pct", 55)), float(x["value"]) if x["key"] == "late.min_curve_pct" else 99) - 3
    events, stats = research.load_events(files, start=last - hours * 3600 if last else None, min_progress_pct=lo)
    span = (stats["first"] or 0, stats["last"] or 0)
    res = research.run_variants(pol, events, [("now", pol, base_sets), ("change", pol, base_sets + [f"{x['key']}={json.dumps(x['value'])}"])],
                                jobs=jobs)
    out = compare(res["now"]["trades"], res["change"]["trades"], span)
    out["files"] = [p.name for p in files]
    return out
