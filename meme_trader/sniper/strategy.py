""""Confirm-then-ride" early-entry strategy for pump.fun launches.

ENTRY - don't race insiders to block 0. Watch each launch for a short window, let
bundlers/dev show their hand, and only buy launches that look organic and accelerating
while the bonding curve is still early:
  1. hard gates (any fail = reject forever)
  2. a 0-100 score from buyer velocity, net SOL inflow, buy/sell balance, distribution,
     socials and caller signals

EXIT - the classic "take initials, ride the rest, leave before it rolls over":
  1. initials: at +initials_at_pct sell enough to get the cost back
  2. ride the remainder with a trailing stop that tightens as the gain grows
  3. leave early on red flags: dev sells, early buyers dumping, sell pressure
     taking over, a stalled top, approaching graduation (migration dump)
  4. hard stop loss and max hold time
"""
from __future__ import annotations

from dataclasses import dataclass

from .tracker import TokenState


@dataclass
class EntryDecision:
    action: str            # wait | enter | reject
    score: float = 0.0
    notes: list[str] | None = None


def evaluate_entry(s: TokenState, now: float, p, ctx: dict) -> EntryDecision:
    """p: params.sniper.entry. ctx: {'creator_launches': int, 'symbol_dupes': int, 'social_weight': float}"""
    age = s.age(now)
    prog = s.curve.progress * 100
    fails: list[str] = []

    # --- hard gates that can fire at any time --------------------------------
    if s.dev_sold > 0:
        fails.append("dev sold")
    if s.dev_initial_pct() > p.max_dev_buy_pct:
        fails.append(f"dev bought {s.dev_initial_pct():.1f}% > {p.max_dev_buy_pct}%")
    if s.bundle_pct() > p.max_bundle_pct:
        fails.append(f"bundle {s.bundle_pct():.1f}% > {p.max_bundle_pct}%")
    if s.early_sold_ratio() > p.max_early_sold_ratio:
        fails.append(f"early buyers dumped {s.early_sold_ratio():.0%}")
    if ctx.get("creator_launches", 0) > p.max_creator_launches_24h:
        fails.append(f"serial deployer ({ctx['creator_launches']} launches/24h)")
    if ctx.get("symbol_dupes", 0) > p.max_symbol_dupes_1h:
        fails.append(f"copycat ticker ({ctx['symbol_dupes']} same symbol/1h)")
    if prog > p.max_curve_progress_pct:
        fails.append(f"curve {prog:.0f}% > {p.max_curve_progress_pct}% (too late)")
    if fails:
        return EntryDecision("reject", 0.0, fails)

    if age > p.max_age_s:
        return EntryDecision("reject", 0.0, [f"no setup within {p.max_age_s:.0f}s"])
    if age < p.min_age_s:
        return EntryDecision("wait")

    # --- conditions that must hold at the moment of entry ---------------------
    waits: list[str] = []
    if s.launch is None and not p.allow_unknown_launch:
        return EntryDecision("reject", 0.0, ["launch not seen (dev unknown)"])
    if p.require_socials and s.launch and not (s.launch.twitter or s.launch.telegram or s.launch.website):
        return EntryDecision("reject", 0.0, ["no social links"])
    if prog < p.min_curve_progress_pct:
        waits.append(f"curve {prog:.1f}% < {p.min_curve_progress_pct}%")
    if len(s.buyers) < p.min_unique_buyers:
        waits.append(f"buyers {len(s.buyers)} < {p.min_unique_buyers}")
    top = s.top_holders_pct(10)
    if top > p.max_top10_pct:
        waits.append(f"top10 {top:.0f}% > {p.max_top10_pct}%")
    flow = s.net_flow_sol(now, p.flow_window_s)
    if flow < p.min_net_flow_sol:
        waits.append(f"net flow {flow:.2f} < {p.min_net_flow_sol} SOL/{p.flow_window_s:.0f}s")

    # --- score -----------------------------------------------------------------
    w = s.window(now, p.flow_window_s)
    nb = sum(1 for t in w if t[2] == "buy")
    ns = sum(1 for t in w if t[2] == "sell")
    ratio = nb / max(ns, 1)
    velocity = s.buyers_in(now, p.flow_window_s)
    near_high = s.curve.price / s.peak_price if s.peak_price else 0

    def cap(x: float, full: float) -> float:
        return max(0.0, min(x / full, 1.0)) if full > 0 else 0.0

    has_socials = bool(s.launch and (s.launch.twitter or s.launch.telegram or s.launch.website))
    score = 100 * (
        0.25 * cap(velocity, p.full_marks_buyers)
        + 0.25 * cap(flow, p.full_marks_flow_sol)
        + 0.15 * cap(ratio - 1, 2.0)
        + 0.15 * cap(p.max_top10_pct - top, p.max_top10_pct)
        + 0.10 * cap(near_high - 0.7, 0.3)
        + 0.05 * has_socials
        + 0.05 * cap(ctx.get("social_weight", 0.0), 1.0)
    )
    score = round(score, 1)
    notes = [f"vel {velocity}", f"flow {flow:.2f}", f"b/s {ratio:.1f}", f"top10 {top:.0f}%", f"curve {prog:.0f}%"]
    if waits or score < p.min_score:
        return EntryDecision("wait", score, waits or [f"score {score} < {p.min_score}"])
    return EntryDecision("enter", score, notes)


@dataclass
class SniperPosition:
    mint: str
    symbol: str
    opened_at: float
    entry_price: float
    tokens: float
    initial_tokens: float
    cost_sol: float            # remaining cost basis
    initial_cost_sol: float
    score: float
    peak_price: float = 0.0
    initials_taken: bool = False
    proceeds_sol: float = 0.0
    exits: list | None = None  # [(ts, reason, tokens, sol)]
    source: str = "sniper"     # sniper | copy:<leader label>
    leader: str = ""           # leader wallet for copy positions
    desk: str = ""             # AI desk verdict summary at entry

    def gain_pct(self, price: float) -> float:
        return (price / self.entry_price - 1) * 100


def trail_pct_for(gain_pct: float, tiers) -> float:
    """tiers: [{above_gain_pct, trail_pct}] sorted ascending; returns the tightest applicable."""
    trail = None
    for t in tiers:
        if gain_pct >= t["above_gain_pct"]:
            trail = t["trail_pct"]
    return trail


def evaluate_exit(pos: SniperPosition, s: TokenState, now: float, x, fee_pct: float):
    """x: params.sniper.exit. Returns (fraction_of_remaining 0..1, reason) or None."""
    price = s.curve.price
    pos.peak_price = max(pos.peak_price, price)
    gain = pos.gain_pct(price)
    peak_gain = pos.gain_pct(pos.peak_price)
    drop = (1 - price / pos.peak_price) * 100 if pos.peak_price else 0.0
    held = now - pos.opened_at

    # red flags: get out entirely
    if s.dev_sold > 0 and x.exit_on_dev_sell:
        return 1.0, "dev sold"
    if gain <= -x.stop_loss_pct:
        return 1.0, f"stop loss {gain:.0f}%"
    if s.migrated or s.curve.progress * 100 >= x.exit_at_curve_progress_pct:
        return 1.0, f"pre-graduation exit (curve {s.curve.progress:.0%})"
    if held >= x.max_hold_s:
        return 1.0, "max hold time"

    # initials: recover cost at the first target
    if not pos.initials_taken and gain >= x.initials_at_pct:
        frac = min(1.0, (1 + fee_pct / 100) / (1 + gain / 100) * x.initials_multiple)
        return frac, f"initials out at +{gain:.0f}%"

    # trailing stop, tighter as the run grows (only once we've been up a bit)
    trail = trail_pct_for(peak_gain, x.trail_tiers)
    if trail is not None and drop >= trail:
        return 1.0, f"trailing stop -{drop:.0f}% from peak (+{peak_gain:.0f}%)"

    # momentum decay: selling dominates and we're off the high
    flow = s.net_flow_sol(now, x.decay_window_s)
    if drop >= x.decay_min_drop_pct and flow <= -x.decay_net_outflow_sol:
        return 1.0, f"momentum decay (outflow {flow:.2f} SOL, -{drop:.0f}%)"

    # stalled: in profit but no new high for a while
    if gain > 0 and now - s.last_high_ts() >= x.stall_s and held >= x.stall_s:
        return 1.0, f"stalled {x.stall_s:.0f}s without new high"

    # dead entry: never got going
    if held >= x.no_progress_s and peak_gain < x.no_progress_min_gain_pct:
        return 1.0, "no follow-through"
    return None
