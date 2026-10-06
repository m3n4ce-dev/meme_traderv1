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

from .features import social_strength
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
    fees = s.fees_paid_sol()
    if fees < p.min_fees_paid_sol:
        waits.append(f"fees paid {fees:.2f} < {p.min_fees_paid_sol} SOL")
    if s.sniper_pct() > p.max_sniper_pct:
        waits.append(f"snipers hold {s.sniper_pct():.0f}% > {p.max_sniper_pct}%")
    if s.insider_pct() > p.max_insider_pct:
        waits.append(f"insiders hold {s.insider_pct():.0f}% > {p.max_insider_pct}%")
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

    # socials: a 833k-launch survival study found Telegram alone lifts graduation 8.9x and all three
    # links 17.4x (arXiv 2607.02823), so links count by channel, Telegram most
    score = 100 * (
        0.225 * cap(velocity, p.full_marks_buyers)
        + 0.225 * cap(flow, p.full_marks_flow_sol)
        + 0.15 * cap(ratio - 1, 2.0)
        + 0.15 * cap(p.max_top10_pct - top, p.max_top10_pct)
        + 0.10 * cap(near_high - 0.7, 0.3)
        + 0.10 * social_strength(s)
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
    ladder_hit: int = 0        # ladder exit profile: steps already sold
    p: float | None = None     # model P(2x first) at entry, for live calibration in analytics
    trough_price: float = 0.0  # lowest price while held (max adverse excursion)
    rent_sol: float = 0.0      # live: refundable token-account rent paid at entry (not part of cost)
    # execution, measured per trade: price when we decided vs what we got, decision-to-fill time, fees burned
    # by attempts that failed. Paper with execution.paper_delay_s models them; live measures them.
    entry_quote: float = 0.0
    entry_delay_s: float = 0.0
    exit_quote: float = 0.0
    exit_fill: float = 0.0
    exit_delay_s: float = 0.0
    failed_fees_sol: float = 0.0
    manual: dict | None = None   # manual positions: {"sl", "tp", "tp_frac", "trail", "tp_done"} (0 = off)
    adds: list | None = None     # buys added to the position later: [(ts, sol, tokens, price)]
    bot: str = ""                # your position handed to the bots ("ride"; older saves say "late"/"sniper")
    handed_price: float = 0.0    # the price when you handed it over, and its peak since
    handed_peak: float = 0.0
    ride_tp: bool = False        # the bots already took their partial profit
    feat: dict | None = None     # what the bot saw when it decided to buy (kept with the trade for research)
    dev_sold_at_entry: float = 0.0   # creator tokens already sold when the bot decided to buy ("dev sold" = more)

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
    if gain <= -x.stop_loss_pct and x.profile != "ladder":     # the ladder profile has its own stop (stop_x)
        return 1.0, f"stop loss {gain:.0f}%"
    if s.migrated or s.curve.progress * 100 >= x.exit_at_curve_progress_pct:
        return 1.0, f"pre-graduation exit (curve {s.curve.progress:.0%})"
    if held >= x.max_hold_s:
        return 1.0, "max hold time"
    if x.profile == "ladder":
        return _ladder_exit(pos, price, x.ladder, fee_pct)

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


def _ladder_exit(pos: SniperPosition, price: float, L, fee_pct: float):
    """Fixed take-profit ladder (e.g. 2x sell half, 5x sell rest, 0.3x stop) with a stop that ratchets up
    after trail_after_x so a big winner can't round-trip back to entry."""
    mult = price / pos.entry_price
    peak_mult = pos.peak_price / pos.entry_price
    if mult <= L.stop_x:
        return 1.0, f"ladder stop at {mult:.2f}x"
    if peak_mult >= L.trail_after_x:
        floor = max(pos.entry_price * (1 + fee_pct / 100), pos.peak_price * (1 - L.trail_pct / 100))
        if price <= floor:
            return 1.0, f"ladder trail stop at {mult:.1f}x (peak {peak_mult:.1f}x)"
    steps = L.steps
    if pos.ladder_hit < len(steps) and mult >= steps[pos.ladder_hit]["at_x"]:
        step = steps[pos.ladder_hit]
        frac = 1.0 if step["sell_frac"] >= 1 else min(1.0, pos.initial_tokens * step["sell_frac"] / pos.tokens)
        return frac, f"ladder {step['at_x']}x sell {'rest' if frac >= 1 else f'{frac:.0%}'}"
    return None


def gate_checklist(s: TokenState, now: float, p, ctx: dict) -> list[dict]:
    """Every sniper gate with its live value, limit and pass/fail - for the token detail view.
    kind: hard = rejects for good, wait = must hold at entry. Mirrors evaluate_entry (tested to agree)."""
    age, prog = s.age(now), s.curve.progress * 100
    flow = s.net_flow_sol(now, p.flow_window_s)
    rows = [
        ("dev hasn't sold", "hard", "sold" if s.dev_sold else "holding", "", not s.dev_sold),
        ("dev initial buy", "hard", f"{s.dev_initial_pct():.1f}%", f"<= {p.max_dev_buy_pct}%", s.dev_initial_pct() <= p.max_dev_buy_pct),
        ("bundled supply", "hard", f"{s.bundle_pct():.1f}%", f"<= {p.max_bundle_pct}%", s.bundle_pct() <= p.max_bundle_pct),
        ("early buyers dumped", "hard", f"{s.early_sold_ratio():.0%}", f"<= {p.max_early_sold_ratio:.0%}", s.early_sold_ratio() <= p.max_early_sold_ratio),
        ("creator launches / 24h", "hard", str(ctx.get("creator_launches", 0)), f"<= {p.max_creator_launches_24h}", ctx.get("creator_launches", 0) <= p.max_creator_launches_24h),
        ("same ticker / 1h", "hard", str(ctx.get("symbol_dupes", 0)), f"<= {p.max_symbol_dupes_1h}", ctx.get("symbol_dupes", 0) <= p.max_symbol_dupes_1h),
        ("curve not too late", "hard", f"{prog:.0f}%", f"<= {p.max_curve_progress_pct}%", prog <= p.max_curve_progress_pct),
        ("age window", "wait", f"{age:.0f}s", f"{p.min_age_s}-{p.max_age_s}s", p.min_age_s <= age <= p.max_age_s),
        ("curve started", "wait", f"{prog:.1f}%", f">= {p.min_curve_progress_pct}%", prog >= p.min_curve_progress_pct),
        ("unique buyers", "wait", str(len(s.buyers)), f">= {p.min_unique_buyers}", len(s.buyers) >= p.min_unique_buyers),
        ("fees paid", "wait", f"{s.fees_paid_sol():.2f} SOL", f">= {p.min_fees_paid_sol}", s.fees_paid_sol() >= p.min_fees_paid_sol),
        ("snipers still hold", "wait", f"{s.sniper_pct():.0f}%", f"<= {p.max_sniper_pct}%", s.sniper_pct() <= p.max_sniper_pct),
        ("insiders still hold", "wait", f"{s.insider_pct():.0f}%", f"<= {p.max_insider_pct}%", s.insider_pct() <= p.max_insider_pct),
        ("top 10 holders", "wait", f"{s.top_holders_pct(10):.0f}%", f"<= {p.max_top10_pct}%", s.top_holders_pct(10) <= p.max_top10_pct),
        (f"net inflow {p.flow_window_s:.0f}s", "wait", f"{flow:.2f} SOL", f">= {p.min_net_flow_sol}", flow >= p.min_net_flow_sol),
    ]
    if s.cluster:
        rows.append(("insider cluster (funding)", "hard", f"{s.cluster['pct']:.0f}%", f"<= {p.funding.max_cluster_pct}%",
                     s.cluster["pct"] <= p.funding.max_cluster_pct))
    return [{"gate": g, "kind": k, "value": v, "limit": lim, "ok": bool(ok)} for g, k, v, lim, ok in rows]


# --------------------------------------------------------------------------- graduation play
SKIP_FOR_GOOD = ("too young", "one-sided buying")   # graduation rejections that end the coin's chance


def late_dev_ok(s: TokenState, L) -> bool:
    """late.max_dev_sold_pct: a graduation play skips coins whose creator has sold more than this % of the supply
    (0 = any sale, the default). Many coins keep filling their curve after the creator leaves: on 2026-10-06 four of
    the ten coins on the graduation watch were passed for it while their charts climbed."""
    lim = L.get("max_dev_sold_pct") or 0
    return s.dev_sold / s.supply * 100 <= lim if lim else not s.dev_sold


def evaluate_late_entry(s: TokenState, now: float, L, red: dict) -> tuple[bool, str]:
    """Late-curve momentum ("graduation play"): a token already filling its curve fast, bought for the
    final leg and sold before migration. L: params.sniper.late. red: red-flag limits + context."""
    prog = s.curve.progress * 100
    if s.migrated or not (L.min_curve_pct <= prog <= L.max_curve_pct) or s.age(now) > L.max_age_s:
        return False, "window"
    if not late_dev_ok(s, L) or s.bundle_pct() > red["max_bundle_pct"] or s.early_sold_ratio() > red["max_early_sold_ratio"]:
        return False, "red flag"
    if red["creator_launches"] > red["max_creator_launches_24h"]:
        return False, "serial deployer"
    if s.cluster and s.cluster["pct"] > red["max_cluster_pct"]:
        return False, "insider cluster"
    if L.get("entry_mode", "rule") == "window":          # research baseline: no momentum condition at all
        return True, f"window baseline: curve {prog:.0f}%"
    w = s.window(now, L.flow_window_s)
    nb = sum(1 for t in w if t[2] == "buy")
    ns = sum(1 for t in w if t[2] == "sell")
    flow = sum(t[3] if t[2] == "buy" else -t[3] for t in w)
    buyers = len({t[4] for t in w if t[2] == "buy"})
    near = s.curve.price / s.peak_price if s.peak_price else 0
    if flow < L.min_net_flow_sol or buyers < L.min_buyers or nb / max(ns, 1) < L.min_buy_sell_ratio \
            or near < L.min_near_high:
        return False, "momentum"
    # The dump profile (2026-10-05 replays): checked only once the coin qualifies, and a coin that matches it is
    # passed over for good (SKIP_FOR_GOOD). Waiting instead for a first sell or an older coin tested worse.
    if s.age(now) < (L.get("min_age_s") or 0):
        return False, "too young"
    need = L.get("min_recent_sells") or 0
    if need and sum(1 for t in s.window(now, 20) if t[2] == "sell") < need:
        return False, "one-sided buying"                   # big inflow, nobody selling
    return True, f"late play: curve {prog:.0f}%, +{flow:.1f} SOL/{L.flow_window_s}s, {buyers} buyers"


def evaluate_late_exit(pos: SniperPosition, s: TokenState, now: float, L, x):
    """Exit for graduation plays: out before migration, tight stop, fast stall/decay exits."""
    price = s.curve.price
    pos.peak_price = max(pos.peak_price, price)
    gain = pos.gain_pct(price)
    drop = (1 - price / pos.peak_price) * 100 if pos.peak_price else 0.0
    held = now - pos.opened_at
    if s.dev_sold > pos.dev_sold_at_entry:              # the creator sold (again) while we held
        return 1.0, "dev sold"
    if s.migrated or s.curve.progress * 100 >= L.exit_curve_pct:
        return 1.0, f"graduation exit (curve {s.curve.progress:.0%})"
    if gain <= -L.stop_loss_pct:
        return 1.0, f"late stop {gain:.0f}%"
    if held >= L.max_hold_s:
        return 1.0, "late max hold"
    if runner_armed(pos, L):                          # a big run: only a trailing stop (and the exits above) sells it
        if drop >= L.runner_trail_pct:
            return 1.0, f"late trail -{drop:.0f}% from peak (+{pos.gain_pct(pos.peak_price):.0f}%)"
        return None
    flow = s.net_flow_sol(now, x.decay_window_s)
    if drop >= x.decay_min_drop_pct and flow <= -x.decay_net_outflow_sol:
        return 1.0, f"momentum decay (outflow {flow:.2f} SOL, -{drop:.0f}%)"
    if now - s.last_high_ts() >= L.stall_s and held >= L.stall_s:
        return 1.0, f"late stall {L.stall_s:.0f}s"
    return None


def runner_armed(pos: SniperPosition, L) -> bool:
    """late.runner_after_pct (0 = off): once a play's peak is this far up, momentum decay and the stall stop selling
    it. In the 2026-10-03..05 replays the 9 plays that reached the graduation exit made +2.26 SOL (median +167%) while
    the 21 that peaked at +50..100% were sold by momentum decay at a median +44%."""
    after = L.get("runner_after_pct") or 0
    return bool(after) and pos.gain_pct(pos.peak_price) >= after


# --------------------------------------------------------------------------- manual positions
def evaluate_manual_exit(pos: SniperPosition, s: TokenState, m: dict):
    """Your own position: no strategy exits, only what you set (stop, take profit, trail), plus an exit when
    the coin leaves the bonding curve (the bot can't price or sell it after). (fraction, reason) or None."""
    price = s.curve.price
    pos.peak_price = max(pos.peak_price, price)
    gain = pos.gain_pct(price)
    if s.migrated and m.get("sell_on_graduation", True):
        return 1.0, "manual: graduated (live sells at graduation)"
    if m.get("sl") and gain <= -m["sl"]:
        return 1.0, f"manual stop {gain:.0f}%"
    if m.get("tp") and not m.get("tp_done") and gain >= m["tp"]:
        m["tp_done"] = True
        return min(max(float(m.get("tp_frac") or 0.5), 0.05), 1.0), f"manual take profit {gain:+.0f}%"
    if m.get("trail") and gain > 0 and pos.peak_price and price <= pos.peak_price * (1 - m["trail"] / 100):
        return 1.0, f"manual trail -{m['trail']:.0f}% off peak"
    return None


def evaluate_ride_exit(pos: SniperPosition, s: TokenState, r: dict, live: bool = False):
    """A position you handed to the bots: ride it for a runner. Part out at take_x times the entry (half, at 2x
    by default), the rest trails trail_pct off its peak once it has run trail_arm_pct past the hand-over price
    (or after the partial), and a stop stop_pct below the lower of the entry and the hand-over price. No time,
    stall, momentum or dev-sold exits: those suit the bots' own quick scalps, and on a coin you'd held for a
    while they fired at once. (fraction, reason) or None."""
    price = s.curve.price
    pos.peak_price = max(pos.peak_price, price)
    handed = pos.handed_price or pos.entry_price
    pos.handed_peak = max(pos.handed_peak or handed, price)
    if s.migrated and live:
        return 1.0, "bots: graduated (live sells at graduation)"
    if price <= min(pos.entry_price, handed) * (1 - r["stop_pct"] / 100):
        return 1.0, f"bots: stop {pos.gain_pct(price):.0f}%"
    if not pos.ride_tp and r.get("take_x") and price >= pos.entry_price * r["take_x"]:
        pos.ride_tp = True
        frac = min(max(float(r.get("take_frac") or 0.5), 0.05), 1.0)
        return frac, f"bots: {frac:.0%} out at {r['take_x']:g}x"
    armed = pos.ride_tp or pos.handed_peak >= handed * (1 + r["trail_arm_pct"] / 100)
    if armed and price <= pos.handed_peak * (1 - r["trail_pct"] / 100):
        return 1.0, f"bots: trail -{r['trail_pct']:g}% off its peak"
    return None


def initials_fraction(pos: SniperPosition, price: float, fee_pct: float) -> float | None:
    """The share of the bag to sell to get back what's still owed of the initial cost (net of fees).
    None if it would take the whole bag (or more): then it isn't 'initials', it's an exit."""
    owed = pos.initial_cost_sol - pos.proceeds_sol
    value = pos.tokens * price * (1 - fee_pct / 100)
    if owed <= 0 or value <= 0 or owed >= value:
        return None
    return owed / value


# --------------------------------------------------------------------------- what the bots are thinking
def late_checklist(s: TokenState, now: float, L, red: dict) -> dict:
    """evaluate_late_entry, step by step, for the Desk: every check with its live value and limit, and the
    verdict. Mirrors evaluate_late_entry (tested to agree): all checks ok <=> it would buy."""
    prog = s.curve.progress * 100
    age = s.age(now)
    checks = [
        ("window", "curve in window", f"{prog:.0f}%", f"{L.min_curve_pct}-{L.max_curve_pct}%",
         not s.migrated and L.min_curve_pct <= prog <= L.max_curve_pct, prog / max(L.min_curve_pct, 1e-9)),
        ("window", "young enough", f"{age / 60:.1f} min", f"<= {L.max_age_s / 60:.0f} min", age <= L.max_age_s,
         None),
        ("flag", "dev holding", f"sold {s.dev_sold / s.supply * 100:.1f}%" if s.dev_sold else "holding",
         f"<= {L.max_dev_sold_pct:g}% sold" if L.get("max_dev_sold_pct") else "", late_dev_ok(s, L), None),
        ("flag", "bundled supply", f"{s.bundle_pct():.0f}%", f"<= {red['max_bundle_pct']}%",
         s.bundle_pct() <= red["max_bundle_pct"], None),
        ("flag", "early buyers dumped", f"{s.early_sold_ratio():.0%}", f"<= {red['max_early_sold_ratio']:.0%}",
         s.early_sold_ratio() <= red["max_early_sold_ratio"], None),
        ("flag", "creator launches / 24h", str(red["creator_launches"]), f"<= {red['max_creator_launches_24h']}",
         red["creator_launches"] <= red["max_creator_launches_24h"], None),
    ]
    if s.cluster:
        checks.append(("flag", "insider cluster", f"{s.cluster['pct']:.0f}%", f"<= {red['max_cluster_pct']}%",
                       s.cluster["pct"] <= red["max_cluster_pct"], None))
    if L.get("entry_mode", "rule") != "window":
        w = s.window(now, L.flow_window_s)
        nb = sum(1 for t in w if t[2] == "buy")
        ns = sum(1 for t in w if t[2] == "sell")
        flow = sum(t[3] if t[2] == "buy" else -t[3] for t in w)
        buyers = len({t[4] for t in w if t[2] == "buy"})
        near = s.curve.price / s.peak_price if s.peak_price else 0
        ratio = nb / max(ns, 1)
        checks += [
            ("momentum", f"net inflow {L.flow_window_s:.0f}s", f"{flow:+.1f} SOL", f">= {L.min_net_flow_sol}",
             flow >= L.min_net_flow_sol, flow / L.min_net_flow_sol if L.min_net_flow_sol else 1.0),
            ("momentum", "unique buyers", str(buyers), f">= {L.min_buyers}", buyers >= L.min_buyers,
             buyers / L.min_buyers if L.min_buyers else 1.0),
            ("momentum", "buys / sells", f"{ratio:.1f}", f">= {L.min_buy_sell_ratio}", ratio >= L.min_buy_sell_ratio,
             ratio / L.min_buy_sell_ratio if L.min_buy_sell_ratio else 1.0),
            ("momentum", "near its high", f"{near:.0%}", f">= {L.min_near_high:.0%}", near >= L.min_near_high,
             near / L.min_near_high if L.min_near_high else 1.0),
        ]
    rows = [{"group": g, "label": lab, "value": v, "limit": lim, "ok": bool(ok),
             "frac": None if fr is None else round(max(0.0, min(fr, 1.5)), 3)} for g, lab, v, lim, ok, fr in checks]
    fails = [r for r in rows if not r["ok"]]
    if not fails:
        verdict, why = "buy", "every check passes"
    elif fails[0]["group"] == "window":
        verdict = "early" if (not s.migrated and prog < L.min_curve_pct and age <= L.max_age_s) else "out"
        why = ("migrated" if s.migrated else f"too old: {age / 60:.0f} min (max {L.max_age_s / 60:.0f})"
               if age > L.max_age_s else f"curve {prog:.0f}%, the window opens at {L.min_curve_pct}%"
               if prog < L.min_curve_pct else f"curve {prog:.1f}%, past the {L.max_curve_pct}% window")
    elif any(r["group"] == "flag" for r in fails):
        f = next(r for r in fails if r["group"] == "flag")
        verdict, why = "pass", f"{f['label']}: {f['value']}"
    else:
        verdict = "wait"
        why = ", ".join(f"{r['label']} {r['value']} / {r['limit'].lstrip('>= ')}" for r in fails)
    return {"checks": rows, "verdict": verdict, "why": why, "progress": round(prog, 1),
            "readiness": round(sum(min(r["frac"], 1.0) for r in rows if r["group"] == "momentum" and r["frac"] is not None)
                               / max(1, sum(1 for r in rows if r["group"] == "momentum")), 3)}


def exit_watch(pos: SniperPosition, s: TokenState, now: float, L, x, own: dict | None = None) -> list[dict]:
    """How close each exit is, for the Desk. Graduation plays mirror evaluate_late_exit's order; your own
    positions (own = {"ride": {...}} when handed over, else your stop/take profit/trail) show only the exits
    that really apply to them; other positions show their stop, peak and time limit."""
    price = s.curve.price
    gain = pos.gain_pct(price)
    peak = max(pos.peak_price, price)
    drop = (1 - price / peak) * 100 if peak else 0.0
    held = now - pos.opened_at
    out = []
    if own is not None and "ride" in own:                 # handed to the bots: evaluate_ride_exit's rules
        r, handed = own["ride"], pos.handed_price or pos.entry_price
        floor = min(pos.entry_price, handed) * (1 - r["stop_pct"] / 100)
        out = [{"label": "stop", "value": f"{gain:+.0f}%", "limit": f"-{r['stop_pct']:g}% of the hand-over",
                "frac": (min(pos.entry_price, handed) - price) / (min(pos.entry_price, handed) - floor) if price < min(pos.entry_price, handed) else 0.0}]
        if not pos.ride_tp:
            out.append({"label": f"half out at {r['take_x']:g}x", "value": f"{price / pos.entry_price:.2f}x",
                        "limit": f"{r['take_x']:g}x", "frac": price / (pos.entry_price * r["take_x"])})
        out.append({"label": "trailing stop", "value": f"-{(1 - price / max(pos.handed_peak or price, price)) * 100:.0f}% off its peak",
                    "limit": f"-{r['trail_pct']:g}% once armed", "frac": None})
    elif own is not None:                                 # yours: only the exits you set
        if own.get("sl"):
            out.append({"label": "your stop", "value": f"{gain:+.0f}%", "limit": f"-{own['sl']:g}%", "frac": max(0.0, -gain) / own["sl"]})
        if own.get("tp") and not own.get("tp_done"):
            out.append({"label": "your take profit", "value": f"{gain:+.0f}%", "limit": f"+{own['tp']:g}%", "frac": max(0.0, gain) / own["tp"]})
        if own.get("trail"):
            out.append({"label": "your trail", "value": f"-{drop:.0f}% off peak", "limit": f"-{own['trail']:g}%",
                        "frac": drop / own["trail"] if gain > 0 else None})
    elif pos.source == "late":
        prog = s.curve.progress * 100
        since_high = now - s.last_high_ts()
        flow = s.net_flow_sol(now, x.decay_window_s)
        out = [
            {"label": "graduation exit", "value": f"curve {prog:.0f}%", "limit": f"{L.exit_curve_pct}%",
             "frac": prog / L.exit_curve_pct},
            {"label": "stop loss", "value": f"{gain:+.0f}%", "limit": f"-{L.stop_loss_pct}%",
             "frac": max(0.0, -gain) / L.stop_loss_pct},
            {"label": "stall (no new high)", "value": f"{since_high:.0f}s", "limit": f"{L.stall_s:.0f}s",
             "frac": min(since_high, held) / L.stall_s if L.stall_s else 0.0},
            {"label": "momentum decay", "value": f"{flow:+.1f} SOL, -{drop:.0f}%",
             "limit": f"-{x.decay_net_outflow_sol} SOL & -{x.decay_min_drop_pct}%",
             "frac": min(max(0.0, -flow) / x.decay_net_outflow_sol, drop / x.decay_min_drop_pct)
             if x.decay_net_outflow_sol and x.decay_min_drop_pct else 0.0},
            {"label": "time limit", "value": f"{held / 60:.1f} min", "limit": f"{L.max_hold_s / 60:.0f} min",
             "frac": held / L.max_hold_s},
        ]
        if runner_armed(pos, L):
            out[2:4] = [{"label": "runner trail", "value": f"-{drop:.0f}% off its peak", "limit": f"-{L.runner_trail_pct:g}%",
                         "frac": drop / L.runner_trail_pct}]
        elif L.get("runner_after_pct"):
            out.append({"label": "runner mode", "value": f"peak {pos.gain_pct(pos.peak_price):+.0f}%",
                        "limit": f"+{L.runner_after_pct:g}%", "frac": max(0.0, pos.gain_pct(pos.peak_price)) / L.runner_after_pct})
    else:
        out = [{"label": "drop from peak", "value": f"-{drop:.0f}%", "limit": "trailing stop", "frac": None},
               {"label": "time held", "value": f"{held / 60:.1f} min", "limit": f"{x.max_hold_s / 60:.0f} min",
                "frac": held / x.max_hold_s if x.max_hold_s else None}]
    for r in out:
        if r["frac"] is not None:
            r["frac"] = round(max(0.0, min(r["frac"], 1.0)), 3)
    return out
