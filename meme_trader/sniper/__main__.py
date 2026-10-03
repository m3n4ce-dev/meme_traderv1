"""Pump.fun sniper + copy trader CLI.

  python -m meme_trader.sniper doctor               # check setup: packages, keys, feed, RPC, wallet
  python -m meme_trader.sniper run                  # live pump.fun feed, PAPER trading, dashboard on :8787
  python -m meme_trader.sniper run --synthetic      # fake market incl. simulated wallets to copy (demo)
  python -m meme_trader.sniper run --live           # REAL trades (see docs/SETUP.md)
  python -m meme_trader.sniper backtest --file data/feed-2026-10-03.jsonl
  python -m meme_trader.sniper backtest --synthetic 2000 --seed 7 --set exit.stop_loss_pct=25
  python -m meme_trader.sniper leaders --file data/feed-*.jsonl   # find wallets worth copying
  python -m meme_trader.sniper review --validate    # AI post-mortem + backtest of its proposals
"""
from __future__ import annotations

import argparse
import asyncio
import copy as _copy
import os
import sys
import time
from collections import Counter, defaultdict

import yaml

from .. import config
from ..journal import DATA
from .engine import Engine
from .execution import LiveExecutor, PaperExecutor
from .feeds import FileFeed, PumpPortalFeed, SyntheticFeed


def apply_overrides(params, sets: list[str]):
    for item in sets or []:
        key, val = item.split("=", 1)
        node = params["sniper"]
        *path, last = key.split(".")
        for k in path:
            node = node[k]
        node[last] = yaml.safe_load(val)
    return params


def _sim_leaders(params, feed: SyntheticFeed) -> None:
    """In synthetic mode with no leaders configured, copy the simulated wallets."""
    if not params["sniper"]["copy"]["leaders"]:
        params["sniper"]["copy"]["leaders"] = [{"address": a, "label": lbl, "mode": "mirror"}
                                               for a, lbl in feed.leader_labels.items()]


def _desk(params, force: bool):
    from .desk import Desk

    if force:
        params["sniper"]["desk"]["enabled"] = True
    d = Desk(params.sniper.desk)
    if params.sniper.desk.enabled and not d.enabled:
        print("AI desk requested but ANTHROPIC_API_KEY is not set - running without it")
    return d if d.enabled else None


async def _run(args, params) -> None:
    feed = SyntheticFeed(seed=args.seed, speed=args.speed) if args.synthetic else \
        PumpPortalFeed(params.sniper.feed.fallback_ws_urls)
    if args.synthetic:
        _sim_leaders(params, feed)
    mode = "paper"
    if args.live:
        if args.synthetic:
            sys.exit("--live cannot be combined with --synthetic")
        if os.environ.get("MEME_TRADER_CONFIRM_LIVE") != "yes":
            sys.exit("live mode refused: set MEME_TRADER_CONFIRM_LIVE=yes to trade real funds")
        from ..wallet import Wallet

        wallet = Wallet(params.wallet.pubkey)
        bal = wallet.sol_balance()
        if bal < params.sniper.capital.starting_sol:
            sys.exit(f"wallet holds {bal:.4f} SOL < sniper.capital.starting_sol")
        executor, mode = LiveExecutor(params.sniper.execution, wallet), "live"
    else:
        executor = PaperExecutor(params.sniper.execution)
    record = None
    if not args.synthetic and not args.no_record:
        DATA.mkdir(exist_ok=True)
        record = DATA / f"feed-{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"
    engine = Engine(params, feed, executor, mode=mode + ("-synthetic" if args.synthetic else ""), record_path=record,
                    desk=_desk(params, args.desk))

    tasks = [asyncio.create_task(engine.run())]
    if not args.no_ui:
        from ..ui.server import serve

        tasks.append(asyncio.create_task(serve(engine, args.host, args.port)))
        print(f"dashboard: http://{args.host}:{args.port}")
    if not args.synthetic:
        from .signals import run_telegram, run_x_stream

        sig = params.sniper.signals
        tasks += [asyncio.create_task(run_telegram(sig.telegram_channels, engine.handle)),
                  asyncio.create_task(run_x_stream(sig.x_accounts, engine.handle))]
    await tasks[0]


async def run_backtest(params, feed, desk=None) -> Engine:
    engine = Engine(params, feed, PaperExecutor(params.sniper.execution), mode="backtest", log_to_journal=False,
                    desk=desk)
    await engine.run()
    for _ in range(int(params.sniper.exit.max_hold_s) + 5):   # let open positions hit time-based exits
        if not engine.positions:
            break
        engine.now += 1
        await engine._tick()
    for m in list(engine.positions):
        await engine.sell_now(m)
    return engine


def _file_or_synth(args):
    if args.file:
        return FileFeed(*args.file)
    return SyntheticFeed(seed=args.seed, speed=0, launches=args.synthetic, start_ts=1_780_000_000)


def _print_stats(title: str, s: dict) -> None:
    print(f"{title:<10} closed {s['closed']:>4}  win {s['win_rate']:>4.0%}  avg win {s['avg_win_pct']:+5.0f}%  "
          f"avg loss {s['avg_loss_pct']:+5.0f}%  PF {s['profit_factor']:5.2f}  P&L {s['realized_pnl_sol']:+.3f} SOL")


async def _backtest(args, params) -> Engine:
    feed = _file_or_synth(args)
    if isinstance(feed, SyntheticFeed):
        _sim_leaders(params, feed)
    engine = await run_backtest(params, feed, _desk(params, True) if args.desk else None)
    s = engine.summary()
    print("\n=== backtest", "synthetic" if not args.file else " ".join(args.file), "===")
    print(f"launches {s['launches']}  entries {s['entries']}  initials hit {s['initials_hit']}  "
          f"best {s['best_pct']:+.0f}%  worst {s['worst_pct']:+.0f}%")
    _print_stats("ALL", s)
    for src, st in s["by_source"].items():
        _print_stats(src, st)
    print(f"realized {s['realized_pnl_sol']:+.3f} SOL on {s['start_sol']} SOL start "
          f"({s['realized_pnl_sol'] / s['start_sol']:+.1%})")
    print("\nrejections:", ", ".join(f"{k} {v}" for k, v in engine.rejects.most_common()))
    exits = defaultdict(list)
    for c in engine.book.closed:
        exits[" ".join(c["exit"].split(" ")[:2])].append(c["pnl"])
    print("exits:", ", ".join(f"{k} n={len(v)} pnl={sum(v):+.3f}" for k, v in sorted(exits.items())))
    if engine.leaders.leaders:
        print("leaders:", ", ".join(f"{x['label']} copied {x['copied']} pnl {x['copied_pnl']:+.3f} ({x['status']})"
                                    for x in engine.leaders.snapshot()))
    if isinstance(feed, SyntheticFeed):   # ground truth: which kinds of launches did we buy?
        snipes = [c for c in engine.book.closed if c["source"] == "sniper"]
        bought = Counter(feed.archetype[c["mint"]] for c in snipes)
        pnl = defaultdict(float)
        for c in snipes:
            pnl[feed.archetype[c["mint"]]] += c["pnl"]
        total = Counter(feed.archetype.values())
        print("sniper buys by archetype:", ", ".join(f"{k} {bought[k]}/{total[k]} ({pnl[k]:+.3f})" for k in total))
    if engine.desk:
        print(f"AI desk: {engine.desk.calls} calls, ~${engine.desk.cost_usd():.2f}")
    if args.report:
        engine.save_report(DATA / args.report)
    return engine


def _leaders(args, params) -> None:
    from .copytrade import discover

    async def collect():
        return [e async for e in _file_or_synth(args).events()]

    ranked = discover(asyncio.run(collect()), params.sniper.entry.bundle_window_s, args.min_tokens)
    print(f"{'wallet':<46} {'tokens':>6} {'closed':>6} {'win%':>5} {'P&L SOL':>9} {'med entry':>9}  flags")
    for w in ranked[: args.top]:
        ages = sorted(w.entry_ages)
        med = ages[len(ages) // 2] if ages else 0
        flags = []
        if w.early_buys / max(w.tokens, 1) > 0.5:
            flags.append("INSIDER-LIKE (buys in bundle window)")
        if w.created:
            flags.append(f"deployer x{w.created}")
        if med < 3:
            flags.append("bot-speed")
        print(f"{w.wallet:<46} {w.tokens:>6} {w.closed:>6} {w.win_rate:>5.0%} {w.total_sol:>+9.2f} {med:>8.0f}s  "
              + ", ".join(flags))
    good = [w for w in ranked[: args.top] if w.total_sol > 0 and w.early_buys / max(w.tokens, 1) <= 0.5][:5]
    if good:
        print("\nCandidate config (paste under sniper.copy.leaders, start in mode: signal):")
        for w in good:
            print(f"  - {{address: {w.wallet}, label: cand-{w.wallet[:4]}, mode: signal}}")
    print("\nRanked on the recorded feed only - re-check on fresh data before trusting a wallet.")


def _review(args, params) -> None:
    from . import review

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("review needs ANTHROPIC_API_KEY (add it to .env)")
    data = review.gather(args.days, dict(params["sniper"]))
    if not data["trade_count"]:
        print("no closed trades in data/ yet - run the bot (paper is fine) first")
        return
    rv = review.ask_claude(data, params.sniper.desk.model)
    validation = None
    if args.validate and rv["param_changes"] and data["recorded_feed_files"]:
        files = data["recorded_feed_files"]
        base = asyncio.run(run_backtest(_copy.deepcopy(params), FileFeed(*files))).summary()
        prop = apply_overrides(_copy.deepcopy(params), [f"{c['key']}={c['value']}" for c in rv["param_changes"]])
        validation = {"baseline": base, "proposed": asyncio.run(run_backtest(prop, FileFeed(*files))).summary()}
    out = DATA / "reviews" / f"review-{time.strftime('%Y-%m-%d-%H%M', time.gmtime())}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(review.render(rv, validation))
    print(out.read_text())
    print(f"\nsaved {out}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="meme_trader.sniper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--synthetic", action="store_true", help="simulated market instead of the live feed")
    r.add_argument("--speed", type=float, default=1.0, help="synthetic playback speed")
    r.add_argument("--seed", type=int)
    r.add_argument("--live", action="store_true", help="trade real funds")
    r.add_argument("--desk", action="store_true", help="enable the AI desk (needs ANTHROPIC_API_KEY)")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--no-ui", action="store_true")
    r.add_argument("--no-record", action="store_true", help="don't save the live feed for backtesting")
    b = sub.add_parser("backtest")
    b.add_argument("--report", help="write JSON report to data/<name>")
    b.add_argument("--desk", action="store_true", help="include AI desk votes (costs API credits)")
    lead = sub.add_parser("leaders", help="rank wallets in recorded data as copy-trading candidates")
    lead.add_argument("--top", type=int, default=25)
    lead.add_argument("--min-tokens", type=int, default=5)
    for p in (b, lead):
        p.add_argument("--file", nargs="*", help="recorded feed JSONL file(s)")
        p.add_argument("--synthetic", type=int, default=1000, help="number of synthetic launches if no --file")
        p.add_argument("--seed", type=int, default=1)
    rv = sub.add_parser("review", help="AI post-mortem of recent trades")
    rv.add_argument("--days", type=int, default=3)
    rv.add_argument("--validate", action="store_true", help="backtest the proposed changes on recorded data")
    sub.add_parser("doctor", help="check setup")
    for p in (r, b, lead, rv, sub.choices["doctor"]):
        p.add_argument("--config")
        p.add_argument("--set", action="append", help="override a sniper param, e.g. exit.stop_loss_pct=25")
    args = ap.parse_args()
    params = apply_overrides(config.load(args.config), args.set)
    if args.cmd == "run":
        asyncio.run(_run(args, params))
    elif args.cmd == "backtest":
        asyncio.run(_backtest(args, params))
    elif args.cmd == "leaders":
        _leaders(args, params)
    elif args.cmd == "review":
        _review(args, params)
    else:
        from .doctor import run as doctor

        doctor(params)


if __name__ == "__main__":
    main()
