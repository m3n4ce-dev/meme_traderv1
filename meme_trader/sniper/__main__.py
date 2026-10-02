"""Pump.fun sniper CLI.

  python -m meme_trader.sniper run                  # live pump.fun feed, PAPER trading, dashboard on :8787
  python -m meme_trader.sniper run --synthetic      # fake market (demo / UI), paper
  python -m meme_trader.sniper run --live           # REAL trades (see README for the required env vars)
  python -m meme_trader.sniper backtest --file data/feed-2026-10-03.jsonl
  python -m meme_trader.sniper backtest --synthetic 2000 --seed 7 --set exit.stop_loss_pct=25
"""
from __future__ import annotations

import argparse
import asyncio
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


async def _run(args, params) -> None:
    feed = SyntheticFeed(seed=args.seed, speed=args.speed) if args.synthetic else PumpPortalFeed()
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
    engine = Engine(params, feed, executor, mode=mode + ("-synthetic" if args.synthetic else ""), record_path=record)

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


async def _backtest(args, params) -> Engine:
    feed = FileFeed(*args.file) if args.file else SyntheticFeed(seed=args.seed, speed=0, launches=args.synthetic,
                                                                 start_ts=1_780_000_000)
    engine = Engine(params, feed, PaperExecutor(params.sniper.execution), mode="backtest", log_to_journal=False)
    await engine.run()
    # flush: let open positions hit their time-based exits
    for _ in range(int(params.sniper.exit.max_hold_s) + 5):
        if not engine.positions:
            break
        engine.now += 1
        await engine._tick()
    for m in list(engine.positions):
        await engine.sell_now(m)

    s = engine.summary()
    print("\n=== backtest", "synthetic" if not args.file else args.file, "===")
    print(f"launches {s['launches']}  entries {s['entries']}  closed {s['closed']}  "
          f"win rate {s['win_rate']:.0%}  initials hit {s['initials_hit']}")
    print(f"avg win {s['avg_win_pct']:+.0f}%  avg loss {s['avg_loss_pct']:+.0f}%  profit factor {s['profit_factor']:.2f}  "
          f"best {s['best_pct']:+.0f}%  worst {s['worst_pct']:+.0f}%")
    print(f"realized {s['realized_pnl_sol']:+.3f} SOL on {s['start_sol']} SOL start "
          f"({s['realized_pnl_sol'] / s['start_sol']:+.1%})")
    print("\nrejections:", ", ".join(f"{k} {v}" for k, v in engine.rejects.most_common()))
    exits = defaultdict(list)
    for c in engine.book.closed:
        exits[c["exit"].split(" ")[0] + " " + c["exit"].split(" ")[1] if " " in c["exit"] else c["exit"]].append(c["pnl"])
    print("exits:", ", ".join(f"{k} n={len(v)} pnl={sum(v):+.3f}" for k, v in sorted(exits.items())))
    if isinstance(feed, SyntheticFeed):   # ground truth: which kinds of launches did we buy?
        bought = Counter(feed.archetype[c["mint"]] for c in engine.book.closed)
        pnl = defaultdict(float)
        for c in engine.book.closed:
            pnl[feed.archetype[c["mint"]]] += c["pnl"]
        total = Counter(feed.archetype.values())
        print("bought by archetype:", ", ".join(f"{k} {bought[k]}/{total[k]} ({pnl[k]:+.3f} SOL)" for k in total))
    if args.report:
        engine.save_report(DATA / args.report)
    return engine


def main() -> None:
    ap = argparse.ArgumentParser(prog="meme_trader.sniper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--synthetic", action="store_true", help="simulated market instead of the live feed")
    r.add_argument("--speed", type=float, default=1.0, help="synthetic playback speed")
    r.add_argument("--seed", type=int)
    r.add_argument("--live", action="store_true", help="trade real funds")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--no-ui", action="store_true")
    r.add_argument("--no-record", action="store_true", help="don't save the live feed for backtesting")
    b = sub.add_parser("backtest")
    b.add_argument("--file", nargs="*", help="recorded feed JSONL file(s)")
    b.add_argument("--synthetic", type=int, default=1000, help="number of synthetic launches if no --file")
    b.add_argument("--seed", type=int, default=1)
    b.add_argument("--report", help="write JSON report to data/<name>")
    for p in (r, b):
        p.add_argument("--config")
        p.add_argument("--set", action="append", help="override a sniper param, e.g. exit.stop_loss_pct=25")
    args = ap.parse_args()
    params = apply_overrides(config.load(args.config), args.set)
    asyncio.run(_run(args, params) if args.cmd == "run" else _backtest(args, params))


if __name__ == "__main__":
    main()
