"""Sniper engine: one event loop that runs every agent on each feed event.

  Feed ─► Tracker (per-token state) ─► Gatekeeper (hard gates + score)  ─┐
  Leader wallets (copy trading) ─► Copy agent (chase/red-flag guards)    ─┼► Risk ─► AI desk ─► Executor ─► Book
  Signals (Telegram/X) ─► Caller book (learned weights) ─► score boost  ─┘          (optional)
  Positions ─► Exit agent (initials / trailing / decay / red flags / leader sold) ─► Executor

The fast path is deterministic. The AI desk (Claude persona agents) only votes on entries,
off the event loop, and is never between a red flag and a sell. All decisions use the
feed's clock, so recorded sessions replay deterministically in the backtester.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from ..journal import DATA, record
from .callouts import Callout, CalloutBook, compose, eligible, is_red_flag, post_telegram
from .copytrade import LeaderBook
from .curve import Curve
from .events import Event, Funding, Launch, Migration, Social, Tick, Trade, dumps
from .funding import FundingResolver, cluster_report, cohort
from .notify import Notifier
from .signals import CallerBook
from .sizing import SolPrice, size_usd, strength
from .strategy import SniperPosition, evaluate_entry, evaluate_exit
from .tracker import TokenState

DUST_SOL = 0.0005


def reason_key(note: str) -> str:
    """'dev bought 7.1% > 6%' -> 'dev bought' (for grouping reject stats)."""
    return re.split(r"[\d(]", note, maxsplit=1)[0].strip(" :-") or note


class Book:
    def __init__(self, start_sol: float):
        self.sol = start_sol
        self.start_sol = start_sol
        self.day = ""
        self.day_pnl = 0.0
        self.closed: list[dict] = []
        self.equity_hist: deque = deque(maxlen=2000)
        self.halted = ""


class Engine:
    def __init__(self, params, feed, executor, mode: str = "paper", record_path: Path | None = None,
                 log_to_journal: bool = True, desk=None, persist: bool | None = None):
        self.p = params.sniper
        self.feed = feed
        self.ex = executor
        self.mode = mode
        self.desk = desk
        self.fee = self.p.execution.curve_fee_pct + self.p.execution.platform_fee_pct
        self.tokens: dict[str, TokenState] = {}
        self.positions: dict[str, SniperPosition] = {}
        self.pending: set[str] = set()
        self.reviewing: set[str] = set()
        self.watch_until: dict[str, float] = {}
        self.book = Book(self.p.capital.starting_sol)
        self.creators: dict[str, deque] = defaultdict(deque)
        self.symbols: dict[str, deque] = defaultdict(deque)
        self.rejects: Counter = Counter()
        self.stats = Counter()
        self.log: deque = deque(maxlen=300)
        self.paused = False
        self.journal = log_to_journal
        self.callers = CallerBook(DATA / "callers.json")
        self.leaders = LeaderBook(self.p.copy.leaders, DATA / "leaders.json" if log_to_journal else None)
        self.record_file = record_path.open("a") if record_path else None
        self.now = 0.0
        self._last_tick = 0.0
        self._last_equity = 0.0
        self._last_summary = 0.0
        self.sol_price = SolPrice(self.p.sizing.sol_usd_fallback)
        self.callouts = CalloutBook()
        self.notifier = Notifier(list(self.p.notify.levels))
        f = self.p.entry.funding
        self.funders: dict[str, tuple[str, str]] = {}     # wallet -> (funder, funder_type)
        self.fanout: Counter = Counter()                   # funder -> wallets it funded (exchange detection)
        self.resolver = FundingResolver(f.backend, f.max_lookups_per_min)
        self.funding_started: dict[str, float] = {}
        # live money must survive restarts (the Ubuntu service restarts on crash)
        self.persist = mode.startswith("live") if persist is None else persist
        self.state_path = DATA / f"sniper_state_{mode.split('-')[0]}.json"
        self._last_state_save = 0.0
        self._load_funders()
        self._last_price_refresh = -1e12

    # ------------------------------------------------------------------ helpers
    def say(self, level: str, text: str, mint: str = "", **fields) -> None:
        self.log.append({"ts": self.now, "level": level, "text": text, "mint": mint})
        if self.feed.realtime:
            self.notifier.push(level, text, self.mode)
        if self.journal:
            record("sniper", level, text=text, mint=mint, **fields)

    def _mark(self, m: str, pos: SniperPosition) -> float:
        s = self.tokens.get(m)
        return s.curve.price if s and s.price_known else pos.entry_price

    def equity(self) -> float:
        return self.book.sol + sum(pos.tokens * self._mark(m, pos) for m, pos in self.positions.items())

    def entries_blocked(self, buy_sol: float | None = None) -> str:
        c = self.p.capital
        if self.book.halted:
            return "halted: " + self.book.halted
        if self.paused:
            return "paused"
        if getattr(self.feed, "degraded", False):
            return f"on backup feed {self.feed.host} (no trade data) - entries paused"
        if -self.book.day_pnl >= c.daily_loss_limit_sol:
            return "daily loss limit"
        trading = sum(1 for p in self.positions.values() if p.source != "callout")   # $1 callout bags don't count
        if trading + len(self.pending) + len(self.reviewing) >= c.max_open_positions:
            return "max positions"
        need = buy_sol or (self.p.sizing.max_usd / self.sol_price.usd if self.p.sizing.enabled else c.buy_sol)
        if self.book.sol - need < c.min_sol_reserve:
            return "low SOL"
        return ""

    def _ctx(self, s: TokenState) -> dict:
        ws = [self.leaders.weight(x.author) if x.source == "wallet" else self.callers.weight(f"{x.source}:{x.author}")
              for x in s.socials]
        return {"creator_launches": len(self.creators.get(s.creator, ())),
                "symbol_dupes": max(len(self.symbols.get(s.symbol.upper(), ())) - 1, 0),
                "social_weight": max(ws) if ws else 0.0}

    async def _watch(self, mint: str) -> None:
        self.watch_until[mint] = self.now + (self.p.record.full_window_s if self.record_file else 1e12)
        await self.feed.watch([mint])

    async def _unwatch(self, mint: str) -> None:
        # while recording, keep every launch's trades for the full window so backtests can
        # re-evaluate tokens that these params rejected
        if self.record_file and self.watch_until.get(mint, 0) > self.now:
            return
        self.watch_until.pop(mint, None)
        await self.feed.unwatch([mint])

    # ------------------------------------------------------------------ event loop
    async def run(self, stop_after_s: float | None = None) -> None:
        start = None
        if self.persist:
            await self.restore_state()
        ticker = asyncio.create_task(self._ticker()) if self.feed.realtime else None
        if self.p.copy.enabled and self.leaders.leaders:
            await self.feed.watch_accounts(list(self.leaders.leaders))
            self.say("info", f"copy trading {len(self.leaders.leaders)} wallet(s)")
        if self.desk and self.desk.enabled:
            self.say("info", f"AI desk on: {', '.join(self.p.desk.personas)} ({self.p.desk.model})")
        try:
            async for e in self.feed.events():
                await self.handle(e)
                start = start or self.now
                if stop_after_s and self.now - start >= stop_after_s:
                    break
        finally:
            if ticker:
                ticker.cancel()
            if self.record_file:
                self.record_file.close()
            self.leaders.save()
            self._save_funders()
            self.save_state()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.handle(Tick(self.feed.now()))

    async def handle(self, e: Event) -> None:
        self.now = max(self.now, e.ts)
        if self.record_file and not isinstance(e, Tick):
            self.record_file.write(dumps(e) + "\n")
        if isinstance(e, Launch):
            await self._on_launch(e)
        elif isinstance(e, Trade):
            await self._on_trade(e)
        elif isinstance(e, Migration):
            s = self.tokens.get(e.mint)
            if s:
                s.migrated = True
                await self._evaluate(s)
        elif isinstance(e, Social):
            await self._on_social(e)
        elif isinstance(e, Funding):
            if e.wallet not in self.funders and e.funder:
                self.fanout[e.funder] += 1
            self.funders[e.wallet] = (e.funder, e.funder_type)
        if self.now - self._last_tick >= 1:
            self._last_tick = self.now
            await self._tick()

    async def _on_launch(self, e: Launch) -> None:
        if e.mint in self.tokens:          # metadata update for a known launch
            s = self.tokens[e.mint]
            if s.launch:
                s.launch.twitter, s.launch.telegram, s.launch.website = e.twitter, e.telegram, e.website
            return
        self.stats["launches"] += 1
        self.creators[e.creator].append(e.ts)
        self.symbols[e.symbol.upper()].append(e.ts)
        s = TokenState(e.mint, e, e.ts)
        s.on_launch(e)
        self.tokens[e.mint] = s
        await self._watch(e.mint)
        if self.feed.realtime and e.uri and not (e.twitter or e.telegram or e.website):
            asyncio.create_task(self._enrich(e))

    async def _enrich(self, e: Launch) -> None:
        from .feeds import fetch_metadata

        md = await fetch_metadata(e.uri)
        if md:
            e.twitter, e.telegram, e.website = md.get("twitter", ""), md.get("telegram", ""), md.get("website", "")
            if self.record_file:
                self.record_file.write(dumps(e) + "\n")

    async def _on_trade(self, e: Trade) -> None:
        s = self.tokens.get(e.mint)
        lead = e.trader in self.leaders.leaders and self.leaders.is_leader(e.trader)
        if s is None:
            if not lead:
                return
            s = self.tokens[e.mint] = TokenState(e.mint, None, e.ts)   # token we only know via a leader
            if e.v_sol and e.v_tokens:
                s.curve = Curve(e.v_sol, e.v_tokens)
            await self._watch(e.mint)
        s.on_trade(e, self.p.entry.bundle_window_s, self.p.entry.sniper_window_s)
        if lead:
            await self._on_leader_trade(s, e)
        await self._evaluate(s)

    async def _on_social(self, e: Social) -> None:
        self.stats["signals"] += 1
        s = self.tokens.get(e.mint)
        if s is None and self.p.signals.social_only_watch:
            s = self.tokens[e.mint] = TokenState(e.mint, None, e.ts)
            await self._watch(e.mint)
        if s is None:
            return
        s.socials.append(e)
        self.callers.on_call(f"{e.source}:{e.author}", e.mint, e.ts, s.curve.price)
        self.say("signal", f"{e.source} @{e.author} called {s.symbol}", e.mint)
        await self._evaluate(s)

    # ------------------------------------------------------------------ copy trading
    async def _on_leader_trade(self, s: TokenState, e: Trade) -> None:
        c = self.p.copy
        lead = self.leaders.leaders[e.trader]
        label = self.leaders.label(e.trader)
        frac = self.leaders.on_trade(e)
        if e.side == "sell":
            pos = self.positions.get(e.mint)
            if pos and pos.leader == e.trader and c.follow_sells and e.mint not in self.pending:
                all_out = frac * 100 >= c.sell_all_when_leader_sold_pct
                await self._sell(s, pos, 1.0 if all_out else frac, f"leader {label} sold {frac:.0%}")
            return
        self.say("signal", f"wallet {label} bought {s.symbol} for {e.sol:.2f} SOL", e.mint)
        if lead.mode == "signal":
            s.socials.append(Social(e.mint, e.ts, "wallet", e.trader, f"{label} bought {e.sol:.2f} SOL"))
            return
        if not c.enabled or e.mint in self.positions or e.mint in self.pending or e.mint in self.reviewing:
            return
        why = self._copy_blocked(s, e)
        if why:
            self.stats["copy_skipped"] += 1
            if not why.startswith(("leader copy limit", "max positions", "paused", "halted", "low SOL",
                                   "daily loss", "on backup feed")):   # capacity limits aren't coin rejections
                self.rejects["copy: " + reason_key(why)] += 1
            if not why.startswith(("leader copy limit", "max positions", "paused", "halted")):   # don't flood the log
                self.say("info", f"skip copy of {label} on {s.symbol}: {why}", e.mint)
            return
        self.leaders.note_copy(e.trader, self.now)
        await self._enter(s, kind="copy", score=self.leaders.weight(e.trader) * 100, buy_sol=c.buy_sol,
                          notes=[f"copy {label}", f"leader {e.sol:.2f} SOL"], source=f"copy:{label}", leader=e.trader,
                          ref_price=e.sol / e.tokens if e.tokens else s.curve.price)

    def _copy_blocked(self, s: TokenState, e: Trade) -> str:
        c, en = self.p.copy, self.p.entry
        blocked = self.entries_blocked(c.buy_sol)
        if blocked:
            return blocked
        if e.pool != "pump":
            return f"not on bonding curve ({e.pool})"
        if e.sol < c.min_leader_buy_sol:
            return f"leader buy {e.sol:.2f} < {c.min_leader_buy_sol} SOL"
        if self.leaders.copies_last_hour(e.trader, self.now) >= c.max_copies_per_leader_per_hour:
            return "leader copy limit/h"
        if s.curve.progress * 100 > c.max_curve_progress_pct:
            return f"curve {s.curve.progress:.0%} too late"
        leader_px = e.sol / e.tokens if e.tokens else 0
        if leader_px and (s.curve.price / leader_px - 1) * 100 > c.max_chase_pct:
            return f"price ran {(s.curve.price / leader_px - 1) * 100:.0f}% past leader"
        if c.apply_red_flags:
            if s.dev_sold > 0:
                return "dev sold"
            if s.bundle_pct() > en.max_bundle_pct:
                return f"bundle {s.bundle_pct():.0f}%"
            if len(self.creators.get(s.creator, ())) > en.max_creator_launches_24h:
                return "serial deployer"
            if s.cluster and s.cluster["pct"] > en.funding.max_cluster_pct:
                return f"insider cluster {s.cluster['pct']:.0f}%"
        return ""

    # ------------------------------------------------------------------ decisions
    async def _evaluate(self, s: TokenState) -> None:
        if s.mint in self.pending or s.mint in self.reviewing:
            return
        if s.mint in self.positions:
            await self._check_exit(s)
        elif not s.decided:
            await self._check_entry(s)

    async def _check_entry(self, s: TokenState) -> None:
        d = evaluate_entry(s, self.now, self.p.entry, self._ctx(s))
        s.score, s.score_notes = d.score, d.notes or []
        if d.action == "reject":
            s.decided = "rejected: " + d.notes[0]
            self.rejects[reason_key(d.notes[0])] += 1
            keep = self.p.callouts.enabled and not is_red_flag(d.notes[0])   # may still be worth a callout later
            if s.mint not in self.positions and not keep:
                await self._unwatch(s.mint)
            return
        if d.action != "enter":
            return
        blocked = self.entries_blocked()
        if blocked:
            self.stats["skipped_" + blocked.split(":")[0].replace(" ", "_")] += 1
            return
        verdict = await self._funding_gate(s)
        if verdict == "wait":
            return
        if verdict:
            s.decided = "rejected: " + verdict
            self.rejects[reason_key(verdict)] += 1
            self.say("info", f"{s.symbol} rejected: {verdict}", s.mint)
            return
        await self._enter(s, kind="sniper", score=d.score, buy_sol=self.p.capital.buy_sol, notes=d.notes or [])

    async def _funding_gate(self, s: TokenState) -> str:
        """'' = pass, 'wait' = lookups in flight, otherwise a rejection reason."""
        f = self.p.entry.funding
        if not f.enabled:
            return ""
        wallets = cohort(s, f.cohort_size)
        missing = [w for w in wallets + ([s.creator] if s.creator else []) if w not in self.funders]
        if missing and self.resolver.available and self.feed.realtime:
            started = self.funding_started.get(s.mint)
            if started is None:
                self.funding_started[s.mint] = self.now
                asyncio.create_task(self._resolve_funders(missing))
                return "wait"
            if self.now - started < f.max_wait_s:
                return "wait"
        rep = cluster_report(s, wallets, self.funders, self.fanout, f.exchange_fanout)
        s.cluster = rep
        if rep["pct"] > f.max_cluster_pct:
            return f"insider cluster {rep['pct']:.0f}% ({rep['linked']} linked wallets)"
        return ""

    def _load_funders(self) -> None:
        path = DATA / "funders.json"
        if self.journal and path.exists():
            try:
                d = json.loads(path.read_text())
                self.funders = {w: tuple(v) for w, v in d.get("funders", {}).items()}
                self.fanout = Counter(d.get("fanout", {}))
            except (ValueError, OSError):
                pass

    def _save_funders(self) -> None:
        if not self.journal:
            return
        DATA.mkdir(exist_ok=True)
        recent = dict(list(self.funders.items())[-50_000:])
        (DATA / "funders.json").write_text(json.dumps({"funders": recent, "fanout": dict(self.fanout.most_common(20_000))}))

    async def _cluster_watch(self, s: TokenState) -> str:
        """While holding: re-check the funding graph every few seconds, since insiders often keep
        accumulating after we're in. Returns an exit reason or ''."""
        f = self.p.entry.funding
        if not f.enabled or self.now - s.last_cluster_check < 5:
            return ""
        s.last_cluster_check = self.now
        wallets = cohort(s, f.cohort_size)
        missing = [w for w in wallets if w not in self.funders]
        if missing and self.resolver.available and self.feed.realtime:
            asyncio.create_task(self._resolve_funders(missing[:10]))
        rep = cluster_report(s, wallets, self.funders, self.fanout, f.exchange_fanout)
        s.cluster = rep
        if rep["pct"] > f.max_cluster_pct_hold:
            return f"insider cluster grew to {rep['pct']:.0f}% ({rep['linked']} linked wallets)"
        return ""

    async def _resolve_funders(self, wallets: list[str]) -> None:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            results = await asyncio.gather(*(self.resolver.lookup(session, w) for w in wallets))
        for r in results:
            if r is not None:
                await self.handle(r)              # cached + recorded so backtests replay the same graph

    def _size(self, s: TokenState, kind: str, score: float, buy_sol: float, desk_mult: float | None,
              notes: list[str]) -> float:
        """SOL to spend. With sizing on: $base..$max from signal strength, hard-capped in USD."""
        z = self.p.sizing
        if not z.enabled:
            return round(buy_sol * (desk_mult or 1.0), 4)
        w = s.window(self.now, self.p.entry.flow_window_s)
        nb, ns = sum(1 for t in w if t[2] == "buy"), sum(1 for t in w if t[2] == "sell")
        smart = len({x.author for x in s.socials if x.source == "wallet"}) + (1 if kind == "copy" else 0)
        st, _ = strength(score, self.p.entry.min_score if kind == "sniper" else 0, nb / max(ns, 1),
                         s.curve.price / s.peak_price if s.peak_price else 1.0, smart, desk_mult)
        usd, why = size_usd(z, st, kind, s.curve.real_sol, self.sol_price.usd)
        notes.append(f"${usd:.0f} ({why})")
        return round(usd / self.sol_price.usd, 4)

    async def _enter(self, s: TokenState, kind: str, score: float, buy_sol: float, notes: list[str],
                     source: str = "sniper", leader: str = "", ref_price: float = 0.0) -> None:
        if self.desk and self.desk.enabled:
            self.reviewing.add(s.mint)
            review = self._desk_then_buy(s, kind, score, buy_sol, notes, source, leader, ref_price)
            if self.feed.realtime:      # live: deliberate off the event loop so feed + exits keep running
                asyncio.create_task(review)
            else:                       # backtest: inline, so replays stay deterministic
                await review
            return
        await self._buy(s, score, self._size(s, kind, score, buy_sol, None, notes), notes, source, leader)

    async def _desk_then_buy(self, s, kind, score, buy_sol, notes, source, leader, ref_price) -> None:
        from .desk import snapshot_for

        start_price = s.curve.price
        extra = {}
        if leader:
            st = self.leaders.stats[leader]
            extra = {"leader": {"label": self.leaders.label(leader), "copied_trades": st.copied,
                                "copied_pnl_sol": round(st.copied_pnl, 3), "observed_closed": st.closed,
                                "observed_realized_sol": round(st.realized_sol, 2)}}
        try:
            v = await self.desk.review(snapshot_for(s, self.now, kind, extra))
        finally:
            self.reviewing.discard(s.mint)
        s.desk = v.summary
        self.say("desk", f"{s.symbol}: {v.summary}", s.mint, votes=[vars(x) for x in v.votes])
        if not v.approve:
            self.rejects["desk passed"] += 1
            if kind == "sniper":
                s.decided = "rejected: desk passed"
            return
        moved = (s.curve.price / start_price - 1) * 100 if start_price else 0
        notes = notes + [f"desk x{v.size_mult:.2f}"]
        size = self._size(s, kind, score, buy_sol, v.size_mult, notes)
        why = self.entries_blocked(size) or ("already held" if s.mint in self.positions else "") or \
            (f"price moved {moved:+.0f}% during review" if moved > self.p.desk.max_price_move_pct else "") or \
            ("dev sold" if s.dev_sold else "")
        if why:
            self.say("info", f"desk approved {s.symbol} but skipped: {why}", s.mint)
            return
        await self._buy(s, score, size, notes, source, leader)

    async def _buy(self, s: TokenState, score: float, sol: float, notes: list[str], source: str = "sniper",
                   leader: str = "") -> None:
        z = self.p.sizing
        if z.enabled and sol * self.sol_price.usd > z.max_usd + 1e-6:     # hard cap, whatever asked for more
            self.say("error", f"size ${sol * self.sol_price.usd:.2f} over hard cap ${z.max_usd} - clamped", s.mint)
            sol = round(z.max_usd / self.sol_price.usd, 4)
        self.pending.add(s.mint)
        try:
            fill = await self.ex.buy(s.mint, s.curve, sol)
        finally:
            self.pending.discard(s.mint)
        s.decided = "entered"
        if not fill.ok:
            self.say("error", f"buy {s.symbol} failed: {fill.error}", s.mint)
            return
        self.book.sol -= fill.sol
        self.positions[s.mint] = SniperPosition(
            mint=s.mint, symbol=s.symbol, opened_at=self.now, entry_price=fill.price, tokens=fill.tokens,
            initial_tokens=fill.tokens, cost_sol=fill.sol, initial_cost_sol=fill.sol, score=score,
            peak_price=fill.price, exits=[], source=source, leader=leader, desk=getattr(s, "desk", ""))
        self.stats["entries"] += 1
        self.save_state()
        self.stats["entries_" + source.split(":")[0]] += 1
        self.say("buy", f"{s.symbol} {fill.sol:.3f} SOL @ curve {s.curve.progress:.0%} [{source}] | score {score:.0f} | "
                        + ", ".join(notes), s.mint, signature=fill.signature)

    async def _check_exit(self, s: TokenState) -> None:
        pos = self.positions[s.mint]
        if not s.price_known:          # restored after a restart: wait for a real price before any exit logic
            return
        if pos.source == "callout":         # hold the $1 callout bag; never trade it against followers
            pos.peak_price = max(pos.peak_price, s.curve.price)
            held = self.now - pos.opened_at
            r = (1.0, "dev sold") if s.dev_sold else \
                ((1.0, "callout hold done") if held >= self.p.callouts.hold_s else None)
        elif (why := await self._cluster_watch(s)):
            r = (1.0, why)
        elif pos.leader and not self.p.copy.use_own_exits:
            r = None
            if s.dev_sold and self.p.exit.exit_on_dev_sell:
                r = (1.0, "dev sold")
        else:
            r = evaluate_exit(pos, s, self.now, self.p.exit, self.fee)
        if r:
            await self._sell(s, pos, r[0], r[1])

    async def _sell(self, s: TokenState, pos: SniperPosition, frac: float, reason: str) -> None:
        tokens = pos.tokens if frac >= 1 else pos.tokens * frac
        self.pending.add(s.mint)
        try:
            fill = await self.ex.sell(s.mint, s.curve, tokens)
        finally:
            self.pending.discard(s.mint)
        if not fill.ok:
            self.say("error", f"sell {s.symbol} failed: {fill.error}", s.mint)
            return
        cost_part = pos.cost_sol * fill.tokens / pos.tokens if pos.tokens else 0
        pos.tokens -= fill.tokens
        pos.cost_sol -= cost_part
        pos.proceeds_sol += fill.sol
        pos.exits.append((self.now, reason, fill.tokens, fill.sol))
        self.book.sol += fill.sol
        self.book.day_pnl += fill.sol - cost_part
        if reason.startswith("initials"):
            pos.initials_taken = True
        elif reason.startswith("ladder ") and "x sell" in reason:
            pos.ladder_hit += 1
            pos.initials_taken = True
        self.say("sell", f"{s.symbol} {fill.tokens / pos.initial_tokens:.0%} for {fill.sol:.3f} SOL | {reason}",
                 s.mint, signature=fill.signature)
        if pos.tokens * s.curve.price < DUST_SOL:
            self._close(pos, s)
        self.save_state()

    def _close(self, pos: SniperPosition, s: TokenState) -> None:
        del self.positions[pos.mint]
        pnl = pos.proceeds_sol - pos.initial_cost_sol
        row = {
            "mint": pos.mint, "symbol": pos.symbol, "opened": pos.opened_at, "closed": self.now,
            "cost": pos.initial_cost_sol, "proceeds": pos.proceeds_sol, "pnl": pnl,
            "pnl_pct": pnl / pos.initial_cost_sol * 100, "peak_gain_pct": pos.gain_pct(pos.peak_price),
            "score": pos.score, "initials": pos.initials_taken, "exit": pos.exits[-1][1] if pos.exits else "",
            "source": pos.source, "desk": pos.desk,
        }
        self.book.closed.append(row)
        self.stats["wins" if pnl > 0 else "losses"] += 1
        self.say("close", f"{pos.symbol} {'+' if pnl >= 0 else ''}{pnl:.3f} SOL ({pnl / pos.initial_cost_sol:+.0%}) "
                          f"[{pos.source}]", pos.mint)
        if self.journal:
            DATA.mkdir(exist_ok=True)
            with (DATA / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(self.now))}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
        if pos.leader:
            paused = self.leaders.on_copy_closed(pos.leader, pnl, self.p.copy.pause_after_losses)
            if paused:
                self.say("error", f"paused copying {self.leaders.label(pos.leader)}: {paused}")
        asyncio.ensure_future(self._unwatch(pos.mint))

    async def _tick(self) -> None:
        if self.persist and self.now - self._last_state_save >= 10:
            self.save_state()
        if self.feed.realtime and self.p.sizing.enabled and self.now - self._last_price_refresh >= 300:
            self._last_price_refresh = self.now
            asyncio.create_task(self.sol_price.refresh())
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now))
        if day != self.book.day:
            self.book.day, self.book.day_pnl = day, 0.0
        eq = self.equity()
        if self.now - self._last_equity >= 5:
            self._last_equity = self.now
            self.book.equity_hist.append((self.now, eq))
        dd = (1 - eq / self.book.start_sol) * 100
        if not self.book.halted and dd >= self.p.capital.max_drawdown_pct:
            self.book.halted = f"drawdown {dd:.0f}%"
            self.say("error", f"KILL SWITCH: {self.book.halted} - selling everything")
        for mint in list(self.tokens):
            s = self.tokens[mint]
            if mint in self.pending or mint in self.reviewing:
                continue
            if mint in self.positions:
                if self.book.halted:
                    await self._sell(s, self.positions[mint], 1.0, "kill switch")
                else:
                    await self._check_exit(s)
            elif not s.decided:
                await self._check_entry(s)
            elif s.age(self.now) > max(self.p.entry.max_age_s,
                                       self.p.callouts.max_age_s if self.p.callouts.enabled else 0) + 60:
                del self.tokens[mint]
                if not self.record_file:
                    await self._unwatch(mint)
        await self._maybe_callout()
        self.callouts.settle(self.now, lambda m: self.tokens[m].curve.price if m in self.tokens else None)
        for mint, until in list(self.watch_until.items()):
            if until <= self.now and mint not in self.positions and (mint not in self.tokens or self.tokens[mint].decided):
                await self._unwatch(mint)
        for book, horizon in ((self.creators, 86400), (self.symbols, 3600)):
            for k in list(book):
                q = book[k]
                while q and q[0] < self.now - horizon:
                    q.popleft()
                if not q:
                    del book[k]
        self.callers.settle(self.now, lambda m: self.tokens[m].curve.price if m in self.tokens else None)
        if self.journal and self.now - self._last_summary >= 60:
            self._last_summary = self.now
            DATA.mkdir(exist_ok=True)
            self._save_funders()
            (DATA / "sniper_summary.json").write_text(json.dumps(
                {"ts": self.now, "summary": self.summary(), "rejects": dict(self.rejects),
                 "leaders": self.leaders.snapshot(), "desk": self.desk_stats()}, default=str, indent=1))

    # ------------------------------------------------------------------ persistence (live)
    def save_state(self) -> None:
        if not self.persist:
            return
        from dataclasses import asdict

        self._last_state_save = self.now
        b = self.book
        state = {"saved_at": time.time(), "mode": self.mode,
                 "book": {"sol": b.sol, "start_sol": b.start_sol, "day": b.day, "day_pnl": b.day_pnl,
                          "halted": b.halted, "closed": b.closed[-500:]},
                 "positions": {m: asdict(p) for m, p in self.positions.items()},
                 "called": sorted(self.callouts.called)[-2000:]}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str))
        tmp.replace(self.state_path)          # atomic: a crash mid-write never corrupts the state

    async def restore_state(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text())
        b = d["book"]
        self.book.sol, self.book.start_sol, self.book.day = b["sol"], b["start_sol"], b["day"]
        self.book.day_pnl, self.book.halted, self.book.closed = b["day_pnl"], b["halted"], b["closed"]
        self.callouts.called.update(d.get("called", []))
        now = self.feed.now()
        for m, pd in d["positions"].items():
            pd["exits"] = [tuple(x) for x in pd.get("exits") or []]
            self.positions[m] = SniperPosition(**pd)
            s = self.tokens[m] = TokenState(m, None, now)       # price unknown until its next trade
            s.decided = "entered"
            await self._watch(m)
        self.say("info", f"restored {len(self.positions)} open position(s) from {self.state_path.name}")
        await self.reconcile()

    async def reconcile(self) -> None:
        """Live: trust the wallet. Positions sold while we were down are closed, changed balances fixed,
        and pump tokens in the wallet we don't know about are reported."""
        wallet = getattr(self.ex, "wallet", None)
        if wallet is None:
            return
        try:
            balances = await asyncio.to_thread(wallet.all_token_balances)
        except Exception as e:
            self.say("error", f"wallet reconcile failed ({e}); positions kept as saved")
            return
        for m, pos in list(self.positions.items()):
            have = balances.get(m, 0) / 1e6
            if have <= 0:
                pos.exits.append((self.now, "gone from wallet while offline", pos.tokens, 0.0))
                pos.tokens = 0
                self._close(pos, self.tokens[m])
                self.say("error", f"{pos.symbol}: not in wallet any more (sold/moved while offline) - closed at 0")
            elif abs(have - pos.tokens) / max(pos.tokens, 1e-9) > 0.01:
                self.say("info", f"{pos.symbol}: wallet holds {have:,.0f} tokens, saved {pos.tokens:,.0f} - corrected")
                pos.tokens = have
        orphans = [m for m, raw in balances.items() if m.endswith("pump") and m not in self.positions and raw > 0]
        if orphans:
            self.say("error", f"{len(orphans)} pump token(s) in the wallet aren't tracked (sell manually): "
                              + ", ".join(o[:6] + "…" for o in orphans[:8]))
        self.save_state()

    # ------------------------------------------------------------------ callouts
    async def _maybe_callout(self) -> None:
        c = self.p.callouts
        if not c.enabled or self.book.halted or self.paused or self.now - self.callouts.last_ts < c.interval_s:
            return
        today = [x for x in self.callouts.calls if self.now - x.ts < 86400]
        if len(today) >= c.max_per_day:
            return
        open_bags = sum(1 for p in self.positions.values() if p.source == "callout")
        best = None
        for s in self.tokens.values():
            if s.mint in self.callouts.called or s.mint in self.pending or s.mint in self.reviewing:
                continue
            if not s.decided:       # the sniper decides first - a $1 bag must never block a real entry
                continue
            ok, score, _ = eligible(s, self.now, c, {
                "max_bundle_pct": self.p.entry.max_bundle_pct, "max_early_sold_ratio": self.p.entry.max_early_sold_ratio,
                "creator_launches": len(self.creators.get(s.creator, ())),
                "max_creator_launches_24h": self.p.entry.max_creator_launches_24h,
                "max_cluster_pct": self.p.entry.funding.max_cluster_pct})
            if ok and (best is None or score > best[1]):
                best = (s, score)
        if not best:
            return
        s, score = best
        usd = self.sol_price.usd
        held = self.positions.get(s.mint)
        if not held or held.tokens * s.curve.price * usd < c.min_hold_usd:
            if open_bags >= c.max_open_bags or self.book.sol - c.position_usd / usd < self.p.capital.min_sol_reserve:
                return
            await self._buy(s, score, round(c.position_usd / usd, 5), [f"callout bag ${c.position_usd}"], "callout")
            if s.mint not in self.positions:
                return
        text = compose(s, self.now, usd)
        call = Callout(s.mint, s.symbol, self.now, s.curve.market_cap_sol, s.curve.price, text, score)
        self.callouts.add(call)
        self.stats["callouts"] += 1
        self.say("callout", text, s.mint)
        if c.auto_post == "telegram" and self.feed.realtime:
            async def post():
                call.posted = "telegram" if await post_telegram(text, s.mint) else ""
            asyncio.create_task(post())

    def mark_posted(self, mint: str) -> None:
        for c in self.callouts.calls:
            if c.mint == mint:
                c.posted = c.posted or "manual"

    # ------------------------------------------------------------------ controls (UI)
    async def sell_now(self, mint: str) -> None:
        if mint in self.positions and mint in self.tokens:
            await self._sell(self.tokens[mint], self.positions[mint], 1.0, "manual sell")

    async def kill(self) -> None:
        self.book.halted = self.book.halted or "manual kill switch"
        for mint in list(self.positions):
            await self.sell_now(mint)

    # ------------------------------------------------------------------ reporting
    @staticmethod
    def _stats(closed: list[dict]) -> dict:
        wins = [c for c in closed if c["pnl"] > 0]
        losses = [c for c in closed if c["pnl"] <= 0]
        gross_win = sum(c["pnl"] for c in wins)
        gross_loss = -sum(c["pnl"] for c in losses)
        return {
            "closed": len(closed), "win_rate": len(wins) / len(closed) if closed else 0.0,
            "avg_win_pct": sum(c["pnl_pct"] for c in wins) / len(wins) if wins else 0.0,
            "avg_loss_pct": sum(c["pnl_pct"] for c in losses) / len(losses) if losses else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
            "realized_pnl_sol": sum(c["pnl"] for c in closed),
            "initials_hit": sum(1 for c in closed if c["initials"]),
            "best_pct": max((c["pnl_pct"] for c in closed), default=0.0),
            "worst_pct": min((c["pnl_pct"] for c in closed), default=0.0),
        }

    def summary(self) -> dict:
        closed = self.book.closed
        out = self._stats(closed)
        out.update({"launches": self.stats["launches"], "entries": self.stats["entries"],
                    "equity_sol": self.equity(), "start_sol": self.book.start_sol,
                    "by_source": {src: self._stats([c for c in closed if c["source"].split(":")[0] == src])
                                  for src in sorted({c["source"].split(":")[0] for c in closed})}})
        return out

    def desk_stats(self) -> dict:
        d = self.desk
        return {"enabled": bool(d and d.enabled), "calls": d.calls if d else 0,
                "cost_usd": round(d.cost_usd(), 4) if d else 0.0,
                "model": self.p.desk.model, "personas": list(self.p.desk.personas)}

    def snapshot(self) -> dict:
        watching = []
        for s in self.tokens.values():
            if s.mint in self.positions:
                continue
            status = "AI desk reviewing" if s.mint in self.reviewing else (s.decided or "watching")
            watching.append({
                "mint": s.mint, "symbol": s.symbol, "age": round(s.age(self.now)), "progress": s.curve.progress,
                "mcap_sol": s.curve.market_cap_sol, "buyers": len(s.buyers), "score": s.score,
                "notes": s.score_notes[:3], "status": status, "socials": len(s.socials),
                "bundle_pct": s.bundle_pct(), "dev_pct": s.dev_initial_pct(), "spark": s.sparkline(40),
            })
        watching.sort(key=lambda w: (w["status"] not in ("watching", "AI desk reviewing"), -w["score"], w["age"]))
        positions, bags = [], []
        for m, pos in self.positions.items():
            s = self.tokens.get(m)
            if not s:
                continue
            if pos.source == "callout":       # $1 callout bags are listed with the callouts, not as trades
                bags.append({"mint": m, "symbol": pos.symbol, "value_usd": pos.tokens * s.curve.price * self.sol_price.usd,
                             "gain_pct": pos.gain_pct(s.curve.price), "held_s": round(self.now - pos.opened_at)})
                continue
            price = self._mark(m, pos)
            positions.append({
                "mint": m, "symbol": pos.symbol, "held_s": round(self.now - pos.opened_at), "entry": pos.entry_price,
                "price": price, "gain_pct": pos.gain_pct(price), "peak_gain_pct": pos.gain_pct(pos.peak_price),
                "value_sol": pos.tokens * price, "cost_sol": pos.initial_cost_sol, "proceeds_sol": pos.proceeds_sol,
                "initials": pos.initials_taken, "progress": s.curve.progress, "score": pos.score,
                "source": pos.source, "desk": pos.desk,
                "spark": [p for t, p, *_ in s.trades if t >= pos.opened_at - 30][-120:],
                "entry_idx": sum(1 for t, *_ in s.trades if pos.opened_at - 30 <= t < pos.opened_at),
            })
        return {
            "now": self.now, "mode": self.mode, "paused": self.paused, "halted": self.book.halted,
            "sol": self.book.sol, "equity": self.equity(), "day_pnl": self.book.day_pnl,
            "summary": self.summary(), "positions": positions, "watching": watching[:40],
            "closed": self.book.closed[-50:][::-1], "rejects": self.rejects.most_common(14),
            "equity_hist": list(self.book.equity_hist)[-400:], "log": list(self.log)[-80:][::-1],
            "callers": sorted(({"caller": k, "calls": v.calls, "avg_return": v.avg_return, "weight": v.weight}
                               for k, v in self.callers.stats.items()), key=lambda c: -c["calls"])[:15],
            "leaders": self.leaders.snapshot(), "desk": self.desk_stats(),
            "sol_usd": self.sol_price.usd, "sol_usd_source": self.sol_price.source,
            "callouts": [{"mint": c.mint, "symbol": c.symbol, "ts": c.ts, "mcap_usd": c.mcap_sol * self.sol_price.usd,
                          "text": c.text, "score": c.score, "posted": c.posted, "outcomes": c.outcomes}
                         for c in self.callouts.calls[-20:][::-1]],
            "callout_stats": self.callouts.stats(), "callout_bags": bags,
            "callout_next_in": max(0, round(self.p.callouts.interval_s - (self.now - self.callouts.last_ts)))
            if self.p.callouts.enabled else None,
            "blocked": self.entries_blocked(),
        }

    def save_report(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"summary": self.summary(), "rejects": dict(self.rejects),
                                    "closed": self.book.closed, "params": dict(self.p)}, indent=1, default=str))
