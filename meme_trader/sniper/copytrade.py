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
from collections import OrderedDict, defaultdict, deque
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
    unknown_bags: int = 0         # coins whose observed accounting isn't exact (opening inventory unknown, etc.)


OBSERVED = ("trades", "closed", "wins", "realized_sol")      # the leader's own results: rebuilt from its events
LEDGER_EVENTS = 256        # events kept per wallet x coin for exact corrections; older ones fold into its snapshot
PAIR_IDLE_S = 3600         # a flat wallet x coin with no event for this long folds into the wallet's totals
FOLDED_IDS_KEPT = 50_000   # identities remembered after folding, so a late correction is marked, not re-counted


@dataclass
class _Bag:
    tokens: float = 0.0
    cost: float = 0.0
    pnl: float = 0.0
    last_buy_price: float = 0.0


@dataclass
class _Pair:
    """One followed wallet's events on one coin, in chain order, so a correction is replayed exactly (an eighth
    review, 2026-10-07: correcting a buy before a later sell isn't the same as adding the difference to what's left -
    the sell's cost share, the realized profit, the closed bag and the win all change)."""
    start: _Bag = field(default_factory=_Bag)          # the bag before `events` (what's folded in can't be corrected)
    start_n: list = field(default_factory=lambda: [0, 0, 0, 0.0])   # trades, closed, wins, realized before `events`
    start_unknown: str = ""
    events: list = field(default_factory=list)         # [seq, trade or None (retracted / moved), slot, identity]
    n: list = field(default_factory=lambda: [0, 0, 0, 0.0])         # the same totals after `events`
    unknown: str = ""                                  # why its accounting isn't exact: "" = it is
    forced: str = ""                                   # (a correction or an arrival that couldn't be replayed)
    last_ts: float = 0.0
    boundary: list = field(default_factory=list)       # the folded events of the checkpoint's slot (its order context)
    checkpoint: int = 0                                # the newest slot folded into `start`: older arrivals can't be
                                                       # placed in order any more (a ninth review, 2026-10-07)


class LeaderBook:
    def __init__(self, leaders: list, path: Path | None = None):
        self.leaders: dict[str, Leader] = {}
        for x in leaders or []:
            d = dict(x)
            self.leaders[d["address"]] = Leader(**{k: d[k] for k in ("address", "label", "mode", "enabled") if k in d})
        self.stats: dict[str, LeaderStats] = defaultdict(LeaderStats)
        self.bags: dict[tuple[str, str], _Bag] = defaultdict(_Bag)
        self.recent_copies: dict[str, deque] = defaultdict(deque)
        self.pairs: dict[tuple[str, str], _Pair] = {}
        self.by_wallet: dict[str, dict[str, _Pair]] = defaultdict(dict)
        self.base: dict[str, list] = {}               # wallet -> observed totals not in any pair (earlier runs, folded)
        self.base_unknown: dict[str, int] = defaultdict(int)
        self.where: dict[tuple, tuple] = {}           # event identity (signature, index) -> ((wallet, mint), seq)
        self.folded_ids: OrderedDict = OrderedDict()  # identities whose events were folded/pruned away -> wallet
        self._seq = self._calls = 0
        self._newest_ts = 0.0
        self.path = path
        if path and path.exists():
            fields = LeaderStats.__dataclass_fields__
            for addr, st in json.loads(path.read_text()).items():
                self.stats[addr] = LeaderStats(**{k: v for k, v in st.items() if k in fields})
                self.base[addr] = [getattr(self.stats[addr], f) for f in OBSERVED]
                self.base_unknown[addr] = self.stats[addr].unknown_bags

    def is_leader(self, wallet: str) -> bool:
        lead = self.leaders.get(wallet)
        return bool(lead and lead.enabled and not self.stats[wallet].paused_reason)

    def label(self, wallet: str) -> str:
        lead = self.leaders.get(wallet)
        return (lead.label or wallet[:6]) if lead else wallet[:6]

    def bag(self, wallet: str, mint: str) -> _Bag:
        return self.bags[(wallet, mint)]

    # ---- the observed ledger -------------------------------------------------------------------------------------
    @staticmethod
    def _step(b: _Bag, n: list, t: Trade) -> tuple[float, str]:
        """One event on a bag and its totals; (fraction of the bag sold, why the accounting became inexact or "")."""
        n[0] += 1
        if t.side == "buy":
            b.tokens += t.tokens
            b.cost += t.sol
            b.last_buy_price = t.sol / t.tokens if t.tokens else 0.0
            return 0.0, ""
        if b.tokens <= 0:
            return 1.0, "a sell with no buy seen: its opening inventory is unknown"
        why = "sold more than it was seen buying: its opening inventory is unknown" \
            if t.tokens > b.tokens * (1 + 1e-9) else ""
        frac = min(t.tokens / b.tokens, 1.0)
        cost_part = b.cost * frac
        proceeds = t.sol * (b.tokens / t.tokens) if why else t.sol     # only the tokens it was seen buying
        b.tokens *= 1 - frac
        b.cost -= cost_part
        b.pnl += proceeds - cost_part
        n[3] += proceeds - cost_part
        if b.tokens <= 1e-6:
            n[1] += 1
            n[2] += b.pnl > 0
            b.tokens = b.cost = b.pnl = b.last_buy_price = 0.0          # the round trip is over: a new bag next
        return frac, why

    def _pair(self, key: tuple[str, str]) -> _Pair:
        p = self.pairs.get(key)
        if p is None:
            p = self.pairs[key] = self.by_wallet[key[0]][key[1]] = _Pair()
        return p

    @staticmethod
    def _ident(t: Trade | None) -> tuple | None:
        return (t.signature, t.event_index) if t is not None and t.signature and t.event_index >= 0 else None

    ORDER_UNPROVEN = "transactions in one slot, with a sell among them: their order isn't proven"

    @staticmethod
    def _slot_ambiguity(trades) -> str:
        """Two or more transactions sharing a slot, any of whose events is a sell: (slot, arrival) doesn't prove the
        transactions' order, and a sell's cost share depends on it (a ninth and tenth review). The whole slot counts:
        a transaction that sells and then buys can't hide its sell behind its own buy. Buys alone commute. The event
        index orders the events inside one transaction, never the transactions inside a slot."""
        by: dict = {}
        for t in trades:
            if t is not None and t.slot:
                g = by.setdefault(t.slot, [set(), False])
                g[0].add(t.signature)
                g[1] = g[1] or t.side == "sell"
        return LeaderBook.ORDER_UNPROVEN if any(len(sigs) > 1 and sell for sigs, sell in by.values()) else ""

    def _rebuild(self, key: tuple[str, str]) -> dict:
        """Replay the pair from its snapshot, in chain order: (slot, then arrival). Returns each event's sold fraction.
        Unknown never becomes known here: the snapshot keeps the reasons of what was folded into it."""
        p = self.pairs[key]
        p.events.sort(key=lambda e: (e[2], e[0]))
        b = _Bag(**asdict(p.start))
        n, why, fr = list(p.start_n), p.start_unknown, {}
        for seq, t, _, _ in p.events:
            if t is not None:
                fr[seq], w = self._step(b, n, t)
                why = why or w
        why = why or self._slot_ambiguity([t for _, t, _, _ in p.events] + p.boundary)
        self.bags[key], p.n, p.unknown = b, n, p.forced or why
        return fr

    def _restat(self, wallet: str) -> None:
        """The wallet's observed totals: what's not in a pair, plus each pair's - recomputed, never adjusted by deltas,
        so a corrected history gives exactly the numbers a fresh replay of it would."""
        st = self.stats[wallet]
        tot = list(self.base.get(wallet, [0, 0, 0, 0.0]))
        unknown = self.base_unknown.get(wallet, 0)
        for p in self.by_wallet.get(wallet, {}).values():
            for i in range(4):
                tot[i] += p.n[i]
            unknown += bool(p.unknown)
        st.trades, st.closed, st.wins, st.realized_sol = tot
        st.unknown_bags = unknown

    def _insert(self, t: Trade) -> tuple[tuple, int]:
        key = (t.trader, t.mint)
        p = self._pair(key)
        self._seq += 1
        ident = self._ident(t)
        p.events.append([self._seq, t, t.slot, ident])
        p.last_ts = max(p.last_ts, t.ts)
        if ident:
            self.where[ident] = (key, self._seq)
        if t.slot and p.checkpoint and t.slot <= p.checkpoint:
            p.forced = p.forced or "an event older than its folded snapshot arrived: its order can't be replayed"
        return key, self._seq

    def _forget(self, key: tuple[str, str], seq: int, ident: tuple | None) -> None:
        """An event leaves the replayable ledger (folded or pruned): its identity is remembered, so a later correction
        for it marks the history unknown instead of counting it again."""
        if ident and self.where.get(ident, (None, None))[1] == seq:
            del self.where[ident]
        if ident:
            self.folded_ids[ident] = key[0]
            while len(self.folded_ids) > FOLDED_IDS_KEPT:
                self.folded_ids.popitem(last=False)

    def _fold(self, key: tuple[str, str]) -> None:
        """Past LEDGER_EVENTS, the oldest events move into the snapshot (they can't be corrected after that)."""
        p = self.pairs[key]
        p.events.sort(key=lambda e: (e[2], e[0]))
        ambiguous = self._slot_ambiguity([t for _, t, _, _ in p.events] + p.boundary)
        while len(p.events) > LEDGER_EVENTS:
            seq, t, slot, ident = p.events.pop(0)
            if t is not None:
                _, w = self._step(p.start, p.start_n, t)
                p.start_unknown = p.start_unknown or w
                if slot and slot > p.checkpoint:
                    p.boundary = []                      # a newer checkpoint slot: its own context starts
                if slot:
                    p.boundary.append(t)
            p.checkpoint = max(p.checkpoint, slot or 0)
            self._forget(key, seq, ident)
        p.start_unknown = p.start_unknown or ambiguous   # folding never turns an unknown order into a known one

    def _prune(self) -> None:
        """A flat bag with no event for PAIR_IDLE_S folds into its wallet's totals (fork corrections come in minutes).
        Every event's identity goes, retracted ones (tombstones) included."""
        for key, p in list(self.pairs.items()):
            if self.bags.get(key, _Bag()).tokens <= 1e-6 and p.last_ts < self._newest_ts - PAIR_IDLE_S:
                tot = self.base.setdefault(key[0], [0, 0, 0, 0.0])
                for i in range(4):
                    tot[i] += p.n[i]
                self.base_unknown[key[0]] += bool(p.unknown)
                for seq, _, _, ident in p.events:
                    self._forget(key, seq, ident)
                del self.pairs[key], self.by_wallet[key[0]][key[1]]
                self.bags.pop(key, None)

    def on_trade(self, t: Trade) -> float:
        """Count one of the leader's trade events. For sells returns the fraction of their bag sold (0..1)."""
        key = (t.trader, t.mint)
        p = self._pair(key)
        last = p.events[-1] if p.events else None
        key, seq = self._insert(t)
        if p.forced or last is None or (t.slot, seq) < (last[2], last[0]):   # out of order, or marked: replay
            frac = self._rebuild(key)[seq]
        else:                                                                 # in chain order: one more step
            frac, why = self._step(self.bags[key], p.n, t)
            same = [e[1] for e in p.events if e[2] == t.slot] + [b for b in p.boundary if b.slot == t.slot]
            p.unknown = p.unknown or why or self._slot_ambiguity(same)
        if len(p.events) > LEDGER_EVENTS:
            self._fold(key)
        self._newest_ts = max(self._newest_ts, t.ts)
        self._restat(t.trader)
        self._calls += 1
        if self._calls % 512 == 0:
            self._prune()
        return frac

    def replace(self, old: Trade | None, new: Trade | None) -> None:
        """A fork correction from the coin's state machine: the version of an event already counted (`old`) wasn't
        the chain's - it kept `new`, or none (the transaction failed); old None reinstates a retracted event. The
        affected bags are replayed from their snapshots, so inventory, cost, realized profit, closed bags and wins
        all match a fresh replay of the corrected history. An event already folded or pruned away can't be: its
        history is marked unknown instead, and nothing is counted again. (Our own copied trades and their fees are
        real and never touched here.)"""
        t = old if old is not None else new
        if t is None:
            return
        ident = self._ident(t)
        loc = self.where.get(ident) if ident else None
        touched: set = set()
        if loc is not None and loc[0] not in self.pairs:     # (an index left behind: treat it as folded away)
            del self.where[ident]
            loc = None
            self.folded_ids[ident] = t.trader
        if loc is not None:
            key, seq = loc
            ev = next((e for e in self.pairs[key].events if e[0] == seq), None)
            if new is not None and ev is not None and (new.trader, new.mint) == key:
                ev[1], ev[2] = new, new.slot
            elif ev is not None:
                ev[1] = None                                  # retracted, or now another wallet's event
                if new is not None and new.trader in self.leaders:
                    touched.add(self._insert(new)[0])
            touched.add(key)
        elif ident and ident in self.folded_ids:             # folded or pruned away: can't be replayed exactly
            w = self.folded_ids[ident]
            pair = self.pairs.get((w, t.mint))
            if pair is not None:
                pair.forced = "a fork correction came for an event already folded away"
                touched.add((w, t.mint))
            else:
                self.base_unknown[w] += 1
                self._restat(w)
        elif old is not None and old.trader in self.leaders:  # counted, but no longer replayable
            if (old.trader, old.mint) in self.pairs:
                self.pairs[(old.trader, old.mint)].forced = "a fork correction came for an event it can't replay"
                touched.add((old.trader, old.mint))
            else:
                self.base_unknown[old.trader] += 1
                self._restat(old.trader)
        elif new is not None and new.trader in self.leaders:  # not counted before (another wallet's, or retracted)
            touched.add(self._insert(new)[0])
        for key in touched:
            self._rebuild(key)
        for w in {k[0] for k in touched}:
            self._restat(w)

    def unknown_if_counted(self, ident: tuple, why: str) -> None:
        """An event counted here whose correction can't be applied (its coin can't be rebuilt): mark its bag unknown."""
        loc = self.where.get(ident)
        if loc is not None and loc[0] in self.pairs:
            self.pairs[loc[0]].forced = why
            self._rebuild(loc[0])
            self._restat(loc[0][0])

    # ---- persistence across restarts (the engine's state file) --------------------------------------------------
    def to_json(self) -> dict:
        def tr(t):
            return asdict(t) if t is not None else None
        return {"seq": self._seq, "base": self.base, "base_unknown": dict(self.base_unknown),
                "folded_ids": [[i[0], i[1], w] for i, w in list(self.folded_ids.items())[-5000:]],
                "pairs": [{"wallet": k[0], "mint": k[1], "start": asdict(p.start), "start_n": p.start_n,
                           "start_unknown": p.start_unknown, "forced": p.forced, "last_ts": p.last_ts,
                           "checkpoint": p.checkpoint, "boundary": [asdict(t) for t in p.boundary],
                           "events": [[seq, tr(t), slot, list(ident) if ident else None]
                                      for seq, t, slot, ident in p.events]}
                          for k, p in self.pairs.items()]}

    def load_state(self, d: dict | None) -> None:
        """The observed ledger as saved with the engine's state: bags carry across a restart, and the observed totals
        come from here (leaders.json still holds our copied results)."""
        if not d:
            return
        self._seq = int(d.get("seq", 0))
        self.base = {w: list(v) for w, v in (d.get("base") or {}).items()}
        self.base_unknown = defaultdict(int, d.get("base_unknown") or {})
        for sig, ei, w in d.get("folded_ids") or []:
            self.folded_ids[(sig, int(ei))] = w
        fields = Trade.__dataclass_fields__
        for r in d.get("pairs") or []:
            key = (r["wallet"], r["mint"])
            p = self._pair(key)
            p.start, p.start_n = _Bag(**r["start"]), list(r["start_n"])
            p.start_unknown, p.forced, p.last_ts = r.get("start_unknown", ""), r.get("forced", ""), r.get("last_ts", 0.0)
            p.checkpoint = int(r.get("checkpoint", 0))
            p.boundary = [Trade(**{k: v for k, v in t.items() if k in fields}) for t in r.get("boundary") or []]
            for ev in r.get("events") or []:
                seq, t, slot = ev[:3]
                tt = Trade(**{k: v for k, v in t.items() if k in fields}) if t else None
                ident = tuple(ev[3]) if len(ev) > 3 and ev[3] else self._ident(tt)
                p.events.append([seq, tt, slot, ident])
                if ident:
                    self.where[ident] = (key, seq)
            self._rebuild(key)
            self._newest_ts = max(self._newest_ts, p.last_ts)
        for w in set(self.base) | set(self.by_wallet):
            self._restat(w)

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
