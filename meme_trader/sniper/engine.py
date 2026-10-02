"""Sniper engine: one event loop that runs the agents on every feed event.

  Feed ─► Tracker (per-token state) ─► Entry agent (gates + score) ─► Risk (capital/limits)
                                                                       └► Executor ─► Book
          Positions ─► Exit agent (initials / trailing / decay / red flags) ─► Executor
  Signals (Telegram/X) ─► Caller book ─► score boost / watch new CAs

All decisions use the feed's clock, so a recorded session replays deterministically
(that is what the backtester does).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from ..journal import DATA, record
from .events import Event, Launch, Migration, Social, Tick, Trade, dumps
from .signals import CallerBook
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
                 log_to_journal: bool = True):
        self.p = params.sniper
        self.feed = feed
        self.ex = executor
        self.mode = mode
        self.fee = self.p.execution.curve_fee_pct + self.p.execution.platform_fee_pct
        self.tokens: dict[str, TokenState] = {}
        self.positions: dict[str, SniperPosition] = {}
        self.pending: set[str] = set()
        self.book = Book(self.p.capital.starting_sol)
        self.creators: dict[str, deque] = defaultdict(deque)
        self.symbols: dict[str, deque] = defaultdict(deque)
        self.rejects: Counter = Counter()
        self.stats = Counter()
        self.log: deque = deque(maxlen=300)
        self.paused = False
        self.callers = CallerBook(DATA / "callers.json")
        self.record_file = record_path.open("a") if record_path else None
        self.journal = log_to_journal
        self.now = 0.0
        self._last_tick = 0.0
        self._last_equity = 0.0

    # ------------------------------------------------------------------ helpers
    def say(self, level: str, text: str, mint: str = "", **fields) -> None:
        self.log.append({"ts": self.now, "level": level, "text": text, "mint": mint})
        if self.journal:
            record("sniper", level, text=text, mint=mint, **fields)

    def equity(self) -> float:
        return self.book.sol + sum(pos.tokens * self.tokens[m].curve.price for m, pos in self.positions.items()
                                   if m in self.tokens)

    def entries_blocked(self) -> str:
        c = self.p.capital
        if self.book.halted:
            return "halted: " + self.book.halted
        if self.paused:
            return "paused"
        if -self.book.day_pnl >= c.daily_loss_limit_sol:
            return "daily loss limit"
        if len(self.positions) + len(self.pending) >= c.max_open_positions:
            return "max positions"
        if self.book.sol - c.buy_sol < c.min_sol_reserve:
            return "low SOL"
        return ""

    def _ctx(self, s: TokenState) -> dict:
        ws = [self.callers.weight(x.author) for x in s.socials]
        return {"creator_launches": len(self.creators.get(s.creator, ())),
                "symbol_dupes": max(len(self.symbols.get(s.symbol.upper(), ())) - 1, 0),
                "social_weight": max(ws) if ws else 0.0}

    # ------------------------------------------------------------------ event loop
    async def run(self, stop_after_s: float | None = None) -> None:
        start = None
        ticker = asyncio.create_task(self._ticker()) if self.feed.realtime else None
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
            s = self.tokens.get(e.mint)
            if s is None:
                return
            s.on_trade(e, self.p.entry.bundle_window_s)
            await self._evaluate(s)
        elif isinstance(e, Migration):
            s = self.tokens.get(e.mint)
            if s:
                s.migrated = True
                await self._evaluate(s)
        elif isinstance(e, Social):
            await self._on_social(e)
        # time-driven checks once per second of feed time
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
        await self.feed.watch([e.mint])
        if self.feed.realtime and e.uri and not (e.twitter or e.telegram or e.website):
            asyncio.create_task(self._enrich(e))

    async def _enrich(self, e: Launch) -> None:
        from .feeds import fetch_metadata

        md = await fetch_metadata(e.uri)
        if md:
            e.twitter, e.telegram, e.website = md.get("twitter", ""), md.get("telegram", ""), md.get("website", "")
            if self.record_file:
                self.record_file.write(dumps(e) + "\n")

    async def _on_social(self, e: Social) -> None:
        self.stats["signals"] += 1
        s = self.tokens.get(e.mint)
        if s is None and self.p.signals.social_only_watch:
            s = self.tokens[e.mint] = TokenState(e.mint, None, e.ts)
            await self.feed.watch([e.mint])
        if s is None:
            return
        s.socials.append(e)
        self.callers.on_call(f"{e.source}:{e.author}", e.mint, e.ts, s.curve.price)
        self.say("signal", f"{e.source} @{e.author} called {s.symbol}", e.mint)
        await self._evaluate(s)

    # ------------------------------------------------------------------ decisions
    async def _evaluate(self, s: TokenState) -> None:
        if s.mint in self.pending:
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
            await self.feed.unwatch([s.mint])
            return
        if d.action != "enter":
            return
        blocked = self.entries_blocked()
        if blocked:
            self.stats["skipped_" + blocked.split(":")[0].replace(" ", "_")] += 1
            return
        await self._buy(s, d.score, d.notes)

    async def _buy(self, s: TokenState, score: float, notes: list[str]) -> None:
        sol = self.p.capital.buy_sol
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
            peak_price=fill.price, exits=[])
        self.stats["entries"] += 1
        self.say("buy", f"{s.symbol} {fill.sol:.3f} SOL @ curve {s.curve.progress:.0%} | score {score:.0f} | "
                        + ", ".join(notes), s.mint, signature=fill.signature)

    async def _check_exit(self, s: TokenState) -> None:
        pos = self.positions[s.mint]
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
        pnl = fill.sol - cost_part
        self.book.day_pnl += pnl
        if reason.startswith("initials"):
            pos.initials_taken = True
        self.say("sell", f"{s.symbol} {fill.tokens / pos.initial_tokens:.0%} for {fill.sol:.3f} SOL | {reason}",
                 s.mint, signature=fill.signature)
        if pos.tokens * s.curve.price < DUST_SOL:
            self._close(pos, s)

    def _close(self, pos: SniperPosition, s: TokenState) -> None:
        del self.positions[pos.mint]
        pnl = pos.proceeds_sol - pos.initial_cost_sol
        self.book.closed.append({
            "mint": pos.mint, "symbol": pos.symbol, "opened": pos.opened_at, "closed": self.now,
            "cost": pos.initial_cost_sol, "proceeds": pos.proceeds_sol, "pnl": pnl,
            "pnl_pct": pnl / pos.initial_cost_sol * 100, "peak_gain_pct": pos.gain_pct(pos.peak_price),
            "score": pos.score, "initials": pos.initials_taken, "exit": pos.exits[-1][1] if pos.exits else "",
        })
        self.stats["wins" if pnl > 0 else "losses"] += 1
        self.say("close", f"{pos.symbol} {'+' if pnl >= 0 else ''}{pnl:.3f} SOL ({pnl / pos.initial_cost_sol:+.0%})",
                 pos.mint)
        # stop paying for this token's trade data
        asyncio.ensure_future(self.feed.unwatch([pos.mint]))

    async def _tick(self) -> None:
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
            if mint in self.positions:
                if self.book.halted:
                    await self._sell(s, self.positions[mint], 1.0, "kill switch")
                else:
                    await self._check_exit(s)
            elif not s.decided:
                await self._check_entry(s)
            elif s.age(self.now) > self.p.entry.max_age_s + 60 and mint not in self.pending:
                del self.tokens[mint]
        # prune launch history used for serial-deployer / copycat checks
        for book, horizon in ((self.creators, 86400), (self.symbols, 3600)):
            for k in list(book):
                q = book[k]
                while q and q[0] < self.now - horizon:
                    q.popleft()
                if not q:
                    del book[k]
        self.callers.settle(self.now, lambda m: self.tokens[m].curve.price if m in self.tokens else None)

    # ------------------------------------------------------------------ controls (UI)
    async def sell_now(self, mint: str) -> None:
        if mint in self.positions and mint in self.tokens:
            await self._sell(self.tokens[mint], self.positions[mint], 1.0, "manual sell")

    async def kill(self) -> None:
        self.book.halted = self.book.halted or "manual kill switch"
        for mint in list(self.positions):
            await self.sell_now(mint)

    # ------------------------------------------------------------------ reporting
    def summary(self) -> dict:
        closed = self.book.closed
        wins = [c for c in closed if c["pnl"] > 0]
        losses = [c for c in closed if c["pnl"] <= 0]
        gross_win = sum(c["pnl"] for c in wins)
        gross_loss = -sum(c["pnl"] for c in losses)
        return {
            "launches": self.stats["launches"], "entries": self.stats["entries"], "closed": len(closed),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "avg_win_pct": sum(c["pnl_pct"] for c in wins) / len(wins) if wins else 0.0,
            "avg_loss_pct": sum(c["pnl_pct"] for c in losses) / len(losses) if losses else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
            "realized_pnl_sol": sum(c["pnl"] for c in closed),
            "equity_sol": self.equity(), "start_sol": self.book.start_sol,
            "initials_hit": sum(1 for c in closed if c["initials"]),
            "best_pct": max((c["pnl_pct"] for c in closed), default=0.0),
            "worst_pct": min((c["pnl_pct"] for c in closed), default=0.0),
        }

    def snapshot(self) -> dict:
        watching = []
        for s in self.tokens.values():
            if s.mint in self.positions:
                continue
            watching.append({
                "mint": s.mint, "symbol": s.symbol, "age": round(s.age(self.now)), "progress": s.curve.progress,
                "mcap_sol": s.curve.market_cap_sol, "buyers": len(s.buyers), "score": s.score,
                "notes": s.score_notes[:3], "status": s.decided or "watching", "socials": len(s.socials),
                "bundle_pct": s.bundle_pct(), "dev_pct": s.dev_initial_pct(), "spark": s.sparkline(40),
            })
        watching.sort(key=lambda w: (w["status"] != "watching", -w["score"], w["age"]))
        positions = []
        for m, pos in self.positions.items():
            s = self.tokens.get(m)
            if not s:
                continue
            price = s.curve.price
            positions.append({
                "mint": m, "symbol": pos.symbol, "held_s": round(self.now - pos.opened_at), "entry": pos.entry_price,
                "price": price, "gain_pct": pos.gain_pct(price), "peak_gain_pct": pos.gain_pct(pos.peak_price),
                "value_sol": pos.tokens * price, "cost_sol": pos.initial_cost_sol, "proceeds_sol": pos.proceeds_sol,
                "initials": pos.initials_taken, "progress": s.curve.progress, "score": pos.score,
                "spark": [p for t, p, *_ in s.trades if t >= pos.opened_at - 30][-120:],
                "entry_idx": sum(1 for t, *_ in s.trades if pos.opened_at - 30 <= t < pos.opened_at),
            })
        return {
            "now": self.now, "mode": self.mode, "paused": self.paused, "halted": self.book.halted,
            "sol": self.book.sol, "equity": self.equity(), "day_pnl": self.book.day_pnl,
            "summary": self.summary(), "positions": positions, "watching": watching[:40],
            "closed": self.book.closed[-50:][::-1], "rejects": self.rejects.most_common(12),
            "equity_hist": list(self.book.equity_hist)[-400:], "log": list(self.log)[-80:][::-1],
            "callers": sorted(({"caller": k, "calls": v.calls, "avg_return": v.avg_return, "weight": v.weight}
                               for k, v in self.callers.stats.items()), key=lambda c: -c["calls"])[:15],
            "blocked": self.entries_blocked(),
        }

    def save_report(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"summary": self.summary(), "rejects": dict(self.rejects),
                                    "closed": self.book.closed, "params": dict(self.p)}, indent=1, default=str))

