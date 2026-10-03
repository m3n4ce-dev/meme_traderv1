"""Per-token live state built from the trade stream: holders, early buyers, flow, dev activity."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .curve import FINAL_V_TOKENS, TOTAL_SUPPLY, Curve
from .events import Launch, Social, Trade


@dataclass
class TokenState:
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
    price_known: bool = False     # False until a launch/trade gave us real reserves (e.g. right after a restart)

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

    # ---- updates ------------------------------------------------------------
    def on_launch(self, e: Launch) -> None:
        self.curve = Curve(e.v_sol, e.v_tokens)
        self.price_known = True
        self.peak_price = self.curve.price
        if e.dev_buy_tokens > 0:
            self.holders[e.creator] = e.dev_buy_tokens
            self.buyers.add(e.creator)

    def on_trade(self, t: Trade, bundle_window_s: float, sniper_window_s: float = 10.0) -> None:
        if t.pool == "pump" and t.v_sol > 0 and t.v_tokens > 0:
            self.curve = Curve(t.v_sol, t.v_tokens)
            self.price_known = True
        elif t.pool != "pump" and t.mcap_sol > 0:
            # graduated (PumpSwap etc.): no curve reserves in the event, so price it from market cap on a
            # curve parked at its end state. Its depth roughly matches the migrated pool's.
            self.migrated = True
            price = t.mcap_sol / TOTAL_SUPPLY
            self.curve = Curve(price * FINAL_V_TOKENS, FINAL_V_TOKENS)
            self.price_known = True
        self.volume_sol += t.sol
        price = self.curve.price
        self.peak_price = max(self.peak_price, price)
        self.last_trade_ts = t.ts
        self.trades.append((t.ts, price, t.side, t.sol, t.trader))
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
        return self.holders.get(self.creator, 0.0) / TOTAL_SUPPLY * 100 if self.creator else 0.0

    def dev_initial_pct(self) -> float:
        return (self.launch.dev_buy_tokens / TOTAL_SUPPLY * 100) if self.launch else 0.0

    def bundle_pct(self) -> float:
        """Supply bought by non-dev wallets inside the bundle window (insider/sniper proxy)."""
        return sum(self.early_bought.values()) / TOTAL_SUPPLY * 100

    def fees_paid_sol(self, fee_pct: float = 1.25) -> float:
        """Total trading fees paid on the curve so far - a proxy for real, paying demand."""
        return self.volume_sol * fee_pct / 100

    def sniper_pct(self) -> float:
        """Supply currently held by wallets that bought within the sniper window (excl. dev)."""
        return sum(self.holders.get(w, 0.0) for w in self.snipers) / TOTAL_SUPPLY * 100

    def insider_pct(self) -> float:
        """Supply currently held by the dev + bundle-window wallets. (Funding-graph clustering would
        catch more insiders - see roadmap.)"""
        ws = set(self.early_bought) | ({self.creator} if self.creator else set())
        return sum(self.holders.get(w, 0.0) for w in ws) / TOTAL_SUPPLY * 100

    def early_sold_ratio(self) -> float:
        total = sum(self.early_bought.values())
        return self.early_sold / total if total else 0.0

    def top_holders_pct(self, n: int) -> float:
        return sum(sorted(self.holders.values(), reverse=True)[:n]) / TOTAL_SUPPLY * 100

    def window(self, now: float, seconds: float) -> list[tuple]:
        return [t for t in self.trades if t[0] >= now - seconds]

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
