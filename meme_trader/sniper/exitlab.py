"""The exit lab: other exit rules, run in the shadow of every bot entry on the same live prices.

When the bot opens a position, the lab opens a "shadow" copy for each variant below and runs the bot's own exit code
on it with the variant's settings, tick by tick, until it exits (or 30 minutes pass). Nothing is traded, and the
frozen strategy isn't changed: adopting a better exit would be a new strategy version with its own test. Results go
to data/exit_lab.jsonl.

The accounting (version 2, a ninth review, 2026-10-07): each shadow has ONE cash ledger, normalized to its entry's
cash debit (= 1). A bot entry starts from the bot's own fill - its cash (fees and network cost included) and tokens,
never charged again; an observation-only entry (a pass, a pick) starts from the raw curve price and pays the buy fee
and network cost once. Every sell, partial or full, books its receipt after the sell fee and its own network cost; a
failed attempt books its network cost. The trigger (`net_gain_pct`) and the recorded result use that same ledger, and
each row carries its entry and cash lines, so it reconciles. Variants marked "lands N s late" fill N s after the
decision at the price then, and each of their sells fails LAB_SELL_FAIL_PCT of the time (a declared assumption, not a
measured rate) and is retried. After an early exit, the price is followed to the 30-minute mark ("after_exit" rows):
what the exit gave up or avoided. Rows from before version 2 (no "accounting") mixed conventions and aren't shown.

The idea comes from a public bot's calibration notes ("exit variants riding the same paper entries"): judge exits on
identical entries, so a difference in results is the exit's doing, not luck in what was bought.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import replace
from pathlib import Path

from ..config import Params
from .strategy import SniperPosition, evaluate_exit, evaluate_late_exit

MAX_OPEN_S = 1800


# the reviewer's take-profit experiment (2026-10-07): +10% NET of the whole position - what selling all of it now
# would bring after the sell fee and both transactions' network costs, against what the buy cost with its fee and
# network cost - not +10% on the price (that's about +5% net on the curve's fees; +10% net needs ~+15% on the price).
# Otherwise the bot's own exits, unchanged. The trim variant sells a quarter there and lets the rest ride them.
TP10 = {"all out at +10% net": {"_tp_net": 10}, "trim 25% at +10% net": {"_trim_net": (10, 0.25)}}
TX_COST_SOL = 0.000005                              # a transaction's base fee; its priority fee comes from the config
ACCOUNTING = 2                                      # the cash-ledger version recorded in each row
LAB_SELL_FAIL_PCT = 10                              # late-landing variants: a sell attempt fails this often (assumed)
MAX_SELL_TRIES = 20                                 # then the shadow's exit never filled: the tokens count as lost


def _late_landing(delay_s: float) -> dict[str, dict]:
    """The TP10 exits and their baseline, landing as late as the paper bot does, with failed and retried sells."""
    if delay_s <= 0:
        return {}
    tag = f" · lands {delay_s:g} s late"
    late = {"_delay": delay_s, "_fail": LAB_SELL_FAIL_PCT}
    return {"as now" + tag: dict(late), **{name + tag: {**over, **late} for name, over in TP10.items()}}


def _late_variants(L, delay_s: float = 0.0) -> dict[str, dict]:
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
        **TP10,
        **_late_landing(delay_s),
    }


def _sniper_variants(x, delay_s: float = 0.0) -> dict[str, dict]:
    return {
        "as now": {},
        "stop 20%": {"stop_loss_pct": 20},
        "stop 45%": {"stop_loss_pct": 45},
        "no stall exit": {"stall_s": 10 ** 9},
        "take profit at 2x": {"_tp": 100},
        "trail 20% once up 30%": {"_trail": (30, 20)},
        # a trader's "4 cuts" (2026-10-06): a quarter of the bag as the curve passes 25, 50 and 75%; the last quarter
        # rides the usual exits, which already sell everything when the creator sells
        "curve ladder 25/50/75%": {"_curve_ladder": (25, 50, 75)},
        **TP10,
        **_late_landing(delay_s),
    }


# the stronger model's picks (P(2x) from the trees at a checkpoint age): stop, trail and time-limit exits. Picked on
# recordings (Oct 3-6, 2026): with instant fills these made money on two unseen days; with fills 2.5 s late, about
# nothing. So each runs twice, filled instantly and as late as the paper bot lands: the gap is what speed is worth.
PICK_EXITS = {                                     # (take profit %, stop %, sell by s, trail once up %, trail %)
    "stop 40, trail 30 once up 50, out at 3 min": (None, 40, 180, 50, 30),
    "stop 40, trail 25 once up 30, out at 3 min": (None, 40, 180, 30, 25),
    "stop 40, trail 25 once up 30, out at 30 min": (None, 40, 1800, 30, 25),
    "+100% or -30%, out at 10 min": (100, 30, 600, None, None),
}


def _pick_variants(delay_s: float) -> dict[str, dict]:
    out = {}
    for name, b in PICK_EXITS.items():
        out[name] = {"_bracket": b}
        if delay_s > 0:
            out[f"{name} · lands {delay_s:g} s late"] = {"_bracket": b, "_delay": delay_s}
    out["the sniper's exits"] = {}
    return out


LATE_KINDS = ("late", "desk-pass", "practice-buy", "practice-pass", "late-blocked")   # on the graduation-play exits
SNIPER_CALLS = ("sniper-pass", "sniper-skip")      # sniper votes that bought nothing: followed on the sniper's exits
PICKS = "model-pick"                               # the trees' picks: a follow, not a trade
PICK_BANDS = (("30%+", .30, 1.01), ("25-30%", .25, .30))

class ExitLab:
    def __init__(self, path: Path | None, fee_pct: float):
        self.path = Path(path) if path else None
        self.fee = fee_pct
        self.open: dict[str, list[dict]] = {}          # mint -> shadows still running
        self.after: list[dict] = []                     # early exits whose coin is still followed (to 30 minutes)
        self.on_close = None                            # called with each closed row (the engine scores team votes)
        self.done: list[dict] = []                      # closed shadows (also in the file)
        if self.path and self.path.exists():
            rows = []
            for line in self.path.read_text().splitlines()[-50_000:]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            rows = [r for r in rows if "record" not in r]
            newest = max((r.get("closed", 0) for r in rows), default=0)
            self.done = [r for r in rows if r.get("closed", 0) >= newest - 14 * 86400]     # the last 14 days of it

    def mints(self) -> set[str]:
        return set(self.open) | {g["mint"] for g in self.after}

    def start(self, mint: str, symbol: str, kind: str, price: float, now: float, p, only: tuple | None = None,
              extra: dict | None = None, stake_sol: float = 0.25, price_kind: str = "all_in_fill",
              entry: dict | None = None) -> None:
        """A bot entry at `price` (p: params.sniper). kind 'late' runs the graduation-play exits, else the sniper's.
        kind 'desk-pass': a graduation coin the AI desk turned down, followed with the bot's own exits ("as now")
        so Analytics can show what the desk's passes would have made. 'practice-buy' / 'practice-pass': a team vote
        that bought nothing (practice, or an approval that couldn't be sized), followed the same way. 'model-pick':
        the trees' pick, on PICK_EXITS (extra: its P(2x), carried into each row). One follow per mint and kind.

        The entry, explicitly (a ninth review): `entry` = the bot's fill {"cash_sol", "tokens"} (fees and network
        cost already inside its cash); else price_kind "all_in_fill" (`price` is cash per token, `stake_sol` the cash)
        or "raw_mark" (`price` is the curve's price: the buy fee and network cost on `stake_sol` are paid here, once)."""
        if not price or any(sh["kind"] == kind for sh in self.open.get(mint, ())):
            return
        late = kind in LATE_KINDS
        delay = float(p.execution.get("paper_delay_s", 0) or 0)
        variants = _late_variants(p.late, delay) if late else \
            _pick_variants(delay) if kind == PICKS else _sniper_variants(p.exit, delay)
        if only:
            variants = {k: v for k, v in variants.items() if k in only}
        tx_sol = float(p.execution.get("priority_fee_sol", 0) or 0) + TX_COST_SOL
        if entry:
            price_kind, stake_sol, price = "all_in_fill", float(entry["cash_sol"]), entry["cash_sol"] / entry["tokens"]
        base_entry = self._entry(price, price_kind, stake_sol, tx_sol)
        tokens = base_entry["tokens"]
        base = SniperPosition(mint=mint, symbol=symbol, opened_at=now, entry_price=1 / tokens, tokens=tokens,
                              initial_tokens=tokens, cost_sol=1.0, initial_cost_sol=1.0, score=0.0,
                              peak_price=1 / tokens, exits=[], source=kind)
        self.open.setdefault(mint, []).extend(
            {"variant": name, "over": over, "late": late, "pos": replace(base, exits=[]), "opened": now,
             "symbol": symbol, "kind": kind, "extra": extra or {},
             "land_at": now + (over.get("_delay", 0) if price_kind == "raw_mark" else 0),   # (a bot fill has landed)
             "tx": base_entry["tx"], "entry": dict(base_entry), "cash": [], "tries": 0, "pending": None}
            for name, over in variants.items())

    def _entry(self, price: float, price_kind: str, stake_sol: float, tx_sol: float) -> dict:
        """The entry, normalized to its cash debit: tokens per unit of cash, and a transaction's network cost."""
        f = self.fee / 100
        if price_kind == "raw_mark":
            debit = stake_sol + tx_sol                   # the stake, plus the buy's network cost
            tokens = stake_sol * (1 - f) / (price * debit)
        else:
            debit, tokens = stake_sol, 1 / price         # the bot's cash already holds its fees and network cost
        return {"price_kind": price_kind, "price": price, "cash_sol": round(debit, 9), "tokens": tokens,
                "tx": tx_sol / debit}

    def _rule(self, sh: dict, s, now: float, p):
        pos, over = sh["pos"], sh["over"]
        gain = pos.gain_pct(s.curve.price)
        pos.peak_price = max(pos.peak_price, s.curve.price)
        if "_bracket" in over:
            tp, stop, by_s, arm, trail = over["_bracket"]
            if tp is not None and gain >= tp:
                return 1.0, f"take profit +{gain:.0f}%"
            if gain <= -stop:
                return 1.0, f"stop {gain:.0f}%"
            peak = pos.gain_pct(pos.peak_price)
            if trail is not None and peak >= arm and s.curve.price <= pos.peak_price * (1 - trail / 100):
                return 1.0, f"trail {trail}% off +{peak:.0f}%"
            if now - sh["opened"] >= by_s:                # counted from the decision, as in the replays
                return 1.0, f"out at {by_s // 60} min"
            return None
        if "_tp" in over and gain >= over["_tp"]:
            return 1.0, f"take profit +{gain:.0f}%"
        if "_tp_net" in over or "_trim_net" in over:
            net = self.net_gain_pct(sh, s.curve.price)
            if "_tp_net" in over and net >= over["_tp_net"]:
                return 1.0, f"take +{net:.1f}% net"
            if "_trim_net" in over and not sh.get("trim_done") and net >= over["_trim_net"][0]:
                sh["trim_done"] = True                   # once; the rest runs on the bot's own exits
                return over["_trim_net"][1], f"trim {over['_trim_net'][1]:.0%} at +{net:.1f}% net"
        if "_half" in over and not sh.get("half_done") and gain >= over["_half"]:
            sh["half_done"] = True                       # once; the rest runs on the normal exits
            return 0.5, f"half out at +{gain:.0f}%"
        if "_curve_ladder" in over:
            levels, i = over["_curve_ladder"], sh.get("rung", 0)
            if i < len(levels) and s.curve.progress * 100 >= levels[i]:
                sh["rung"] = i + 1                       # a quarter of the original bag per rung
                share = pos.initial_tokens / (len(levels) + 1)
                return min(share / max(pos.tokens, 1e-12), 1.0), f"curve ladder {levels[i]}%"
        if "_trail" in over:
            up, trail = over["_trail"]
            peak = pos.gain_pct(pos.peak_price)
            if peak >= up and s.curve.price <= pos.peak_price * (1 - trail / 100):
                return 1.0, f"trail {trail}% off +{peak:.0f}%"
        plain = {k: v for k, v in over.items() if not k.startswith("_")}
        if sh["late"]:
            return evaluate_late_exit(pos, s, now, Params({**p.late, **plain}), p.exit)
        return evaluate_exit(pos, s, now, Params({**p.exit, **plain}), self.fee)

    def net_gain_pct(self, sh: dict, price: float) -> float:
        """The whole position's net P&L % if the rest were sold now, from its cash ledger: every receipt and failed
        attempt so far, plus the rest at `price` after the sell fee and this sell's network cost, against the entry's
        cash (= 1). The recorded result is the same sum, at the price it actually closed at."""
        rest = sh["pos"].tokens * price * (1 - self.fee / 100) - sh["tx"] if sh["pos"].tokens > 0 else 0.0
        return (sum(x["sol"] for x in sh["cash"]) + rest - 1) * 100

    def _sell(self, sh: dict, frac: float, price: float, now: float, why: str) -> None:
        pos = sh["pos"]
        sold = pos.tokens * min(max(frac, 0.0), 1.0)
        got = sold * price * (1 - self.fee / 100) - sh["tx"]
        sh["cash"].append({"t": round(now, 3), "frac": round(frac, 6), "price": price, "tokens": sold,
                           "sol": got, "status": "filled", "why": why})
        pos.tokens -= sold
        if frac < 1:
            pos.initials_taken = True

    @staticmethod
    def _fails(sh: dict) -> bool:
        """A late-landing variant's sell attempt fails LAB_SELL_FAIL_PCT of the time: deterministic per shadow and
        attempt, so a replay of the same prices gives the same rows."""
        pct = sh["over"].get("_fail", 0)
        if not pct:
            return False
        h = hashlib.sha256(f"{sh['pos'].mint}|{sh['variant']}|{sh['opened']}|{sh['tries']}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2 ** 64 < pct / 100

    def tick(self, tokens: dict, now: float, p) -> None:
        self._follow_after(tokens, now)
        for mint, shadows in list(self.open.items()):
            s = tokens.get(mint)
            for sh in shadows:
                if sh.get("closed"):
                    continue
                if s is None or not s.price_known:
                    if now - sh["opened"] >= MAX_OPEN_S:
                        self._close(sh, None, now, "token gone")
                    continue
                pos, price = sh["pos"], s.curve.price
                if sh.get("land_at") and now < sh["land_at"]:
                    continue                             # a late fill: the buy hasn't landed yet
                if sh.get("land_at") and sh["over"].get("_delay"):
                    sh["land_at"] = 0                    # it lands now, at this price (a raw mark: costs paid once)
                    e = sh["entry"]
                    if e["price_kind"] == "raw_mark":
                        f = self.fee / 100
                        stake = e["cash_sol"] - sh["tx"] * e["cash_sol"]
                        e["tokens"] = stake * (1 - f) / (price * e["cash_sol"])
                        e["landed_price"] = price
                    pos.tokens = pos.initial_tokens = e["tokens"]
                    pos.entry_price = pos.peak_price = 1 / e["tokens"]
                pend = sh["pending"]
                if pend is not None:                     # a late sell: sent, it lands now (or fails and is retried)
                    if now < pend["at"]:
                        continue
                    sh["tries"] += 1
                    if self._fails(sh):
                        sh["cash"].append({"t": round(now, 3), "frac": pend["frac"], "price": price, "tokens": 0.0,
                                           "sol": -sh["tx"], "status": "failed", "why": pend["why"]})
                        if sh["tries"] >= MAX_SELL_TRIES:
                            self._close(sh, None, now, "exit never filled")
                        else:
                            pend["at"] = now + sh["over"]["_delay"]
                        continue
                    sh["pending"] = None
                    r = (pend["frac"], pend["why"])
                else:
                    r = (1.0, "30 min limit") if now - sh["opened"] >= MAX_OPEN_S else self._rule(sh, s, now, p)
                    if r and sh["over"].get("_delay"):  # full or partial: it lands later, its fraction kept
                        sh["pending"] = {"at": now + sh["over"]["_delay"], "frac": r[0], "why": r[1],
                                         "decided": round(now, 3)}
                        continue
                if not r:
                    continue
                frac, why = r
                self._sell(sh, frac, price, now, why)
                if frac >= 1 or pos.tokens * price < 1e-6:
                    self._close(sh, price, now, why)
            if all(sh.get("closed") for sh in shadows):
                del self.open[mint]

    def _close(self, sh: dict, price: float | None, now: float, why: str) -> None:
        pos = sh["pos"]
        if price and pos.tokens > 0:
            self._sell(sh, 1.0, price, now, why)
        net = sum(x["sol"] for x in sh["cash"]) - 1.0
        e = sh["entry"]
        row = {"closed": now, "opened": sh["opened"], "mint": pos.mint, "symbol": sh["symbol"], "kind": sh["kind"],
               "variant": sh["variant"], "pnl_pct": round(net * 100, 2), "why": why,
               "held_s": round(now - sh["opened"]), "accounting": ACCOUNTING, "net": round(net, 9),
               "entry": {"price_kind": e["price_kind"], "price": e["price"], "cash_sol": e["cash_sol"],
                         "tokens_per_cash": e["tokens"], "tx_per_cash": round(sh["tx"], 9),
                         **({"landed_price": e["landed_price"]} if "landed_price" in e else {})},
               "cash": [{**x, "sol": round(x["sol"], 9), "tokens": round(x["tokens"], 12)} for x in sh["cash"]],
               **({"delay_s": sh["over"]["_delay"], "sell_tries": sh["tries"]} if sh["over"].get("_delay") else {}),
               **sh.get("extra", {})}
        sh["closed"] = True
        self.done.append(row)
        if price and why not in ("30 min limit",) and now - sh["opened"] < MAX_OPEN_S:   # follow what it gave up
            self.after.append({"mint": pos.mint, "variant": sh["variant"], "kind": sh["kind"], "opened": sh["opened"],
                               "closed": now, "exit_price": price, "peak": price, "low": price, "last": price})
        if self.on_close is not None:
            self.on_close(row)
        self._write(row)

    def _follow_after(self, tokens: dict, now: float) -> None:
        """After an early exit, the coin's price to the 30-minute mark, relative to the exit price."""
        keep = []
        for g in self.after:
            s = tokens.get(g["mint"])
            if s is not None and s.price_known:
                px = s.curve.price
                g["peak"], g["low"], g["last"] = max(g["peak"], px), min(g["low"], px), px
            if now - g["opened"] < MAX_OPEN_S and s is not None:
                keep.append(g)
                continue
            x = g["exit_price"]
            self._write({"record": "after_exit", "mint": g["mint"], "variant": g["variant"], "kind": g["kind"],
                         "opened": g["opened"], "closed": g["closed"], "until": now, "followed_to_end": s is not None,
                         "peak_after_pct": round((g["peak"] / x - 1) * 100, 2),
                         "low_after_pct": round((g["low"] / x - 1) * 100, 2),
                         "end_after_pct": round((g["last"] / x - 1) * 100, 2)})
        self.after = keep

    def _write(self, row: dict) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")

    def view(self, kind: str = "late") -> dict:
        """Per variant: entries, mean and median P&L % per entry, win rate; sorted by mean, best first."""
        rows = [r for r in self.done if r.get("accounting") == ACCOUNTING and (
            r["kind"] == kind or (kind == "sniper" and r["kind"] not in LATE_KINDS + SNIPER_CALLS + (PICKS,)))]
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

    def picks_view(self) -> dict:
        """The trees' picks: per exit rule and P(2x) band, mean / median P&L per pick after fees and how many won."""
        rows = [r for r in self.done if r["kind"] == PICKS and r.get("accounting") == ACCOUNTING]
        bands = {}
        for name, lo, hi in PICK_BANDS:
            by: dict[str, list[float]] = {}
            for r in rows:
                if lo <= r.get("p", 0) < hi:
                    by.setdefault(r["variant"], []).append(r["pnl_pct"])
            bands[name] = [{"variant": v, "n": len(xs), "mean_pct": statistics.fmean(xs), "median_pct": statistics.median(xs),
                            "win_rate": sum(1 for x in xs if x > 0) / len(xs)} for v, xs in by.items()]
        return {"kind": PICKS, "picks": len({(r["mint"], r["opened"]) for r in rows}),
                "open": len({m for m, v in self.open.items() for sh in v if sh["kind"] == PICKS and not sh.get("closed")}),
                "since": min((r["opened"] for r in rows), default=None), "bands": bands,
                "exits": list(PICK_EXITS)}
