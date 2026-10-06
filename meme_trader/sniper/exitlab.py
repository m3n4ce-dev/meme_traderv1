"""The exit lab: other exit rules, run in the shadow of every bot entry on the same live prices.

When the bot opens a position, the lab opens a "shadow" copy for each variant below and runs the bot's own exit code
on it with the variant's settings, tick by tick, until it exits (or 30 minutes pass). Fills are instant and fees are
counted on both sides, so the variants compare fairly with each other and with "as now" (today's settings, also
instant). Nothing is traded, and the frozen strategy isn't changed: adopting a better exit would be a new strategy
version with its own test. Results go to data/exit_lab.jsonl.

The idea comes from a public bot's calibration notes ("exit variants riding the same paper entries"): judge exits on
identical entries, so a difference in results is the exit's doing, not luck in what was bought.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import replace
from pathlib import Path

from ..config import Params
from .strategy import SniperPosition, evaluate_exit, evaluate_late_exit

MAX_OPEN_S = 1800


def _late_variants(L) -> dict[str, dict]:
    return {
        "as now": {},
        "stop 10%": {"stop_loss_pct": 10},
        "stop 25%": {"stop_loss_pct": 25},
        "stall 90 s": {"stall_s": 90},
        "hold to 98% of the curve": {"exit_curve_pct": 98},
        "take profit at 2x": {"_tp": 100},
        "trail 20% once up 30%": {"_trail": (30, 20)},
        # bank gains early, many small wins (Cupsy's style: "take your profit, stop hunting home runs")
        "bank half at +30%": {"_half": 30},
        "all out at +50%": {"_tp": 50},
    }


def _sniper_variants(x) -> dict[str, dict]:
    return {
        "as now": {},
        "stop 20%": {"stop_loss_pct": 20},
        "stop 45%": {"stop_loss_pct": 45},
        "no stall exit": {"stall_s": 10 ** 9},
        "take profit at 2x": {"_tp": 100},
        "trail 20% once up 30%": {"_trail": (30, 20)},
    }


LATE_KINDS = ("late", "desk-pass", "practice-buy", "practice-pass")   # followed on the graduation-play exits

class ExitLab:
    def __init__(self, path: Path | None, fee_pct: float):
        self.path = Path(path) if path else None
        self.fee = fee_pct
        self.open: dict[str, list[dict]] = {}          # mint -> shadows still running
        self.on_close = None                            # called with each closed row (the engine scores team votes)
        self.done: list[dict] = []                      # closed shadows (also in the file)
        if self.path and self.path.exists():
            rows = []
            for line in self.path.read_text().splitlines()[-50_000:]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            newest = max((r.get("closed", 0) for r in rows), default=0)
            self.done = [r for r in rows if r.get("closed", 0) >= newest - 14 * 86400]     # the last 14 days of it

    def mints(self) -> set[str]:
        return set(self.open)

    def start(self, mint: str, symbol: str, kind: str, price: float, now: float, p, only: tuple | None = None) -> None:
        """A bot entry at `price` (p: params.sniper). kind 'late' runs the graduation-play exits, else the sniper's.
        kind 'desk-pass': a graduation coin the AI desk turned down, followed with the bot's own exits ("as now")
        so Analytics can show what the desk's passes would have made. 'practice-buy' / 'practice-pass': a team vote
        that bought nothing (practice, or an approval that couldn't be sized), followed the same way."""
        if not price or mint in self.open:
            return
        late = kind in LATE_KINDS
        variants = _late_variants(p.late) if late else _sniper_variants(p.exit)
        if only:
            variants = {k: v for k, v in variants.items() if k in only}
        base = SniperPosition(mint=mint, symbol=symbol, opened_at=now, entry_price=price, tokens=1 / price,
                              initial_tokens=1 / price, cost_sol=1.0, initial_cost_sol=1.0, score=0.0,
                              peak_price=price, exits=[], source=kind)
        self.open[mint] = [{"variant": name, "over": over, "late": late, "pos": replace(base, exits=[]),
                            "proceeds": 0.0, "opened": now, "symbol": symbol, "kind": kind}
                           for name, over in variants.items()]

    def _rule(self, sh: dict, s, now: float, p):
        pos, over = sh["pos"], sh["over"]
        gain = pos.gain_pct(s.curve.price)
        pos.peak_price = max(pos.peak_price, s.curve.price)
        if "_tp" in over and gain >= over["_tp"]:
            return 1.0, f"take profit +{gain:.0f}%"
        if "_half" in over and not sh.get("half_done") and gain >= over["_half"]:
            sh["half_done"] = True                       # once; the rest runs on the normal exits
            return 0.5, f"half out at +{gain:.0f}%"
        if "_trail" in over:
            up, trail = over["_trail"]
            peak = pos.gain_pct(pos.peak_price)
            if peak >= up and s.curve.price <= pos.peak_price * (1 - trail / 100):
                return 1.0, f"trail {trail}% off +{peak:.0f}%"
        plain = {k: v for k, v in over.items() if not k.startswith("_")}
        if sh["late"]:
            return evaluate_late_exit(pos, s, now, Params({**p.late, **plain}), p.exit)
        return evaluate_exit(pos, s, now, Params({**p.exit, **plain}), self.fee)

    def tick(self, tokens: dict, now: float, p) -> None:
        for mint, shadows in list(self.open.items()):
            s = tokens.get(mint)
            for sh in shadows:
                if sh.get("closed"):
                    continue
                if s is None or not s.price_known:
                    if now - sh["opened"] >= MAX_OPEN_S:
                        self._close(sh, None, now, "token gone")
                    continue
                r = (1.0, "30 min limit") if now - sh["opened"] >= MAX_OPEN_S else self._rule(sh, s, now, p)
                if not r:
                    continue
                frac, why = r
                pos, price = sh["pos"], s.curve.price
                sold = pos.tokens * min(max(frac, 0.0), 1.0)
                sh["proceeds"] += sold * price * (1 - self.fee / 100)
                pos.tokens -= sold
                if frac < 1:
                    pos.initials_taken = True
                if frac >= 1 or pos.tokens * price < 1e-6:
                    self._close(sh, price, now, why)
            if all(sh.get("closed") for sh in shadows):
                del self.open[mint]

    def _close(self, sh: dict, price: float | None, now: float, why: str) -> None:
        pos = sh["pos"]
        if price:
            sh["proceeds"] += pos.tokens * price * (1 - self.fee / 100)
        cost = 1.0 * (1 + self.fee / 100)
        row = {"closed": now, "opened": sh["opened"], "mint": pos.mint, "symbol": sh["symbol"], "kind": sh["kind"],
               "variant": sh["variant"], "pnl_pct": round((sh["proceeds"] / cost - 1) * 100, 2), "why": why,
               "held_s": round(now - sh["opened"])}
        sh["closed"] = True
        self.done.append(row)
        if self.on_close is not None:
            self.on_close(row)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")

    def view(self, kind: str = "late") -> dict:
        """Per variant: entries, mean and median P&L % per entry, win rate; sorted by mean, best first."""
        rows = [r for r in self.done if r["kind"] == kind or (kind == "sniper" and r["kind"] not in LATE_KINDS)]
        by: dict[str, list[float]] = {}
        for r in rows:
            by.setdefault(r["variant"], []).append(r["pnl_pct"])
        out = [{"variant": v, "n": len(xs), "mean_pct": statistics.fmean(xs), "median_pct": statistics.median(xs),
                "win_rate": sum(1 for x in xs if x > 0) / len(xs), "sum_pct": sum(xs)} for v, xs in by.items()]
        out.sort(key=lambda r: -r["mean_pct"])
        entries = len({(r["mint"], r["opened"]) for r in rows})
        return {"kind": kind, "entries": entries, "open": sum(1 for v in self.open.values() for sh in v
                                                              if not sh.get("closed") and (sh["kind"] == kind)),
                "variants": out}
