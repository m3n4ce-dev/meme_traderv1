"""Market caps in and out for trades closed before they were recorded (2026-10-05), read back from the recorded
feed: every pump.fun trade line carries the curve's reserves after it, and market cap = price x supply.

    python -m meme_trader.sniper mcap-backfill

Writes data/mcap_backfill.json ("mint:opened" -> {entry_mcap_sol, exit_mcap_sol}); the bot reads it at startup and
fills in the trades it lists. Read-only on the trade and feed files."""
from __future__ import annotations

import bisect
import glob
import gzip
import json
import re
from pathlib import Path

from .curve import TOTAL_SUPPLY

MINT = re.compile(r'^\{"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')


def key(row: dict) -> str:
    return f"{row.get('mint')}:{int(row.get('opened') or 0)}"


def run(data: Path) -> dict:
    rows = [json.loads(l) for f in sorted(glob.glob(str(data / "trades-*.jsonl"))) for l in open(f) if l.strip()]
    need: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("entry_mcap_sol") and r.get("mint") and r.get("source") != "callout":
            need.setdefault(r["mint"], []).append(r)
    pts: dict[str, list[tuple[float, float]]] = {m: [] for m in need}
    for f in sorted(glob.glob(str(data / "feed-*.jsonl*"))):
        op = gzip.open if f.endswith(".gz") else open
        try:
            with op(f, "rt", errors="replace") as fh:
                for line in fh:
                    m = MINT.match(line)
                    if not m or m.group(1) not in pts or '"v_sol"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("v_sol", 0) > 0 and d.get("v_tokens", 0) > 0:
                        pts[d["mint"]].append((d["ts"], d["v_sol"] / d["v_tokens"] * TOTAL_SUPPLY))
        except (EOFError, OSError):                 # a feed file a crash cut short: use what it has
            continue
    out = {}
    for mint, trs in need.items():
        p = sorted(pts[mint])
        if not p:
            continue
        ts = [t for t, _ in p]

        def at(t: float) -> float | None:
            i = bisect.bisect_right(ts, t + 3)      # the fill lands a few seconds after the decision
            return p[i - 1][1] if i else None
        for r in trs:
            mc_in, mc_out = at(r["opened"]), at(r["closed"])
            if mc_in:
                out[key(r)] = {"entry_mcap_sol": round(mc_in, 2), "exit_mcap_sol": round(mc_out, 2) if mc_out else None}
    path = data / "mcap_backfill.json"
    path.write_text(json.dumps(out))
    return {"trades_missing": sum(len(v) for v in need.values()), "filled": len(out), "path": str(path)}


def apply(rows: list[dict], data: Path, sol_usd: float) -> int:
    """Fill rows that lack market caps from data/mcap_backfill.json (dollars at today's SOL price: approximate)."""
    try:
        fill = json.loads((data / "mcap_backfill.json").read_text())
    except (OSError, ValueError):
        return 0
    n = 0
    for r in rows:
        f = fill.get(key(r)) if not r.get("entry_mcap_sol") else None
        if f:
            r.update(f, mcap_approx=True)
            if sol_usd:
                r["entry_mcap_usd"] = round(f["entry_mcap_sol"] * sol_usd)
                r["exit_mcap_usd"] = round(f["exit_mcap_sol"] * sol_usd) if f.get("exit_mcap_sol") else None
            n += 1
    return n
