"""Post to X and Telegram from a terminal: the same accounts and limits as the dashboard's composer.

    python -m meme_trader.social status
    python -m meme_trader.social post "gm. 3 graduation plays today" --to x,telegram
    python -m meme_trader.social post "Track record so far" --card record
    python -m meme_trader.social post "Called it" --card call:<id> --to telegram
    python -m meme_trader.social proof --to x          # publish the call ledger's newest hash

X bills each post (about $0.015, or $0.20 with a link): every command shows the price and asks first (--yes skips).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import ROOT
from .journal import DATA


def _env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def _card(spec: str) -> bytes | None:
    from .sniper.calls import CallLedger
    from .ui import cards

    kind, _, ident = (spec or "").partition(":")
    L = CallLedger(DATA / "calls.jsonl")
    if kind == "record":
        return cards.record_card(L.stats(ident), L.head)
    if kind == "call":
        row = next((r for r in L.rows(limit=100_000) if r["id"] == ident or str(r["n"]) == ident), None)
        if row is None:
            sys.exit(f"no call {ident!r} in the ledger")
        return cards.call_card(row)
    if kind == "trade":
        rows = []
        for f in sorted(DATA.glob("trades-*.jsonl"))[-14:]:
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        t = next((r for r in reversed(rows) if r.get("mint") == ident or r.get("symbol", "").lower() == ident.lower()), None)
        if t is None:
            sys.exit(f"no closed trade for {ident!r} in the last 14 days")
        return cards.trade_card(t, None, t.get("mode", "paper"))
    sys.exit("--card is record[:caller], call:<id or number> or trade:<mint or symbol>")


def main() -> None:
    from .ui.social import Social, SocialError, x_cost, x_length

    ap = argparse.ArgumentParser(prog="python -m meme_trader.social", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("post")
    p.add_argument("text")
    p.add_argument("--to", default="x", help="x, telegram, or x,telegram")
    p.add_argument("--card", help="attach an image: record[:caller], call:<id>, trade:<mint or symbol>")
    p.add_argument("--yes", action="store_true", help="don't ask before posting")
    pr = sub.add_parser("proof", help="publish the call ledger's newest hash")
    pr.add_argument("--to", default="x")
    pr.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    _env()
    soc = Social(DATA)
    if args.cmd == "status":
        st = soc.status()
        x, tg = st["x"], st["telegram"]
        print(f"X: {('connected via ' + x['method']) if x['connected'] else 'not connected'}"
              + (f" as @{x['user']}" if x["user"] else "")
              + f" · {x['posts_today']} posts today (~${x['spent_today_usd']:.3f}) of {x['max_per_day']}")
        print(f"Telegram: {'posting to ' + str(tg['chat']) if tg['configured'] else 'not set up'}")
        return
    to = tuple(t.strip() for t in args.to.split(",") if t.strip())
    if any(t not in ("x", "telegram") for t in to):
        sys.exit("--to takes x, telegram, or both")
    if args.cmd == "proof":
        from .sniper.calls import CallLedger, proof_text

        L = CallLedger(DATA / "calls.jsonl")
        v = L.verify()
        if not v["ok"]:
            sys.exit("the ledger fails its own check: " + v["text"])
        text, png = proof_text(L), None
    else:
        text, png = args.text, (_card(args.card) if args.card else None)
    if "x" in to:
        print(f"X: {x_length(text)}/280 characters, about ${x_cost(text):.3f}"
              + (" (it contains a link)" if x_cost(text) > 0.1 else ""))
    print("---\n" + text + "\n---" + ("\n+ image card" if png else ""))
    if not args.yes and input(f"Post to {', '.join(to)}? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("not posted")
    res = asyncio.run(soc.post(text, png, to))
    ok = False
    for ch, r in res.items():
        if r.get("ok"):
            ok = True
            print(f"✓ {ch}: {r.get('url') or 'posted'}" + (f" ({r['note']})" if r.get("note") else ""))
            if args.cmd == "proof":
                from .sniper.calls import CallLedger

                CallLedger(DATA / "calls.jsonl").anchor(ch, r.get("url", ""))
        else:
            print(f"✕ {ch}: {r.get('error')}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
