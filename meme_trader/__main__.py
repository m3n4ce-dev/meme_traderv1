"""python -m meme_trader [--once] [--config path]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import config
from .orchestrator import Orchestrator


def main() -> None:
    ap = argparse.ArgumentParser(prog="meme_trader")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--config", help="params file (default config/params.yaml)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    try:
        params = config.load(args.config)
    except config.ConfigError as e:
        sys.exit(f"config error: {e}")
    wallet = None
    if params.mode == "live":
        if os.environ.get("MEME_TRADER_CONFIRM_LIVE") != "yes":
            sys.exit("live mode refused: set MEME_TRADER_CONFIRM_LIVE=yes to trade real funds")
        from .wallet import Wallet

        from .agents.risk import state_path

        wallet = Wallet(params.wallet.pubkey)
        bal = wallet.sol_balance()
        resuming = state_path("live", wallet.pubkey).exists()     # part of the budget may sit in positions
        if bal < (params.capital.min_sol_reserve if resuming else params.capital.starting_sol):
            sys.exit(f"wallet holds {bal:.4f} SOL - not enough to {'resume' if resuming else 'start'} live trading")
    try:
        orch = Orchestrator(params, wallet)
    except RuntimeError as e:
        sys.exit(str(e))
    orch.run(once=args.once)


if __name__ == "__main__":
    main()
