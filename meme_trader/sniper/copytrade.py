"""Copy trading: follow profitable wallets' pump.fun trades in real time.

Every pump.fun trade is a public Solana transaction, so any wallet's buys/sells are visible
the moment they land - no callout needed. PumpPortal's subscribeAccountTrade streams them
(metered 0.01 SOL / 10k events, needs PUMPPORTAL_API_KEY).

Hard truths built into the design:
  * we always fill AFTER the leader (and after every faster copy bot) -> a max-chase guard
    skips trades where price already ran past the leader's fill by more than max_chase_pct
  * known KOLs get farmed: some buy, wait for copiers, and sell into them ("bait") -> every
    leader is scored on OUR copied results and auto-paused after a losing streak
  * leaders run many wallets and rotate them -> discovery (`leaders` command) finds wallets
    from recorded data instead of relying on public lists

Modes per leader:
  mirror - buy when they buy (sized by our rules), sell when they sell
  signal - their buy only boosts the sniper's score (like a trusted caller)
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .events import Trade


@dataclass
class Leader:
    address: str
    label: str = ""
    mode: str = "mirror"          # mirror | signal
    enabled: bool = True


@dataclass
class LeaderStats:
    # leader's own observed results on the curve
    trades: int = 0
    closed: int = 0
    wins: int = 0
    realized_sol: float = 0.0
    # our results copying them
    copied: int = 0
    copied_wins: int = 0
    copied_pnl: float = 0.0
    loss_streak: int = 0
    paused_reason: str = ""


@dataclass
class _Bag:
    tokens: float = 0.0
    cost: float = 0.0
    pnl: float = 0.0
    last_buy_price: float = 0.0


class LeaderBook:
    def __init__(self, leaders: list, path: Path | None = None):
        self.leaders: dict[str, Leader] = {}
        for x in leaders or []:
            d = dict(x)
            self.leaders[d["address"]] = Leader(**{k: d[k] for k in ("address", "label", "mode", "enabled") if k in d})
        self.stats: dict[str, LeaderStats] = defaultdict(LeaderStats)
        self.bags: dict[tuple[str, str], _Bag] = defaultdict(_Bag)
        self.recent_copies: dict[str, deque] = defaultdict(deque)
        self.path = path
        if path and path.exists():
            for addr, st in json.loads(path.read_text()).items():
                self.stats[addr] = LeaderStats(**st)

    def is_leader(self, wallet: str) -> bool:
        lead = self.leaders.get(wallet)
        return bool(lead and lead.enabled and not self.stats[wallet].paused_reason)

    def label(self, wallet: str) -> str:
        lead = self.leaders.get(wallet)
        return (lead.label or wallet[:6]) if lead else wallet[:6]

    def bag(self, wallet: str, mint: str) -> _Bag:
        return self.bags[(wallet, mint)]

    def on_trade(self, t: Trade) -> float:
        """Update the leader's bag. For sells returns the fraction of their bag sold (0..1)."""
        st = self.stats[t.trader]
        st.trades += 1
        b = self.bags[(t.trader, t.mint)]
        if t.side == "buy":
            b.tokens += t.tokens
            b.cost += t.sol
            b.last_buy_price = t.sol / t.tokens if t.tokens else 0.0
            return 0.0
        if b.tokens <= 0:
            return 1.0
        frac = min(t.tokens / b.tokens, 1.0)
        cost_part = b.cost * frac
        b.tokens *= 1 - frac
        b.cost -= cost_part
        b.pnl += t.sol - cost_part
        st.realized_sol += t.sol - cost_part
        if b.tokens <= 1e-6:
            st.closed += 1
            st.wins += b.pnl > 0
            del self.bags[(t.trader, t.mint)]
        return frac

    def replace(self, old: Trade, new: Trade | None) -> None:
        """A fork repair: the version of a leader's trade already counted (`old`) was wrong - the chain kept `new`, or
        none (it failed). Their bag is corrected by the difference, so later sell fractions use the real inventory."""
        b = self.bags[(old.trader, old.mint)]
        sign = 1 if old.side == "buy" else -1
        b.tokens -= sign * old.tokens
        b.cost -= old.sol if old.side == "buy" else 0.0
        if new is None:
            self.stats[old.trader].trades -= 1
        else:
            b.tokens += (1 if new.side == "buy" else -1) * new.tokens
            b.cost += new.sol if new.side == "buy" else 0.0
        b.tokens, b.cost = max(b.tokens, 0.0), max(b.cost, 0.0)

    def copies_last_hour(self, wallet: str, now: float) -> int:
        q = self.recent_copies[wallet]
        while q and q[0] < now - 3600:
            q.popleft()
        return len(q)

    def note_copy(self, wallet: str, now: float) -> None:
        self.recent_copies[wallet].append(now)

    def on_copy_closed(self, wallet: str, pnl: float, pause_after_losses: int) -> str:
        st = self.stats[wallet]
        st.copied += 1
        st.copied_pnl += pnl
        if pnl > 0:
            st.copied_wins += 1
            st.loss_streak = 0
        else:
            st.loss_streak += 1
            if st.loss_streak >= pause_after_losses:
                st.paused_reason = f"{st.loss_streak} copied losses in a row"
        self.save()
        return st.paused_reason

    def weight(self, wallet: str) -> float:
        """0..1 trust from our copied results (neutral 0.5 until 5 copies)."""
        st = self.stats[wallet]
        if st.copied < 5:
            return 0.5
        return max(0.0, min(1.0, 0.5 + st.copied_pnl / max(st.copied, 1) / 0.05 * 0.25))

    def snapshot(self) -> list[dict]:
        out = []
        for addr, lead in self.leaders.items():
            st = self.stats[addr]
            out.append({"address": addr, "label": lead.label or addr[:6], "mode": lead.mode,
                        "status": "paused: " + st.paused_reason if st.paused_reason else
                        ("on" if lead.enabled else "off"),
                        "copied": st.copied, "copied_pnl": st.copied_pnl,
                        "copied_win_rate": st.copied_wins / st.copied if st.copied else 0.0,
                        "leader_closed": st.closed, "leader_realized": st.realized_sol, "weight": self.weight(addr)})
        return out

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(exist_ok=True)
            self.path.write_text(json.dumps({a: asdict(s) for a, s in self.stats.items() if a in self.leaders}, indent=1))


# --------------------------------------------------------------------------- discovery
@dataclass
class WalletScore:
    wallet: str
    tokens: int = 0
    closed: int = 0
    wins: int = 0
    realized_sol: float = 0.0
    open_pnl_sol: float = 0.0     # open bags: current value minus their remaining cost
    entry_ages: list = field(default_factory=list)
    early_buys: int = 0      # bought inside the bundle window (insider-like)
    created: int = 0         # tokens this wallet launched

    @property
    def total_sol(self) -> float:
        return self.realized_sol + self.open_pnl_sol

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed if self.closed else 0.0


def discover(events, bundle_window_s: float = 2.0, min_tokens: int = 5) -> list[WalletScore]:
    """Rank wallets by realized + marked P&L across a recorded feed. Flags insider-like wallets.
    A bag (wallet x token) is one round trip: its win or loss is decided on the whole bag at closure,
    and an open bag counts its value minus what's left of its cost."""
    from .events import Launch

    launched: dict[str, float] = {}
    creator: dict[str, str] = {}
    last_price: dict[str, float] = {}
    bags: dict[tuple[str, str], list] = {}
    scores: dict[str, WalletScore] = {}

    def sc(w: str) -> WalletScore:
        return scores.setdefault(w, WalletScore(w))

    for e in events:
        if isinstance(e, Launch):
            launched[e.mint] = e.ts
            creator[e.mint] = e.creator
            sc(e.creator).created += 1
            continue
        if not isinstance(e, Trade) or e.pool != "pump" or not e.tokens:
            continue
        last_price[e.mint] = e.v_sol / e.v_tokens if e.v_tokens else last_price.get(e.mint, 0)
        if e.trader == creator.get(e.mint):
            continue
        key = (e.trader, e.mint)
        s = sc(e.trader)
        if e.side == "buy":
            if key not in bags:
                s.tokens += 1
                age = e.ts - launched.get(e.mint, e.ts)
                s.entry_ages.append(age)
                if e.mint in launched and age <= bundle_window_s:
                    s.early_buys += 1
                bags[key] = [0.0, 0.0, 0.0]               # tokens, remaining cost, realized so far
            bags[key][0] += e.tokens
            bags[key][1] += e.sol
        elif key in bags:
            tok, cost, realized = bags[key]
            frac = min(e.tokens / tok, 1.0) if tok else 1.0
            pnl = e.sol - cost * frac
            s.realized_sol += pnl
            bags[key] = [tok - tok * frac, cost * (1 - frac), realized + pnl]
            if bags[key][0] <= 1e-6:
                s.closed += 1
                s.wins += bags[key][2] > 0
                del bags[key]
    for (w, m), (tok, cost, _) in bags.items():
        scores[w].open_pnl_sol += tok * last_price.get(m, 0.0) - cost
    ranked = [s for s in scores.values() if s.tokens >= min_tokens]
    ranked.sort(key=lambda s: -s.total_sol)
    return ranked
