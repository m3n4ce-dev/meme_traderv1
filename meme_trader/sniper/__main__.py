"""Pump.fun sniper + copy trader CLI.

  python -m meme_trader.sniper doctor               # check setup: packages, keys, feed, RPC, wallet
  python -m meme_trader.sniper run                  # live pump.fun feed, PAPER trading, dashboard on :8787
  python -m meme_trader.sniper run --synthetic      # fake market incl. simulated wallets to copy (demo)
  python -m meme_trader.sniper run --live           # REAL trades (see docs/SETUP.md)
  python -m meme_trader.sniper backtest --file data/feed-2026-10-03.jsonl.gz
  python -m meme_trader.sniper backtest --synthetic 2000 --seed 7 --set exit.stop_loss_pct=25
  python -m meme_trader.sniper leaders --file data/feed-*        # find wallets worth copying
  python -m meme_trader.sniper sweep --file data/feed-* --jobs 4 --grid exit.stop_loss_pct=20,30,40
  python -m meme_trader.sniper train --file data/feed-*          # fit + grade the P(2x) model
  python -m meme_trader.sniper compare --seeds 1-8 --variant "late: late.enabled=true"   # A/B on many markets
  python -m meme_trader.sniper report                            # HTML report of paper/live trades
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

from pathlib import Path

import yaml

from .. import config
from ..journal import DATA
from .engine import Engine
from .execution import LiveExecutor, PaperExecutor
from .feeds import FileFeed, PumpPortalFeed, SolanaTradeFeed, SyntheticFeed, compress_file


def apply_overrides(params, sets: list[str]):
    """--set key=value overrides (value parsed as YAML), then the whole config is validated again."""
    for item in sets or []:
        if "=" not in item:
            raise config.ConfigError(f"--set {item!r}: use key=value")
        key, val = item.split("=", 1)
        node = params["sniper"]
        *path, last = key.split(".")
        for k in path:
            if not isinstance(node, dict) or k not in node:
                raise config.ConfigError(f"--set {key}: unknown setting")
            node = node[k]
        if not isinstance(node, dict) or last not in node:
            raise config.ConfigError(f"--set {key}: unknown setting")
        node[last] = yaml.safe_load(val)
    if sets:
        config.validate(params)
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
    f = params.sniper.feed
    if args.synthetic:
        feed = SyntheticFeed(seed=args.seed, speed=args.speed)
    elif f.trades == "pumpportal":
        feed = PumpPortalFeed(f.fallback_ws_urls)
    else:
        feed = SolanaTradeFeed(f.ws_url, f.fallback_ws_urls, f.commitment, f.max_gap_pct, f.stall_s, f.max_lag_s)
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
        resuming = (DATA / "sniper_state_live.json").exists()
        if resuming:      # after a restart part of the budget is in open positions - only require fee money
            if bal < params.sniper.capital.min_sol_reserve:
                sys.exit(f"wallet holds {bal:.4f} SOL - not even the fee reserve")
        elif bal < params.sniper.capital.starting_sol:
            sys.exit(f"wallet holds {bal:.4f} SOL < sniper.capital.starting_sol")
        executor, mode = LiveExecutor(params.sniper.execution, wallet), "live"
    else:
        executor = PaperExecutor(params.sniper.execution)
    full_mode = mode + ("-synthetic" if args.synthetic else "")
    # one bot per mode per data folder, and one live bot per wallet (across checkouts): two engines
    # sharing a state file or a wallet would trade against each other's books
    locks = [_lock(DATA / f"run-{full_mode}.lock", f"a {full_mode} bot is already running from this folder")]
    if mode == "live":
        locks.append(_lock(Path.home() / ".cache" / "meme_trader" / f"wallet-{executor.wallet.pubkey}.lock",
                           "a live bot is already trading this wallet"))
    record = None
    if not args.synthetic and not args.no_record:
        DATA.mkdir(exist_ok=True)
        record = DATA / f"feed-{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"
        _compress_old_feeds(record)
    engine = Engine(params, feed, executor, mode=full_mode, record_path=record, desk=_desk(params, args.desk))

    runner = None
    if not args.no_ui:                    # bind BEFORE trading: no controls = no engine
        from ..ui.server import start

        try:
            runner = await start(engine, args.host, args.port)
        except OSError as e:
            sys.exit(f"dashboard can't start on {args.host}:{args.port} ({e}) - nothing was traded. "
                     "Stop the other bot, use --port, or run headless with --no-ui")
        print(f"dashboard: http://{args.host}:{args.port}")
    main = asyncio.create_task(engine.run())
    aux = set()
    if not args.synthetic:
        from .signals import run_telegram, run_x_stream

        sig = params.sniper.signals
        aux = {asyncio.create_task(run_telegram(sig.telegram_channels, engine.handle), name="telegram signals"),
               asyncio.create_task(run_x_stream(sig.x_accounts, engine.handle), name="X signals")}
    try:
        await _supervise(engine, main, aux)
    finally:
        if runner is not None:
            await runner.cleanup()
        for t in aux:
            t.cancel()
        del locks


def _lock(path: Path, busy: str):
    """Exclusive, non-blocking lock held for the life of the process (released automatically on exit)."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        pid = f.read().strip() or "?"
        f.close()
        sys.exit(f"{busy} (pid {pid}; lock {path}). Stop it first.")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


async def _supervise(engine, main: asyncio.Task, aux: set) -> None:
    """Wait for the engine; report helper tasks that die instead of letting their errors vanish."""
    pending = {main, *aux}
    while main in pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is main:
                main.result()                       # re-raise an engine crash
            elif not t.cancelled() and t.exception() is not None:
                engine.say("error", f"{t.get_name()} stopped: {t.exception()!r} - trading continues without it")


def _compress_old_feeds(current: Path) -> None:
    """Gzip finished days left uncompressed (e.g. the service was down at midnight), in the background."""
    import threading

    old = [p for p in sorted(DATA.glob("feed-*.jsonl")) if p != current and not p.with_name(p.name + ".gz").exists()]

    def job():
        for p in old:
            try:
                compress_file(p)
                print(f"compressed {p.name}")
            except OSError as err:
                print(f"could not compress {p.name}: {err}")
    if old:
        threading.Thread(target=job, daemon=True).start()


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
    engine._audit_settle(final=True)
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


def _source(args, params) -> dict:
    """Where the events come from, in a form worker processes can rebuild them from."""
    if args.file:
        return {"files": list(args.file)}
    _sim_leaders(params, SyntheticFeed(seed=args.seed, speed=0, launches=args.synthetic, start_ts=1_780_000_000))
    return {"synthetic": args.synthetic, "seed": args.seed}


def _sweep(args, params) -> None:
    from .sweep import load_events, report, sweep

    source = _source(args, params)
    events = load_events(source)
    jobs = args.jobs or max(1, min((os.cpu_count() or 2) - 1, 8))
    res = asyncio.run(sweep(params, events, args.grid, args.train, args.metric, args.min_trades, args.top,
                            jobs=jobs, source=source))
    print(report(res, args.metric))


def _train(args, params) -> None:
    from ..config import ROOT
    from .predictor import train
    from .sweep import load_events

    source = _source(args, params)
    events = load_events(source)
    print(f"{len(events):,} events loaded; building labelled snapshots...")
    model, info = train(events, params, args.train, source=source)
    out = Path(args.out) if args.out else candidate_path(params)
    out = out if out.is_absolute() else ROOT / out
    model.save(out)
    t, tr = info["test"], info["train"]
    print(f"\n=== model: P({info['label']}) ===")
    print(f"samples   train {info['n_train']:,}  calibrate {info['n_cal']:,}  test {info['n_test']:,}  ({info['seconds']}s)")
    if t.get("n"):
        print(f"test      AUC {t['auc']:.3f}  Brier skill {t['brier_skill']:+.3f}  base rate {t['base_rate']:.1%}  "
              f"top decile {t['top_decile_rate']:.1%} ({t['top_decile_lift']:.1f}x lift)")
        raw = info["test_uncalibrated"]
        print(f"calibration temperature {info['temperature']}  (Brier skill before {raw['brier_skill']:+.3f}, "
              f"after {t['brier_skill']:+.3f}; T > 1 = the raw model was overconfident)")
        print(f"train     AUC {tr['auc']:.3f}  (a big train/test gap = overfitting)")
        print("calibration (test): predicted -> actual")
        for b in t["calibration"]:
            print(f"  {b['lo']:.0%}-{b['hi']:.0%}  n={b['n']:<6} {b['predicted']:.0%} -> {b['actual']:.0%}")
    print("strongest features:", ", ".join(f"{k} {w:+.2f}" for k, w in info["weights"][:8]))
    print(f"\nsaved CANDIDATE {out} - not used by the bot until promoted (scripts/start.sh promote)")
    auc = t.get("auc") or 0
    if auc < PROMOTE_MIN_AUC:
        print("VERDICT: little skill on unseen data. Don't promote it; record more data and retrain.")
    elif auc < 0.7:
        print("VERDICT: some skill. Promote it to see P(2x) on the dashboard; keep sniper.predict.display_only: true "
              "until a sweep/compare shows it helps.")
    else:
        print("VERDICT: useful skill on held-out data. Promote it, then sweep sniper.predict.min_p (e.g. 0,0.15,0.25) "
              "with display_only: false before letting it gate or size real trades.")
    if source.get("synthetic"):
        print("NOTE: trained on the SYNTHETIC market - fine for testing the pipeline, meaningless for real trading. "
              "It can't be promoted. Train on your recorded data (data/feed-*).")


PROMOTE_MIN_AUC = 0.6


def candidate_path(params) -> Path:
    mp = Path(params.sniper.predict.model_path)
    return mp.with_name(mp.stem + "-candidate" + mp.suffix)


def _promote(args, params) -> None:
    """Deploy a trained candidate: only real recorded data, only with skill on unseen launches."""
    from ..config import ROOT
    from .predictor import LogisticModel

    src = Path(args.file_model) if args.file_model else candidate_path(params)
    src = src if src.is_absolute() else ROOT / src
    m = LogisticModel.load(src)
    if m is None:
        sys.exit(f"no usable model at {src} - run: scripts/start.sh train")
    info = m.info or {}
    auc = (info.get("test") or {}).get("auc") or 0
    if info.get("source") != "recorded":
        sys.exit(f"refused: {src.name} was trained on {info.get('source', 'unknown')} data, not your recordings")
    if auc < PROMOTE_MIN_AUC and not args.force:
        sys.exit(f"refused: held-out AUC {auc:.3f} < {PROMOTE_MIN_AUC} (little skill). --force to promote anyway")
    info.update(promoted=True, promoted_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), promoted_from=src.name)
    m.info = info
    dst = Path(params.sniper.predict.model_path)
    dst = dst if dst.is_absolute() else ROOT / dst
    m.save(dst)
    print(f"promoted {src.name} -> {dst} (held-out AUC {auc:.3f}); a running bot loads it within a minute.")
    print("It's display-only while sniper.predict.display_only is true." if params.sniper.predict.display_only
          else "display_only is false: it now gates and sizes entries (predict.min_p, kelly_fraction).")


def _report(args, params) -> None:
    from .report import equity_from_trades, load_trades, row_mode, sessions, write

    sn = params.sniper
    if args.backtest:
        engine = asyncio.run(run_backtest(params, FileFeed(*args.file) if args.file else
                                          _synthetic_with_leaders(args, params)))
        closed, eq = engine.book.closed, list(engine.book.equity_hist)
        source = ("backtest of " + " ".join(Path(f).name for f in args.file)) if args.file else \
            f"backtest on a SYNTHETIC market ({args.synthetic} launches, seed {args.seed}) - tests logic, not profitability"
        gates, model = engine.gate_audit(), engine.model_card()
    else:
        everything = load_trades(DATA, args.days)
        modes = sorted({row_mode(c) for c in everything})
        mode = args.mode or (modes[0] if len(modes) == 1 else None)
        if mode is None:                 # never merge demo, paper and live results without being asked
            sys.exit(f"recorded trades come from several modes ({', '.join(modes)}): pick one with --mode "
                     f"(or --mode all to combine them deliberately)")
        closed = load_trades(DATA, args.days, mode, args.session)
        runs = sessions(closed)
        starts = {s for _, s, _ in runs if s}
        start = runs[0][1] if runs and runs[0][1] else sn.capital.starting_sol
        eq = equity_from_trades(closed, start)
        strategies = sorted({c.get("source", "").split(":")[0] for c in closed})
        source = (f"{len(closed)} recorded {mode} trades from data/trades-*.jsonl in {len(runs)} session(s); "
                  f"strategies: {', '.join(strategies) or 'none'}")
        if len(starts) > 1:
            listed = ", ".join(f"{x:g}" for x in sorted(starts))
            source += (f". NOTE: sessions started with different balances ({listed}"
                       f" SOL); the equity curve adds all their P&L onto the first session's {start:g} SOL - "
                       "use --session for one run's exact curve")
        gates, model = [], None
    out = Path(args.out) if args.out else DATA / "report.html"
    html_path, csv_path = write(closed, eq, sn.capital.starting_sol, sn.capital.max_drawdown_pct, out,
                                args.title, source, gates, model, sn.execution.curve_fee_pct + sn.execution.platform_fee_pct)
    print(f"report: {html_path}\ntrades: {csv_path}")


def _compare(args, params) -> None:
    from .compare import compare, parse_seeds, parse_variant, report
    from .feeds import dedupe_feed_paths

    variants = [parse_variant(v) for v in args.variant]
    if args.file:
        samples = [(Path(f).name.split(".")[0], {"files": [str(f)]}) for f in dedupe_feed_paths(args.file)]
    else:
        samples = [(f"seed {sd}", {"synthetic": args.synthetic, "seed": sd}) for sd in parse_seeds(args.seeds)]
    jobs = args.jobs or max(1, min((os.cpu_count() or 2) - 1, 8))     # leave a core for a running bot
    res = compare(params, variants, samples, jobs)
    print(report(res, synthetic=not args.file))


def _synthetic_with_leaders(args, params) -> SyntheticFeed:
    feed = SyntheticFeed(seed=args.seed, speed=0, launches=args.synthetic, start_ts=1_780_000_000)
    _sim_leaders(params, feed)
    return feed


def _review(args, params) -> None:
    from . import review

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("review needs ANTHROPIC_API_KEY (add it to .env)")
    data = review.gather(args.days, dict(params["sniper"]))
    if not data["trade_count"]:
        print("no closed trades in data/ yet - run the bot (paper is fine) first")
        return
    rv = review.ask_claude(data, params.sniper.desk.model)
    rv["param_changes"], rv["rejected_changes"] = review.validate_changes(rv["param_changes"], dict(params["sniper"]))
    validation = None
    if args.validate and rv["param_changes"] and data["recorded_feed_files"]:
        files = data["recorded_feed_files"]
        base = asyncio.run(run_backtest(_copy.deepcopy(params), FileFeed(*files))).summary()
        try:
            prop = apply_overrides(_copy.deepcopy(params), [c["arg"] for c in rv["param_changes"]])
        except ValueError as e:
            sys.exit(f"proposed settings are invalid together: {e}")
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
    sw = sub.add_parser("sweep", help="walk-forward parameter tuning on recorded data")
    sw.add_argument("--grid", action="append", required=True,
                    help="key=v1,v2,... e.g. exit.stop_loss_pct=20,30,40 (repeat for more keys)")
    sw.add_argument("--train", type=float, default=0.6, help="share of launches used for tuning")
    sw.add_argument("--metric", choices=["pnl", "pf"], default="pnl")
    sw.add_argument("--min-trades", type=int, default=10)
    sw.add_argument("--top", type=int, default=3)
    sw.add_argument("--jobs", type=int, default=0, help="parallel processes (default: CPU cores - 1)")
    tr = sub.add_parser("train", help="fit and grade the P(2x first) model on recorded data")
    tr.add_argument("--train", type=float, default=0.7, help="share of launches used for fitting (rest grades it)")
    tr.add_argument("--out", help="candidate model file (default: <model_path>-candidate.json)")
    pm = sub.add_parser("promote", help="deploy the trained candidate model (recorded data + skill required)")
    pm.add_argument("--model", dest="file_model", help="candidate file (default: <model_path>-candidate.json)")
    pm.add_argument("--force", action="store_true", help="promote even with low held-out skill")
    rp = sub.add_parser("report", help="shareable HTML performance report + trades CSV")
    rp.add_argument("--backtest", action="store_true", help="report a backtest instead of recorded trades")
    rp.add_argument("--days", type=int, help="recorded trades: only the last N days")
    rp.add_argument("--out", help="output HTML (default: data/report.html)")
    rp.add_argument("--title", default="Sniper performance report")
    rp.add_argument("--mode", help="recorded trades: paper | live | paper-synthetic | unknown | all")
    rp.add_argument("--session", help="recorded trades: only this session id (see the trades CSV)")
    cp = sub.add_parser("compare", help="A/B test setting variants across many market samples, in parallel")
    cp.add_argument("--variant", action="append", default=[], help="'name: key=value key=value' (repeat)")
    cp.add_argument("--seeds", default="1-6", help="synthetic market seeds, e.g. 1-8 or 1,3,5")
    cp.add_argument("--jobs", type=int, default=0, help="parallel processes (default: CPU cores - 1)")
    cp.add_argument("--file", nargs="*", help="recorded feed files: each file (one UTC day) is one sample")
    cp.add_argument("--synthetic", type=int, default=1000, help="launches per synthetic sample")
    for p in (b, lead, sw, tr, rp):
        p.add_argument("--file", nargs="*", help="recorded feed file(s): data/feed-*")
        p.add_argument("--synthetic", type=int, default=1000, help="number of synthetic launches if no --file")
        p.add_argument("--seed", type=int, default=1)
    rv = sub.add_parser("review", help="AI post-mortem of recent trades")
    rv.add_argument("--days", type=int, default=3)
    rv.add_argument("--validate", action="store_true", help="backtest the proposed changes on recorded data")
    sub.add_parser("doctor", help="check setup")
    from .research import add_parser as research_parser

    research_parser(sub)
    for p in (r, b, lead, sw, tr, rp, cp, rv, pm, sub.choices["doctor"]):
        p.add_argument("--config")
        p.add_argument("--set", action="append", help="override a sniper param, e.g. exit.stop_loss_pct=25")
    args = ap.parse_args()
    if args.cmd == "research":                     # policies carry their own settings (not params.yaml)
        from .research import main as research

        try:
            research(args)
        except config.ConfigError as e:
            sys.exit(f"research: {e}")
        return
    try:
        params = apply_overrides(config.load(args.config), args.set)
    except config.ConfigError as e:
        sys.exit(f"config error: {e}")
    if args.cmd == "run":
        asyncio.run(_run(args, params))
    elif args.cmd == "backtest":
        asyncio.run(_backtest(args, params))
    elif args.cmd == "leaders":
        _leaders(args, params)
    elif args.cmd == "review":
        _review(args, params)
    elif args.cmd == "sweep":
        _sweep(args, params)
    elif args.cmd == "train":
        _train(args, params)
    elif args.cmd == "promote":
        _promote(args, params)
    elif args.cmd == "report":
        _report(args, params)
    elif args.cmd == "compare":
        _compare(args, params)
    else:
        from .doctor import run as doctor

        doctor(params)


if __name__ == "__main__":
    main()
