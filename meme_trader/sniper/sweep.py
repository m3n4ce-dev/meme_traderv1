"""Walk-forward parameter sweep: tune on earlier data, prove it on later data it never saw.

    python -m meme_trader.sniper sweep --file data/feed-* --jobs 4 \\
        --grid exit.stop_loss_pct=20,30,40 --grid entry.min_score=45,55,65

Launches are split by launch time: the first `train` share of tokens is the tuning set, the rest
is the test set. The split is purged: tuning data stops at the cutoff, and tokens launched within
the longest holding time before it belong to neither side (see split()). Every grid combination
runs on the tuning set; the best few, plus the current settings, are then run on the test set. A change
is only recommended if it also beats the current settings on the test set - otherwise it was probably noise.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import time

import yaml

from .events import Funding, Launch
from .feeds import Feed, FileFeed, SyntheticFeed


class MemoryFeed(Feed):
    realtime = False

    def __init__(self, events: list):
        self.ev = events

    async def events(self):
        for e in self.ev:
            self._last = e.ts
            yield e


def load_events(source: dict) -> list:
    """source: {"files": [...]} or {"synthetic": n_launches, "seed": s}. Synthetic markets are
    deterministic per seed, so worker processes can rebuild one instead of receiving it."""
    feed = FileFeed(*source["files"]) if source.get("files") else \
        SyntheticFeed(seed=source.get("seed"), speed=0, launches=source["synthetic"], start_ts=1_780_000_000)

    async def collect():
        return [e async for e in feed.events()]
    return asyncio.run(collect())


_W: dict = {}


def _init_worker(params, source: dict, train: float) -> None:
    """Runs once per worker process: load the events there, so no task ships a market over a pipe."""
    tr, te = split(load_events(source), train, embargo_for(params))
    _W.update(params=params, tr=tr, te=te)


def _run_in_worker(sets: list[str], which: str) -> dict:
    from .__main__ import apply_overrides, run_backtest

    p = apply_overrides(copy.deepcopy(_W["params"]), sets)
    return asyncio.run(run_backtest(p, MemoryFeed(_W[which]))).summary()


def split(events: list, train: float, embargo_s: float = 0.0) -> tuple[list, list]:
    """Chronological split by launch time, purged so the training side can't see the future:
    * training data stops at the cutoff (later trades of earlier tokens are dropped, not kept);
    * tokens launched within `embargo_s` before the cutoff go to neither side, so every training
      outcome that takes up to `embargo_s` to resolve is complete before the first test launch;
    * funding lookups go to training only if they happened before the cutoff (the test side, replayed
      in time order, may use all of them)."""
    events = sorted(events, key=lambda e: e.ts)
    launches = [e for e in events if isinstance(e, Launch)]
    if len(launches) < 2:
        return events, []
    cutoff = launches[int(len(launches) * train)].ts
    side: dict[str, str] = {}
    for e in launches:
        if e.mint not in side:
            side[e.mint] = "test" if e.ts >= cutoff else ("train" if e.ts < cutoff - embargo_s else "purged")
    tr, te = [], []
    for e in events:
        if isinstance(e, Funding):
            if e.ts < cutoff:
                tr.append(e)
            te.append(e)
            continue
        mint = getattr(e, "mint", None)
        where = side.get(mint, "train" if e.ts < cutoff - embargo_s else ("test" if e.ts >= cutoff else "purged"))
        if where == "train" and e.ts < cutoff:
            tr.append(e)
        elif where == "test":
            te.append(e)
    return tr, te


def embargo_for(params) -> float:
    """How long a strategy outcome takes to resolve: the longest a position can be held."""
    sn = params.sniper
    return float(max(sn.exit.max_hold_s, sn.late.max_age_s + sn.late.max_hold_s, sn.callouts.hold_s))


def combos(grids: list[str], cap: int = 200) -> list[list[str]]:
    axes = []
    for g in grids:
        key, vals = g.split("=", 1)
        axes.append([f"{key}={v.strip()}" for v in vals.split(",") if v.strip()])
    out = [list(c) for c in itertools.product(*axes)]
    if len(out) > cap:
        raise SystemExit(f"{len(out)} combinations - narrow the grid (max {cap})")
    return out


def score(s: dict, metric: str, min_trades: int) -> float:
    if s["closed"] < min_trades:
        return float("-inf")
    if metric == "pf":
        return min(s["profit_factor"], 50.0)
    return s["realized_pnl_sol"]


def beats(cand: float, base: float, metric: str) -> bool:
    """A real improvement, not noise: >= 5% better AND an absolute margin (0.02 SOL, or 0.1 PF)."""
    margin = 0.02 if metric == "pnl" else 0.1
    return cand >= base + max(abs(base) * 0.05, margin)


async def sweep(params, events: list, grids: list[str], train: float = 0.6, metric: str = "pnl",
                min_trades: int = 10, top: int = 3, log=print, jobs: int = 1, source: dict | None = None) -> dict:
    """jobs > 1 (needs `source`): backtests run in that many processes, one per CPU core."""
    from .__main__ import apply_overrides, run_backtest

    tr, te = split(events, train, embargo_for(params))
    grid = combos(grids)
    log(f"{len(grid)} combination(s); tuning on {sum(isinstance(e, Launch) for e in tr)} launches, "
        f"testing on {sum(isinstance(e, Launch) for e in te)}" + (f"; {jobs} processes" if jobs > 1 else ""))
    pool = None
    if jobs > 1 and source:
        from concurrent.futures import ProcessPoolExecutor

        pool = ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker, initargs=(params, source, train))

    async def run(sets: list[str], evs: list) -> dict:
        if pool is not None:
            return await asyncio.wrap_future(pool.submit(_run_in_worker, sets, "tr" if evs is tr else "te"))
        p = apply_overrides(copy.deepcopy(params), sets)
        return (await run_backtest(p, MemoryFeed(evs))).summary()

    t0 = time.time()
    try:
        base_train = await run([], tr)
        rows = []

        async def one(sets):
            s = await run(sets, tr)
            rows.append({"sets": sets, "train": s, "score": score(s, metric, min_trades)})
            log(f"  [{len(rows)}/{len(grid)}] {' '.join(sets):<60} train P&L {s['realized_pnl_sol']:+.3f}  "
                f"PF {min(s['profit_factor'], 99):.2f}  n={s['closed']}  ({time.time() - t0:.0f}s)")
        if pool is not None:
            await asyncio.gather(*(one(sets) for sets in grid))
        else:
            for sets in grid:
                await one(sets)
        rows.sort(key=lambda r: (r["score"], r["sets"]), reverse=True)     # ties broken the same way every run
        finalists = [r for r in rows[:top] if r["score"] != float("-inf")]
        base_test = await run([], te) if te else None
        tests = await asyncio.gather(*(run(r["sets"], te) for r in finalists)) if te and pool is not None else \
            [await run(r["sets"], te) if te else None for r in finalists]
        for r, t in zip(finalists, tests):
            r["test"] = t
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
    best = None
    if base_test is not None:
        better = [r for r in finalists
                  if r["test"]["closed"] >= max(1, min_trades // 2)
                  and beats(score(r["test"], metric, 0), score(base_test, metric, 0), metric)]
        best = max(better, key=lambda r: score(r["test"], metric, 0)) if better else None
    return {"baseline": {"train": base_train, "test": base_test}, "finalists": finalists, "best": best,
            "all": rows}


def report(res: dict, metric: str) -> str:
    def fmt(s):
        if not s:
            return "-"
        return f"P&L {s['realized_pnl_sol']:+.3f}  PF {min(s['profit_factor'], 99):.2f}  win {s['win_rate']:.0%}  n={s['closed']}"
    lines = ["", "=== walk-forward result ===", f"current settings    train: {fmt(res['baseline']['train'])}",
             f"                    test:  {fmt(res['baseline']['test'])}"]
    for r in res["finalists"]:
        lines += [f"{' '.join(r['sets'])}", f"                    train: {fmt(r['train'])}",
                  f"                    test:  {fmt(r.get('test'))}"]
    if res["best"]:
        sets = " ".join(f"--set {x}" for x in res["best"]["sets"])
        yaml_lines = "\n".join(f"#   sniper.{x.split('=')[0]}: {yaml.safe_load(x.split('=', 1)[1])}"
                               for x in res["best"]["sets"])
        lines += ["", f"RECOMMENDED (beat current settings on unseen data by {metric}): {sets}",
                  "Put it in config/params.yaml, paper-trade it a few days, then re-run this sweep:", yaml_lines]
    else:
        lines += ["", "No combination beat the current settings by a real margin (>=5% and >=0.02 SOL / 0.1 PF) "
                      "on the unseen test data - keep current settings."]
    return "\n".join(lines)
