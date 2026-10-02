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

    params = config.load(args.config)
    wallet = None
    if params.mode == "live":
        if os.environ.get("MEME_TRADER_CONFIRM_LIVE") != "yes":
            sys.exit("live mode refused: set MEME_TRADER_CONFIRM_LIVE=yes to trade real funds")
        from .wallet import Wallet

        wallet = Wallet(params.wallet.pubkey)
        bal = wallet.sol_balance()
        if bal < params.capital.starting_sol:
            sys.exit(f"wallet holds {bal:.4f} SOL < capital.starting_sol {params.capital.starting_sol}")
    Orchestrator(params, wallet).run(once=args.once)


if __name__ == "__main__":
    main()
