"""Per-token live state built from the trade stream: holders, early buyers, flow, dev activity."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar

from .curve import FINAL_V_TOKENS, TOTAL_SUPPLY, Curve
from .events import Launch, Social, Trade

LEDGER_MAX = 3000      # trades kept per coin to rebuild it after a fork repair; past this a repair can't be exact
TRADE_FIELDS = ("curve", "holders", "buyers", "sellers", "early_bought", "early_sold", "snipers", "volume_sol",
                "dev_sold", "buys", "sells", "trades", "peak_price", "last_trade_ts", "migrated", "price_known",
                "non_organic_trades", "curve_slot", "slot_moves", "slot_start", "net", "sold_by", "early_c")


def _content(t: Trade) -> tuple:
    """What a trade event says, apart from where it landed: two deliveries with the same content are one event."""
    return (t.trader, t.side, round(t.sol, 9), round(t.tokens, 6), round(t.v_sol, 9), round(t.v_tokens, 6),
            t.new_balance, t.pool, t.mcap_sol)

# pump.fun's Mayhem-mode agent: a Mayhem token mints 2B, half of it to this wallet, which then trades it
MAYHEM_AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
MAYHEM_SUPPLY = 2 * TOTAL_SUPPLY
# Concentration (dev, bundle, snipers, insiders, top holders) is a share of the TRADABLE supply: the 1B on the curve.
# A Mayhem coin mints 2B, but ~1B sits with the Mayhem agent (checked on-chain 2026-10-06): dividing by 2B would halve
# every risk share on those coins. Market cap uses the full minted supply (`supply`).
TRADABLE = TOTAL_SUPPLY


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
    mayhem: bool = False          # Mayhem mode (2B supply, the agent trades it): read from its curve, or its first agent trade
    mayhem_checked: bool = False  # its curve account was read: `mayhem` is known, not just "not seen yet"
    non_organic_trades: int = 0
    curve_slot: int = 0           # the slot of the newest reserves applied to `curve`
    slot_moves: list = field(default_factory=list)   # that slot's trades: (tokens before, tokens after, sol after)
    slot_start: tuple | None = None                  # (v_tokens, v_sol) before that slot's first trade
    net: dict = field(default_factory=dict)          # wallet -> tokens bought minus sold, signed: any arrival order
    sold_by: dict = field(default_factory=dict)      # wallet -> tokens it sold (early buyers' dumps, order-free)
    early_c: dict = field(default_factory=dict)      # early buyer -> its part of early_sold
    seen: dict = field(default_factory=dict)         # trade identity (signature, event index) -> its content
    partial: bool = False                            # restored after a gap: other wallets' balances aren't known
    # fork repair (a sixth review, 2026-10-07): the trades applied, in arrival order, so the coin can be rebuilt when
    # the chain shows a different version of one of them was the real one
    ledger: list = field(default_factory=list)       # (trade, bundle_window_s, sniper_window_s)
    ledger_at: dict = field(default_factory=dict)    # identity -> index in ledger
    ledger_full: bool = False                        # past LEDGER_MAX: a repair can't replay it exactly
    conflicts: dict = field(default_factory=dict)    # identity -> {"versions": [Trade], "status": ..., ...}
    new_conflicts: list = field(default_factory=list)   # identities the engine hasn't asked the chain about yet
    unrepairable: bool = False                       # a conflict it couldn't rebuild from (restored coin, full ledger)
    safety_hold: str = ""                            # a block carried across a restart (the engine's fork registry)
    replacements: list = field(default_factory=list)   # (old trade, new trade or None): for downstream consumers

    # a conflict keeps two things apart (an eighth review, 2026-10-07):
    # - its STRONGEST EVIDENCE, which only stronger evidence replaces: the chain's answer (slot, level "confirmed" or
    #   "final", failed or not, source, method, the content it proved) that the coin's state is built on;
    # - its STATUS, the state of the resolution: "unresolved" (provisional, or new content arrived since the evidence:
    #   unsafe until a fresh answer at least as strong agrees), "confirmed" (usable, re-checked until finalized),
    #   "final", "unresolvable" (no answer, or one matching no single delivered version) and "retracted" (the
    #   transaction failed on chain: its trade never happened; stays unsafe).
    # Weaker evidence, success or failure, never changes the state. Two finalized answers that disagree are an
    # integrity fault (`fault`): unresolvable for good, never a reason to pick the later one.
    SAFE: ClassVar[tuple] = ("confirmed", "final")
    LEVEL: ClassVar[dict] = {"": 0, "confirmed": 1, "final": 2}

    @property
    def unsafe(self) -> str:
        """Why this coin's trade-derived state can't be trusted for a new automated entry, or ""."""
        if self.safety_hold:
            return self.safety_hold
        if self.unrepairable:
            return "fork conflict: state can't be rebuilt"
        st = [c["status"] for c in self.conflicts.values()]
        if any(c.get("fault") for c in self.conflicts.values()):
            return "fork conflict: the chain's finalized answers contradict each other"
        if "retracted" in st:
            return "fork conflict: a version's transaction failed on chain"
        if any(x not in self.SAFE for x in st):
            return "fork conflict unresolved"
        return ""

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
    _launched: bool = False

    def on_launch(self, e: Launch) -> None:
        self._launched = True
        self.curve = Curve(e.v_sol, e.v_tokens)
        self.price_known = True
        self.peak_price = self.curve.price
        if e.dev_buy_tokens > 0:
            self.holders[e.creator] = self.net[e.creator] = e.dev_buy_tokens
            self.buyers.add(e.creator)

    def _apply_reserves(self, t: Trade) -> None:
        """The curve after `t`, in chain order, not arrival order: some endpoints deliver trades late or out of order
        (RPC Fast, 2026-10-06: ~5% within their slot). A trade from an older slot doesn't move the price back. Within a
        slot, each trade is a step from the reserves before it to the reserves after; in chain order the steps form one
        path, so the slot's last state is the one reached once more than it's left - whatever order they arrived in,
        and when the path revisits a state (buy, sell, buy). While a step is missing (several such states) the current
        state stays; a slot whose steps return to where it started ends there."""
        if not t.slot:                                   # no chain order known (synthetic, old recordings): arrival
            self.curve = Curve(t.v_sol, t.v_tokens)
            return
        if t.slot < self.curve_slot:
            return
        if t.slot > self.curve_slot:
            self.slot_start = (self.curve.v_tokens, self.curve.v_sol) if self.price_known else None
            self.curve_slot, self.slot_moves = t.slot, []
        before = t.v_tokens + t.tokens if t.side == "buy" else t.v_tokens - t.tokens
        self.slot_moves.append((before, t.v_tokens, t.v_sol))
        bal: dict[int, int] = {}
        at: dict[int, tuple] = {}
        for b0, a0, vs in self.slot_moves:
            bal[round(b0)] = bal.get(round(b0), 0) - 1
            bal[round(a0)] = bal.get(round(a0), 0) + 1
            at[round(a0)] = (a0, vs)
        ends = [k for k, v in bal.items() if v > 0]
        if len(ends) == 1:
            vt, vs = at[ends[0]]
            self.curve = Curve(vs, vt)
        elif not ends and self.slot_start:               # a closed loop: the slot ends where it began
            self.curve = Curve(self.slot_start[1], self.slot_start[0])

    def _disposition(self, t: Trade) -> str:
        """The same trade EVENT delivered again: its identity is the transaction's signature and the event's place in
        it, not its content (one transaction can hold two identical buys). Without an event index (older recordings)
        nothing is dropped - uniqueness isn't invented.

        Delivered again with the SAME content (a reconnect, or the same execution in a fork's next slot): counted once.
        With DIFFERENT content, it's a fork: the transaction executed in a block that didn't survive and again in one
        that did, and only the chain knows which landed. The coin is flagged (no new automated entry), the engine asks
        the chain (`resolve_conflict`), and meanwhile it's rebuilt provisionally with the later slot's version - the
        one that landed in all 9 of 9 cases checked on 10-07. Never final without the chain's answer."""
        if not t.signature or t.event_index < 0:
            return "new"
        k = (t.signature, t.event_index)
        new = _content(t)
        if k not in self.seen:
            self.seen[k] = new
            if len(self.seen) > 4096:
                self.seen.pop(next(iter(self.seen)))
            return "new"
        c = self.conflicts.get(k)
        if self.seen[k] == new or (c is not None and any(v is not None and _content(v) == new for v in c["versions"])):
            return "repeat"
        if c is None:
            first = self.ledger[self.ledger_at[k]][0] if k in self.ledger_at else None
            c = self.conflicts[k] = {"versions": [first] if first is not None else [], "status": "unresolved",
                                     "history": [], "strongest": None, "fault": ""}
            self.new_conflicts.append(k)
        elif c["status"] != "unresolved":                 # new contradicting content reopens a settled event: unsafe
            c["status"], c["reopened"] = "unresolved", c.get("reopened", 0) + 1     # until it's verified again - but
            self.new_conflicts.append(k)                  # the evidence already established stays (`strongest`)
        c["versions"].append(t)
        applied = self.ledger[self.ledger_at[k]][0] if k in self.ledger_at else None
        if applied is not None and t.slot > applied.slot and not c.get("strongest"):
            self._replace(k, t)                         # provisional, with no evidence yet: the later slot's version
        return "conflict"

    def _replace(self, k: tuple, t: Trade | None) -> None:
        """Rebuild the coin's trade-derived state with `t` in place of the version applied for identity k (None:
        the event is retracted - its transaction failed). Downstream consumers get (old, new) in `replacements`."""
        if self.partial or self.ledger_full or k not in self.ledger_at:
            self.unrepairable = True                     # (restored mid-life, or too long to replay exactly)
            return
        i = self.ledger_at[k]
        old, bw, sw = self.ledger[i]
        self.ledger[i] = (t, bw, sw)
        if t is not None:
            self.seen[k] = _content(t)
        self.replacements.append((old, t))
        fresh = TokenState(self.mint, self.launch, self.first_seen)
        if self.launch is not None and self._launched:
            fresh.on_launch(self.launch)
        for tr, b, s in self.ledger:
            if tr is not None:
                fresh._apply(tr, b, s)
        for name in TRADE_FIELDS:
            setattr(self, name, getattr(fresh, name))
        self.mayhem = self.mayhem or fresh.mayhem

    def resolve_conflict(self, k: tuple, slot: int, status: str = "", source: str = "", err: str = "",
                         content: tuple | None = None, method: str = "") -> str:
        """The chain's answer for a conflicting event: the slot its transaction landed in, at what confirmation,
        whether it failed, and - when the transaction itself was fetched and decoded - the event's content there.
        Returns "kept", "replaced", "retracted", "unresolvable" or "" (nothing changed).
        - evidence is ranked by level (finalized over confirmed); weaker evidence never changes the state, success
          or failure, and the same evidence again changes nothing;
        - a FAILED transaction: its trade never happened - retracted from the coin, which stays unsafe;
        - with the decoded content, the canonical version is the delivered one with that content; without it, the one
          delivered from the landed slot - and two differing copies from one slot can't be told apart that way;
        - two finalized answers that disagree on the slot or on failure, or two finalized transaction contents that
          disagree, are an integrity fault, not a choice. A version chosen by its slot alone is an inference: the
          transaction's decoded content can replace it at the same level without any contradiction."""
        c = self.conflicts.get(k)
        if c is None:
            return ""
        c.setdefault("strongest", None)
        c.setdefault("fault", "")
        level = "final" if status == "finalized" else "confirmed" if status == "confirmed" else ""
        ev = {"slot": slot, "level": level, "status": status, "source": source, "err": err,
              "method": method or ("tx" if content is not None else "status"),
              "content": list(content) if content is not None else None}
        hist = c.setdefault("history", [])
        if ev in hist and c["status"] != "unresolved":
            return ""                                    # the same evidence again: idempotent
        if ev not in hist:
            hist.append(ev)
        c["evidence"] = ev                               # (the latest answer; `strongest` is what counts)
        if c["fault"]:
            return ""                                    # contradictory finalized answers: nothing settles it now
        if not level:                                    # no answer in time: a state, not evidence
            if c["status"] == "unresolvable" or (c["strongest"] or {}).get("level") == "final":
                return ""                                # (a finalized answer stands)
            c["status"] = "unresolvable"
            return "unresolvable"
        strong = c["strongest"]                          # the STATUS facts: slot, level, failed or not
        proof = c.get("proof")                           # the strongest decoded transaction CONTENT, if any
        rank = self.LEVEL
        if strong is not None and rank[level] < rank[strong["level"]]:
            return ""                                    # weaker than what's established: recorded, never applied
        if strong is not None and (strong["slot"] != slot or bool(strong["err"]) != bool(err)):
            if strong["level"] == level == "final":
                c["fault"] = "finalized answers contradict each other (slot or failure)"
                c["status"] = "unresolvable"
                return "unresolvable"
            if strong["level"] == level:                 # two confirmed answers disagree: wait for finalized
                c["status"] = "unresolvable"
                return "unresolvable"
        if content is not None and proof is not None and proof["slot"] == slot and proof["level"] == level == "final" \
                and list(proof["content"]) != list(content):
            c["fault"] = "two finalized transaction contents contradict each other"
            c["status"] = "unresolvable"
            return "unresolvable"
        if err:
            canon = None
        else:
            if content is not None:
                match = [v for v in c["versions"] if v is not None and _content(v) == tuple(content)]
            else:
                match = [v for v in c["versions"] if v is not None and v.slot == slot]
            if len({_content(v) for v in match}) != 1:
                c["status"] = "unresolvable"             # doesn't identify exactly one delivered version: unsafe
                return "unresolvable"
            canon = match[0]
        applied = self.ledger[self.ledger_at[k]][0] if k in self.ledger_at else None
        out = ""
        if canon is None:
            if applied is not None:
                self._replace(k, None)
                out = "retracted"
            elif c["status"] != "retracted":
                out = "retracted"
        elif applied is None or _content(applied) != _content(canon) or applied.slot != canon.slot:
            self._replace(k, canon)
            out = "replaced"
        elif c["status"] != level:
            out = "kept"
        if self.unrepairable:
            c["status"] = "unresolvable"
            return "unresolvable"
        if strong is None or rank[level] > rank[strong["level"]]:
            strong = c["strongest"] = {**ev, "content": None, "inferred": None}
        if canon is None or (proof is not None and proof["slot"] != slot):
            proof = c["proof"] = None                    # a failed transaction, or a proof of another slot: stale
        if content is not None and canon is not None and (proof is None or rank[level] >= rank[proof["level"]]):
            proof = c["proof"] = {"level": level, "slot": slot, "content": list(content), "source": source}
        # what `strongest` shows: content only when PROVED from the transaction; a version picked by its slot alone is
        # an inference (`inferred`), never evidence of its bytes (a ninth review, 2026-10-07)
        strong["content"] = list(proof["content"]) if proof is not None else None
        strong["method"] = "tx" if proof is not None and rank[proof["level"]] >= rank[strong["level"]] else "status"
        strong["inferred"] = None if canon is None or proof is not None else list(_content(canon))
        c["status"], c["slot"] = ("retracted" if canon is None else level), slot
        return out

    def _early(self, w: str) -> None:
        """early_sold as the sum over early buyers of min(sold, bought early): the same in any arrival order."""
        new = min(self.sold_by.get(w, 0.0), self.early_bought.get(w, 0.0))
        self.early_sold += new - self.early_c.get(w, 0.0)
        self.early_c[w] = new

    def on_trade(self, t: Trade, bundle_window_s: float, sniper_window_s: float = 10.0) -> str:
        """Apply a delivered trade event; returns its disposition: "new" (applied), "repeat" (the same event again:
        nothing changes) or "conflict" (a differing version of an event already seen - see `_disposition`)."""
        d = self._disposition(t)
        if d != "new":
            return d
        if len(self.ledger) < LEDGER_MAX:
            if t.signature and t.event_index >= 0:
                self.ledger_at[(t.signature, t.event_index)] = len(self.ledger)
            self.ledger.append((t, bundle_window_s, sniper_window_s))
        else:
            self.ledger_full = True
        self._apply(t, bundle_window_s, sniper_window_s)
        return "new"

    def _apply(self, t: Trade, bundle_window_s: float, sniper_window_s: float) -> None:
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
        w = t.trader
        if t.side == "buy":
            self.buys += 1
            self.buyers.add(w)
            if w != self.creator:
                if t.ts - self.created_ts <= bundle_window_s:
                    self.early_bought[w] = self.early_bought.get(w, 0.0) + t.tokens
                    self._early(w)
                if t.ts - self.created_ts <= sniper_window_s:
                    self.snipers.add(w)
        else:
            self.sells += 1
            self.sellers.add(w)
            if w == self.creator:
                self.dev_sold += t.tokens
            self.sold_by[w] = self.sold_by.get(w, 0.0) + t.tokens
            if w in self.early_bought:
                self._early(w)
        # balances as signed sums: a sell that arrives before its buy nets out instead of being clipped at 0
        n = t.new_balance if t.new_balance >= 0 else self.net.get(w, 0.0) + (t.tokens if t.side == "buy" else -t.tokens)
        self.net[w] = n
        if n > 0:
            self.holders[w] = n
        else:
            self.holders.pop(w, None)

    # ---- metrics ------------------------------------------------------------
    def dev_pct(self) -> float:
        return self.holders.get(self.creator, 0.0) / TRADABLE * 100 if self.creator else 0.0

    def dev_initial_pct(self) -> float:
        return (self.launch.dev_buy_tokens / TRADABLE * 100) if self.launch else 0.0

    def bundle_pct(self) -> float:
        """Supply bought by non-dev wallets inside the bundle window (insider/sniper proxy)."""
        return sum(self.early_bought.values()) / TRADABLE * 100

    def fees_paid_sol(self, fee_pct: float = 1.25) -> float:
        """Total trading fees paid on the curve so far - a proxy for real, paying demand."""
        return self.volume_sol * fee_pct / 100

    def sniper_pct(self) -> float:
        """Supply currently held by wallets that bought within the sniper window (excl. dev)."""
        return sum(self.holders.get(w, 0.0) for w in self.snipers) / TRADABLE * 100

    def insider_pct(self) -> float:
        """Supply currently held by the dev + bundle-window wallets. (Funding-graph clustering would
        catch more insiders - see roadmap.)"""
        ws = set(self.early_bought) | ({self.creator} if self.creator else set())
        return sum(self.holders.get(w, 0.0) for w in ws) / TRADABLE * 100

    def early_sold_ratio(self) -> float:
        total = sum(self.early_bought.values())
        return self.early_sold / total if total else 0.0

    def top_holders_pct(self, n: int) -> float:
        return sum(sorted(self.holders.values(), reverse=True)[:n]) / TRADABLE * 100

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
