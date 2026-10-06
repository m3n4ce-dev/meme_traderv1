"""Per-token live state built from the trade stream: holders, early buyers, flow, dev activity."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar

from .curve import FINAL_V_TOKENS, TOTAL_SUPPLY, Curve
from .events import Launch, Social, Trade

# pump.fun's Mayhem-mode agent: a Mayhem token mints 2B, half of it to this wallet, which then trades it
MAYHEM_AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
MAYHEM_SUPPLY = 2 * TOTAL_SUPPLY


@dataclass
class TokenState:
    # wallets whose trades move the price but aren't demand (set from sniper.market.non_organic_wallets)
    NON_ORGANIC: ClassVar[frozenset] = frozenset({MAYHEM_AGENT})
    mint: str
    launch: Launch | None
    first_seen: float
    curve: Curve = field(default_factory=Curve)
    holders: dict[str, float] = field(default_factory=dict)
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    early_bought: dict[str, float] = field(default_factory=dict)   # wallet -> tokens bought in the bundle window
    early_sold: float = 0.0
    snipers: set[str] = field(default_factory=set)   # non-dev wallets that bought in the sniper window
    volume_sol: float = 0.0
    dev_sold: float = 0.0
    buys: int = 0
    sells: int = 0
    trades: deque = field(default_factory=lambda: deque(maxlen=2000))   # (ts, price, side, sol, trader)
    peak_price: float = 0.0
    last_trade_ts: float = 0.0
    migrated: bool = False
    socials: list[Social] = field(default_factory=list)
    decided: str = ""          # "" while still being evaluated; otherwise "entered" / "rejected: ..."
    score: float = 0.0
    score_notes: list[str] = field(default_factory=list)
    desk: str = ""             # last AI desk verdict
    cluster: dict | None = None   # funding-graph insider report (see funding.py)
    last_cluster_check: float = 0.0
    unpriced_calls: list = field(default_factory=list)   # (caller, ts) for calls seen before any price
    p: float | None = None        # model probability of 2x-before-stop (when a model is loaded)
    p_ts: float = -1e12
    late_tried: bool = False      # graduation play already attempted
    price_known: bool = False     # False until a launch/trade gave us real reserves (e.g. right after a restart)
    mayhem: bool = False          # Mayhem mode (2B supply, the agent trades it): seen when the agent trades it
    non_organic_trades: int = 0
    curve_slot: int = 0           # the slot of the newest reserves applied to `curve`
    slot_moves: list = field(default_factory=list)   # that slot's trades: (tokens before, tokens after, sol after)

    @property
    def created_ts(self) -> float:
        return self.launch.ts if self.launch else self.first_seen

    @property
    def creator(self) -> str:
        return self.launch.creator if self.launch else ""

    @property
    def symbol(self) -> str:
        return self.launch.symbol if self.launch else self.mint[:6]

    def age(self, now: float) -> float:
        return now - self.created_ts

    @property
    def supply(self) -> float:
        return MAYHEM_SUPPLY if self.mayhem else TOTAL_SUPPLY

    @property
    def market_cap_sol(self) -> float:
        """Price x real total supply (Mayhem tokens have 2B; curve.market_cap_sol assumes 1B)."""
        return self.curve.price * self.supply

    # ---- updates ------------------------------------------------------------
    def on_launch(self, e: Launch) -> None:
        self.curve = Curve(e.v_sol, e.v_tokens)
        self.price_known = True
        self.peak_price = self.curve.price
        if e.dev_buy_tokens > 0:
            self.holders[e.creator] = e.dev_buy_tokens
            self.buyers.add(e.creator)

    def _apply_reserves(self, t: Trade) -> None:
        """The curve after `t`, in chain order, not arrival order: some endpoints deliver trades late or out of order
        (RPC Fast, 2026-10-06: ~5% within their slot). A trade from an older slot doesn't move the price back. Within
        one slot, the trades chain by reserves (each starts where another ended): the newest state is the one no
        other trade starts from. While a link is missing, a trade that doesn't continue the current state leaves it."""
        if not t.slot:                                   # no chain order known (synthetic, old recordings): arrival
            self.curve = Curve(t.v_sol, t.v_tokens)
            return
        if t.slot < self.curve_slot:
            return
        if t.slot > self.curve_slot:
            self.curve_slot, self.slot_moves = t.slot, []
        before = t.v_tokens + t.tokens if t.side == "buy" else t.v_tokens - t.tokens
        first = not self.slot_moves
        self.slot_moves.append((before, t.v_tokens, t.v_sol))
        starts = [b for b, _, _ in self.slot_moves]
        heads = [m for m in self.slot_moves if not any(abs(m[1] - b) < 1.0 for b in starts)]
        cur = self.curve.v_tokens
        if not first and abs(before - cur) >= 1.0 and any(abs(h[1] - cur) < 1.0 for h in heads):
            return          # a separate piece of the chain (its link hasn't arrived): keep the newest state known
        _, vt, vs = (heads or self.slot_moves)[-1]
        self.curve = Curve(vs, vt)

    def on_trade(self, t: Trade, bundle_window_s: float, sniper_window_s: float = 10.0) -> None:
        if t.pool == "pump" and t.v_sol > 0 and t.v_tokens > 0:
            self._apply_reserves(t)
            self.price_known = True
        elif t.pool != "pump" and t.mcap_sol > 0:
            # graduated (PumpSwap etc.): no curve reserves in the event, so price it from market cap on a
            # curve parked at its end state. Its depth roughly matches the migrated pool's.
            self.migrated = True
            price = t.mcap_sol / self.supply
            self.curve = Curve(price * FINAL_V_TOKENS, FINAL_V_TOKENS, amm=True)
            self.price_known = True
        price = self.curve.price
        self.peak_price = max(self.peak_price, price)
        self.last_trade_ts = t.ts
        self.trades.append((t.ts, price, t.side, t.sol, t.trader))
        if t.trader in self.NON_ORGANIC:              # price only: not demand, not a buyer, not a holder
            self.non_organic_trades += 1
            if t.trader == MAYHEM_AGENT:
                self.mayhem = True
            return
        self.volume_sol += t.sol
        prev = self.holders.get(t.trader, 0.0)
        if t.side == "buy":
            self.buys += 1
            self.buyers.add(t.trader)
            bal = prev + t.tokens
            if t.trader != self.creator:
                if t.ts - self.created_ts <= bundle_window_s:
                    self.early_bought[t.trader] = self.early_bought.get(t.trader, 0.0) + t.tokens
                if t.ts - self.created_ts <= sniper_window_s:
                    self.snipers.add(t.trader)
        else:
            self.sells += 1
            self.sellers.add(t.trader)
            bal = max(prev - t.tokens, 0.0)
            if t.trader == self.creator:
                self.dev_sold += t.tokens
            if t.trader in self.early_bought:
                self.early_sold += min(t.tokens, prev)
        self.holders[t.trader] = t.new_balance if t.new_balance >= 0 else bal
        if self.holders[t.trader] <= 0:
            del self.holders[t.trader]

    # ---- metrics ------------------------------------------------------------
    def dev_pct(self) -> float:
        return self.holders.get(self.creator, 0.0) / self.supply * 100 if self.creator else 0.0

    def dev_initial_pct(self) -> float:
        return (self.launch.dev_buy_tokens / self.supply * 100) if self.launch else 0.0

    def bundle_pct(self) -> float:
        """Supply bought by non-dev wallets inside the bundle window (insider/sniper proxy)."""
        return sum(self.early_bought.values()) / self.supply * 100

    def fees_paid_sol(self, fee_pct: float = 1.25) -> float:
        """Total trading fees paid on the curve so far - a proxy for real, paying demand."""
        return self.volume_sol * fee_pct / 100

    def sniper_pct(self) -> float:
        """Supply currently held by wallets that bought within the sniper window (excl. dev)."""
        return sum(self.holders.get(w, 0.0) for w in self.snipers) / self.supply * 100

    def insider_pct(self) -> float:
        """Supply currently held by the dev + bundle-window wallets. (Funding-graph clustering would
        catch more insiders - see roadmap.)"""
        ws = set(self.early_bought) | ({self.creator} if self.creator else set())
        return sum(self.holders.get(w, 0.0) for w in ws) / self.supply * 100

    def early_sold_ratio(self) -> float:
        total = sum(self.early_bought.values())
        return self.early_sold / total if total else 0.0

    def top_holders_pct(self, n: int) -> float:
        return sum(sorted(self.holders.values(), reverse=True)[:n]) / self.supply * 100

    def window(self, now: float, seconds: float) -> list[tuple]:
        """Organic trades of the last `seconds` (flow, buyers). Price history uses self.trades directly."""
        return [t for t in self.trades if t[0] >= now - seconds and t[4] not in self.NON_ORGANIC]

    def net_flow_sol(self, now: float, seconds: float) -> float:
        return sum(t[3] if t[2] == "buy" else -t[3] for t in self.window(now, seconds))

    def buyers_in(self, now: float, seconds: float) -> int:
        return len({t[4] for t in self.window(now, seconds) if t[2] == "buy"})

    def last_high_ts(self) -> float:
        best, ts = 0.0, self.created_ts
        for t in self.trades:
            if t[1] >= best:
                best, ts = t[1], t[0]
        return ts

    def sparkline(self, points: int = 60) -> list[float]:
        prices = [t[1] for t in self.trades]
        if len(prices) <= points:
            return prices
        step = len(prices) / points
        return [prices[int(i * step)] for i in range(points)] + [prices[-1]]
