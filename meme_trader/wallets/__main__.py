"""Wallet study CLI (docs/WALLETS.md).

  python -m meme_trader.wallets record                 # the recorder (runs as the meme-wallets user service)
  python -m meme_trader.wallets status                 # what it has collected
  python -m meme_trader.wallets register wallets-v1    # fix the rules before looking at data
  python -m meme_trader.wallets select wallets-v1      # period A so far: moves and qualifying wallets
  python -m meme_trader.wallets freeze wallets-v1      # lock the wallet list (after period A)
  python -m meme_trader.wallets eval wallets-v1        # period B: signals, trades, baselines, verdict
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

from .. import config
from . import study
from .recorder import DATA, Recorder


def _status() -> None:
    p = DATA / "status.json"
    if not p.exists():
        print("No recorder status yet (is the meme-wallets service running?)")
        return
    s = json.loads(p.read_text())
    age = time.time() - s["updated"]
    d = s.get("discovery") or {}
    print(f"updated {age:.0f}s ago; running {(s['updated'] - s['started']) / 3600:.1f} h")
    print(f"pools: {s['pools_known']} known, {s['pools_eligible']} in the universe, {s['pools_polled']} polled, "
          f"{s['overdue']} overdue; GeckoTerminal calls last hour {s['gt_calls_last_hour']}")
    print(f"trades this run: {s['rows_this_run']} {s['rows_by_day']}; overflow gaps {s['gaps']}")
    if s.get("stream_gb_by_day"):
        run_h = max((s["updated"] - s["started"]) / 3600, 1e-6)
        total = sum(s["stream_gb_by_day"].values())
        print(f"stream download: {s['stream_gb_by_day']} GB (~{total / run_h * 24:.0f} GB/day at this rate)")
    if s.get("coverage_est") is not None:
        print(f"estimated coverage of trades in the universe: {s['coverage_est']:.0%}")
    if d:
        print(f"last discovery: {d.get('swaps')} swaps on {d.get('pools')} pools in {d.get('secs')}s "
              f"({d.get('new_pools')} new) via {d.get('url')}")
    files = study.trade_files()
    print(f"files: {len(files)} ({sum(f.stat().st_size for f in files) / 1e6:.1f} MB)")
    for e in s.get("errors") or []:
        print("  !", e)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m meme_trader.wallets", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["record", "status", "register", "select", "freeze", "eval"])
    ap.add_argument("policy", nargs="?", default="wallets-v1")
    ap.add_argument("--quick", action="store_true", help="eval: fewer baseline draws")
    ap.add_argument("--json", action="store_true", help="select/eval: print JSON")
    a = ap.parse_args(argv)
    if a.cmd == "record":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        cfg = config.load().wallets
        asyncio.run(Recorder(cfg).run())
        return
    if a.cmd == "status":
        _status()
        return
    try:
        pol = study.load_policy(a.policy)
        if a.cmd == "register":
            lock = study.register(pol)
            print(f"{a.policy}: rules {lock['rules_signature']} registered at {lock['registered_at']}")
        elif a.cmd == "select":
            res = study.cmd_select(pol)
            res.pop("move_list")
            if a.json:
                print(json.dumps({k: v for k, v in res.items() if k != "control"}, indent=1, default=str))
            else:
                study.print_select(res, pol)
        elif a.cmd == "freeze":
            lock = study.cmd_freeze(pol)
            print(f"{a.policy}: {len(lock['wallets'])} wallets frozen at {lock['frozen_at']} "
                  f"(period A {lock['period_a'][0]} .. {lock['period_a'][1]})")
        elif a.cmd == "eval":
            rep = study.cmd_eval(pol, quick=a.quick)
            if a.json:
                print(json.dumps(rep, indent=1, default=str))
            else:
                study.print_eval(rep)
    except study.StudyError as e:
        sys.exit(f"{a.policy}: {e}")


if __name__ == "__main__":
    main()
