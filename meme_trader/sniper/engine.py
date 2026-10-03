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
import hashlib
import json
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from ..config import ROOT, ConfigError, validate_sniper
from ..journal import DATA, record
from .callouts import Callout, CalloutBook, compose, eligible, is_red_flag, post_telegram
from .copytrade import LeaderBook
from .curve import Curve
from .events import Event, Funding, Launch, Metadata, Migration, Social, Tick, Trade, dumps
from .execution import SniperFill
from .features import extract
from .funding import FundingResolver, cluster_report, cohort
from .notify import Notifier
from .predictor import LogisticModel, expected_value_pct, kelly
from .signals import CallerBook
from .sizing import SolPrice, size_usd, strength
from .strategy import (SniperPosition, evaluate_entry, evaluate_exit, evaluate_late_entry, evaluate_late_exit,
                       gate_checklist)
from .tracker import TokenState

DUST_SOL = 0.0005
TOKEN_ACCOUNT_RENT = 0.00203928       # refundable SOL locked in each new token account (live)
UNRESOLVED_EXPIRY_S = 150             # a Solana tx can't land once its blockhash expires (~60-90 s)
CASH_TOLERANCE_SOL = 0.002            # ledger vs wallet SOL difference that's still just rounding/timing


def reason_key(note: str) -> str:
    """'dev bought 7.1% > 6%' -> 'dev bought' (for grouping reject stats)."""
    return re.split(r"[\d(]", note, maxsplit=1)[0].strip(" :-") or note


# Settings the dashboard may change at runtime: (key under sniper, type, min, max, label, help)
CONTROLS = [
    ("entry.enabled", "bool", None, None, "Sniper entries", "Early-entry sniper buys"),
    ("copy.enabled", "bool", None, None, "Copy trading", "Mirror leader wallets"),
    ("callouts.enabled", "bool", None, None, "Callouts", "$1 bags + callout cards"),
    ("late.enabled", "bool", None, None, "Graduation plays", "Late-curve momentum, out before migration"),
    ("risk_adapt.enabled", "bool", None, None, "Defense mode", "Halve size + raise the bar after a bad run"),
    ("entry.min_score", "int", 0, 100, "Min entry score", "Rule score needed to buy"),
    ("predict.min_p", "float", 0.0, 0.95, "Min P(2x first)", "Model gate; 0 = show only"),
    ("capital.max_open_positions", "int", 1, 20, "Max open positions", "Trading slots (callout bags excluded)"),
    ("sizing.base_usd", "float", 1.0, 50.0, "Base buy ($)", "Size for a minimal signal"),
    ("sizing.max_usd", "float", 1.0, 100.0, "Max buy ($, hard cap)", "Never exceeded, whatever asks"),
    ("capital.daily_loss_limit_sol", "float", 0.01, 100.0, "Daily loss limit (SOL)", "No new entries past this"),
    ("exit.profile", "enum:trail,ladder", None, None, "Exit profile", "trail = initials + trailing stop"),
    ("exit.stop_loss_pct", "float", 5.0, 90.0, "Stop loss %", "Trail profile hard stop"),
]


class Book:
    """The bot's cash ledger. `sol` is cash actually held - live, what the wallet should show for the bot's
    budget (SOL locked as refundable token-account rent isn't counted until it's reclaimed). `reserved` is
    cash promised to buy orders that haven't resolved yet, so concurrent approvals can't spend it twice."""

    def __init__(self, start_sol: float):
        self.sol = start_sol
        self.start_sol = start_sol
        self.day = ""
        self.day_pnl = 0.0
        self.closed: list[dict] = []
        self.equity_hist: deque = deque(maxlen=2000)       # chart only; risk stats below never forget
        self.halted = ""
        self.reserved: dict[str, float] = {}               # mint -> SOL held back for an in-flight buy
        self.peak_equity = start_sol
        self.max_dd_pct = 0.0

    @property
    def available(self) -> float:
        return self.sol - sum(self.reserved.values())

    def mark(self, equity: float) -> float:
        """Track the session's peak and worst drawdown on every update, independent of the chart buffer."""
        self.peak_equity = max(self.peak_equity, equity)
        dd = (1 - equity / self.peak_equity) * 100 if self.peak_equity > 0 else 0.0
        self.max_dd_pct = max(self.max_dd_pct, dd)
        return dd


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
        # replays start from neutral caller weights: today's learned track records are future information
        self.callers = CallerBook(DATA / "callers.json" if log_to_journal else None)
        self.session = f"{mode}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        self.config_id = hashlib.sha1(json.dumps(params["sniper"], sort_keys=True,
                                                 default=str).encode()).hexdigest()[:10]
        self.order_tasks: set[asyncio.Task] = set()        # live orders run beside the feed, never in front of it
        self.unresolved: dict[str, dict] = {}              # signature -> sent order whose outcome isn't known yet
        self._last_resolve = 0.0
        self._resolving = False
        self._last_cash_check = 0.0
        self._surplus_noted = False
        self._callout_inflight = ""                   # mint of the callout bag being bought
        self.leaders = LeaderBook(self.p.copy.leaders, DATA / "leaders.json" if log_to_journal else None)
        self.record_path = Path(record_path) if record_path else None
        self.record_file = self._open_record(self.record_path) if self.record_path else None
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
        self.funding_started: dict[str, tuple[float, frozenset]] = {}
        self._tick_lock = asyncio.Lock()
        self.http = None                                   # shared aiohttp session (created lazily, live only)
        self._last_funders_save = 0.0
        self._last_price_fallback = 0.0
        self._last_callout_scan = -1e12
        self._last_cleanup = -1e12
        # live money must survive restarts (the Ubuntu service restarts on crash)
        self.persist = mode.startswith("live") if persist is None else persist
        self.state_path = DATA / f"sniper_state_{mode.split('-')[0]}.json"
        self._last_state_save = 0.0
        self._load_funders()
        self._last_price_refresh = -1e12
        # probability model (python -m meme_trader.sniper train), gate audit, defensive mode
        mp = Path(self.p.predict.model_path)
        self.model_path = mp if mp.is_absolute() else ROOT / mp
        # live: only a promoted model. Replays decide on their first event, once they know which data
        # they cover (a model that has seen that data must not grade itself on it).
        self.model = self._load_model(for_live=True) if self.p.predict.enabled and feed.realtime else None
        self._model_checked_replay = feed.realtime
        self._model_mtime = self._mtime(self.model_path)
        self._last_model_check = 0.0
        self.audit: dict[str, list] = {}                   # mint -> [gate, t0, p0, peak, trough, outcome]
        self.gate_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "win": 0, "loss": 0, "peak_sum": 0.0})
        self.defense_until = 0.0
        self.defense_reason = ""
        self._last_late_scan = -1e12
        self._snap_cache: tuple[float, dict] | None = None
        self._analytics: tuple | None = None
        self._summary_cache: tuple | None = None
        self.last_event = 0.0                              # feed health: time of the last real event
        self._last_flush = 0.0

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

    def _global_block(self) -> str:
        """Account-level stops that apply to EVERY entry source (sniper, copy, graduation, callout)."""
        if self.book.halted:
            return "halted: " + self.book.halted
        if self.paused:
            return "paused"
        if getattr(self.feed, "degraded", False):
            return f"on backup feed {self.feed.host} (no trade data) - entries paused"
        if -self.book.day_pnl >= self.p.capital.daily_loss_limit_sol:
            return "daily loss limit"
        return ""

    def _order_overhead(self, source: str = "") -> float:
        """SOL an order costs beyond its principal: priority + base fee, plus refundable rent for the new
        token account when trading live. Reserved up front so it can't come out of the exit reserve."""
        prio = self.p.callouts.priority_fee_sol if source == "callout" else self.p.execution.priority_fee_sol
        return prio + 0.000005 + (TOKEN_ACCOUNT_RENT if self.mode.startswith("live") else 0.0)

    def _cash_block(self, sol: float, source: str = "") -> str:
        if self.book.available - (sol + self._order_overhead(source)) < self.p.capital.min_sol_reserve:
            return "low SOL"
        return ""

    def entries_blocked(self, buy_sol: float | None = None) -> str:
        """Preliminary check before a strategy prepares an order (the final one is _authorize, with the
        order's real size)."""
        c = self.p.capital
        why = self._global_block()
        if why:
            return why
        trading = sum(1 for p in self.positions.values() if p.source != "callout")   # $1 callout bags don't count
        in_flight = len(self.book.reserved.keys() - set(self.positions))
        if trading + in_flight + len(self.reviewing) >= c.max_open_positions:
            return "max positions"
        need = buy_sol or (self.p.sizing.max_usd / self.sol_price.usd if self.p.sizing.enabled else c.buy_sol)
        return self._cash_block(need)

    def _authorize(self, mint: str, sol: float, source: str) -> str:
        """Final, central go/no-go for a buy of exactly `sol`, right before cash is reserved for it."""
        why = self._global_block()
        if why:
            return why
        if source != "callout":
            trading = sum(1 for p in self.positions.values() if p.source != "callout")
            in_flight = len(self.book.reserved.keys() - set(self.positions) - {mint})
            if trading + in_flight + len(self.reviewing - {mint}) >= self.p.capital.max_open_positions:
                return "max positions"
        return self._cash_block(sol, source)

    def _ctx(self, s: TokenState) -> dict:
        ws = [self.leaders.weight(x.author) if x.source == "wallet" else self.callers.weight(f"{x.source}:{x.author}")
              for x in s.socials]
        return {"creator_launches": len(self.creators.get(s.creator, ())),
                "symbol_dupes": max(len(self.symbols.get(s.symbol.upper(), ())) - 1, 0),
                "social_weight": max(ws) if ws else 0.0}

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _load_model(self, for_live: bool, replay_start: float | None = None) -> LogisticModel | None:
        """Live: only a model `promote` approved (trained on recordings, with held-out skill).
        Replay: only a model whose training data ended before the replay's first event, so a backtest,
        sweep or compare never scores a model on launches it was fitted to."""
        m = LogisticModel.load(self.model_path)
        if m is None:
            return None
        info = m.info or {}
        if for_live:
            if not info.get("promoted") or info.get("source") != "recorded":
                self.say("info", f"{self.model_path.name} isn't a promoted model - ignored (train, then promote)")
                return None
            return m
        end = info.get("data_end_ts")
        if end is None or replay_start is None or end >= replay_start:
            self.say("info", f"{self.model_path.name} was trained on data overlapping this replay (or has no "
                             "provenance) - replay runs without it")
            return None
        return m

    def _maybe_reload_model(self) -> None:
        """`promote` while the bot runs: the new model is picked up within a minute, no restart."""
        if not self.p.predict.enabled or not self.feed.realtime or self.now - self._last_model_check < 60:
            return
        self._last_model_check = self.now
        mt = self._mtime(self.model_path)
        if mt and mt != self._model_mtime:
            self._model_mtime = mt
            m = self._load_model(for_live=True)
            if m is not None:
                self.model = m
                for s in self.tokens.values():
                    s.p = None                              # re-score with the new model
                auc = (m.info or {}).get("test", {}).get("auc")
                self.say("info", f"new prediction model loaded (held-out AUC {auc:.3f})" if auc else "new prediction model loaded")

    def _model_steers(self) -> bool:
        """Whether the model may change trades (gate/EV filter/sizing). display_only keeps it on screen only."""
        return self.model is not None and not self.p.predict.get("display_only", True)

    @staticmethod
    def _open_record(path: Path):
        if str(path).endswith(".gz"):
            import gzip
            return gzip.open(path, "at", compresslevel=5)
        return path.open("a")

    def _rotate_record(self) -> None:
        """A 24/7 service starts a new feed-YYYY-MM-DD.jsonl at UTC midnight and gzips the finished
        day in the background (plain text while writing, so a crash can't corrupt an archive)."""
        m = re.fullmatch(r"feed-\d{4}-\d{2}-\d{2}(\.jsonl(?:\.gz)?)", self.record_path.name)
        if not m:
            return
        new = self.record_path.with_name(f"feed-{time.strftime('%Y-%m-%d', time.gmtime(self.now))}{m.group(1)}")
        if new == self.record_path:
            return
        self.record_file.close()
        old, self.record_path = self.record_path, new
        self.record_file = self._open_record(new)
        self.say("info", f"recording to {new.name}")
        if old.suffix == ".jsonl" and self.feed.realtime:
            from .feeds import compress_file

            def job(path=old):
                try:
                    compress_file(path)
                except OSError as err:              # e.g. disk full: the plain file stays, nothing lost
                    print(f"could not compress {path.name}: {err}")
            asyncio.get_running_loop().run_in_executor(None, job)

    async def _watch(self, mint: str) -> None:
        # recordings cover graduation plays' whole window (late.max_age_s) even while they're off, so
        # `compare` can test turning them on without the data running out halfway
        keep = max(self.p.record.full_window_s, self.p.late.max_age_s)
        self.watch_until[mint] = self.now + (keep if self.record_file else 1e12)
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
            if self.order_tasks:                # let sent orders finish so their outcome is booked and saved
                await asyncio.wait(set(self.order_tasks), timeout=120)
            if self.record_file:
                self.record_file.close()
            self.leaders.save()
            self._save_funders()
            if self.journal:
                self.callers.save()
            self.save_state()
            if self.http is not None and not self.http.closed:
                await self.http.close()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.handle(Tick(self.feed.now()))

    async def handle(self, e: Event) -> None:
        try:
            await self._handle(e)
        except Exception as err:   # live: one bad event/tick must never stop trading or exit handling
            if not self.feed.realtime:
                raise             # backtests/tests: surface bugs
            import traceback
            self.say("error", f"internal error on {getattr(e, 'kind', '?')}: {err!r}")
            traceback.print_exc()

    async def _handle(self, e: Event) -> None:
        if not self._model_checked_replay:
            self._model_checked_replay = True
            if self.p.predict.enabled and self.model is None:     # (an injected model is the caller's choice)
                self.model = self._load_model(for_live=False, replay_start=e.ts)
        self.now = max(self.now, e.ts)
        if not isinstance(e, Tick):
            self.last_event = self.now
            if self.record_file:
                self.record_file.write(dumps(e) + "\n")
        if isinstance(e, Launch):
            await self._on_launch(e)
        elif isinstance(e, Metadata):
            s = self.tokens.get(e.mint)
            if s is not None and s.launch is not None:
                s.launch.twitter, s.launch.telegram, s.launch.website = e.twitter, e.telegram, e.website
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
            if len(self.funders) > 300_000:               # bounded memory on a 24/7 feed (oldest first)
                for w in list(self.funders)[:50_000]:
                    del self.funders[w]
        if self.now - self._last_tick >= 1 and not self._tick_lock.locked():
            self._last_tick = self.now
            async with self._tick_lock:                   # a slow live sell must not let ticks overlap
                await self._tick()

    async def _on_launch(self, e: Launch) -> None:
        if e.mint in self.tokens:          # metadata update for a known launch (recordings before Metadata events)
            s = self.tokens[e.mint]
            if s.launch:
                s.launch.twitter, s.launch.telegram, s.launch.website = e.twitter, e.telegram, e.website
            else:                          # known only from a restart/leader trade: now we know its creator
                s.launch = e
                if e.dev_buy_tokens > 0:
                    s.holders.setdefault(e.creator, e.dev_buy_tokens)
                    s.buyers.add(e.creator)
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
            if self.record_file:                 # stamped with its arrival time: replays mustn't know it earlier
                md_event = Metadata(e.mint, self.feed.now(), e.twitter, e.telegram, e.website)
                self.record_file.write(dumps(md_event) + "\n")

    async def _on_trade(self, e: Trade) -> None:
        a = self.audit.get(e.mint)
        if a is not None:                                  # gate audit: what happened after the decision?
            px = e.v_sol / e.v_tokens if e.pool == "pump" and e.v_tokens > 0 else (e.mcap_sol / 1e9 if e.mcap_sol else 0)
            if px > 0:
                a[3], a[4] = max(a[3], px), min(a[4], px)
                if not a[5]:                               # first passage, same rule as the model's label
                    pr = self.p.predict
                    if px >= a[2] * (1 + pr.up_pct / 100):
                        a[5] = "win"
                    elif px <= a[2] * (1 - pr.down_pct / 100):
                        a[5] = "loss"
        s = self.tokens.get(e.mint)
        known = e.trader in self.leaders.leaders            # paused leaders still matter: we follow their sells
        lead = known and self.leaders.is_leader(e.trader)
        if s is None:
            if not lead:
                return
            s = self.tokens[e.mint] = TokenState(e.mint, None, e.ts)   # token we only know via a leader
            if e.v_sol and e.v_tokens:
                s.curve = Curve(e.v_sol, e.v_tokens)
            await self._watch(e.mint)
        s.on_trade(e, self.p.entry.bundle_window_s, self.p.entry.sniper_window_s)
        if s.unpriced_calls and s.price_known:            # calls on a CA we hadn't priced yet
            for caller, ts in s.unpriced_calls:
                self.callers.on_call(caller, e.mint, ts, s.curve.price)
            s.unpriced_calls.clear()
        if known:
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
        if s.price_known:
            self.callers.on_call(f"{e.source}:{e.author}", e.mint, e.ts, s.curve.price)
        else:                                              # don't score the caller against a placeholder price
            s.unpriced_calls.append((f"{e.source}:{e.author}", e.ts))
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
        if not self.leaders.is_leader(e.trader):           # paused: follow sells only
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
        if not self.p.entry.enabled:
            s.decided = "sniper off"                       # callouts / graduation plays still consider it
            return
        d = evaluate_entry(s, self.now, self.p.entry, self._ctx(s))
        s.score, s.score_notes = d.score, d.notes or []
        if d.action != "reject" and self.model is not None:
            p = self._predict(s)
            s.score_notes = s.score_notes + [f"P(2x) {p:.0%}"]
        if d.action == "reject":
            s.decided = "rejected: " + d.notes[0]
            self.rejects[reason_key(d.notes[0])] += 1
            self._audit_start(s, reason_key(d.notes[0]))
            # may still be worth a callout or a graduation play later: keep its trades coming
            keep = (self.p.callouts.enabled or self.p.late.enabled) and not is_red_flag(d.notes[0])
            if s.mint not in self.positions and not keep:
                await self._unwatch(s.mint)
            return
        if d.action != "enter":
            return
        if self._defensive() and d.score < self.p.entry.min_score + self.p.risk_adapt.min_score_add:
            s.score_notes = [f"defense mode: score {d.score:.0f} < {self.p.entry.min_score + self.p.risk_adapt.min_score_add}"]
            return
        pr = self.p.predict
        if self._model_steers() and s.p is not None:
            if pr.min_p > 0 and s.p < pr.min_p:
                s.score_notes = [f"P(2x) {s.p:.0%} < {pr.min_p:.0%}"] + s.score_notes
                return
            if pr.require_positive_ev and self._ev(s.p) < 0:
                s.score_notes = [f"negative EV ({self._ev(s.p):+.0f}%)"] + s.score_notes
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
            self._audit_start(s, reason_key(verdict))
            self.say("info", f"{s.symbol} rejected: {verdict}", s.mint)
            return
        await self._enter(s, kind="sniper", score=d.score, buy_sol=self.p.capital.buy_sol, notes=d.notes or [])

    # ------------------------------------------------------------------ model / audit / defense
    def _cost_pct(self) -> float:
        ex = self.p.execution
        return 2 * (ex.curve_fee_pct + ex.platform_fee_pct + ex.paper_latency_slippage_pct)

    def _predict(self, s: TokenState) -> float | None:
        if self.model is None:
            return None
        if s.p is None or self.now - s.p_ts >= 3:
            s.p = self.model.predict(extract(s, self.now, self._ctx(s)))
            s.p_ts = self.now
        return s.p

    def _ev(self, p: float) -> float:
        pr = self.p.predict
        return expected_value_pct(p, pr.up_pct, pr.down_pct, self._cost_pct())

    def _audit_start(self, s: TokenState, gate: str) -> None:
        """Follow a decision for audit.window_s: had we bought at this price, would it have hit +up% before
        -down% (the model's label)? '(bought)' rows follow our own entries, as the yardstick for the gates."""
        if s.price_known and s.mint not in self.audit and (gate == "(bought)" or s.mint not in self.positions):
            px = s.curve.price
            self.audit[s.mint] = [gate, self.now, px, px, px, ""]

    def _audit_settle(self, force: bool = False, final: bool = False) -> None:
        """final (end of a backtest): count resolved audits, drop ones whose window was cut short."""
        window = self.p.audit.window_s
        for mint, (gate, t0, p0, peak, trough, outcome) in list(self.audit.items()):
            if force or self.now - t0 >= window or final:
                del self.audit[mint]
                if final and not outcome and self.now - t0 < window:
                    continue
                g = self.gate_stats[gate]
                g["n"] += 1
                g["win"] += outcome == "win"
                g["loss"] += outcome == "loss"
                g["peak_sum"] += peak / p0

    def gate_audit(self) -> list[dict]:
        rows = []
        for gate, g in self.gate_stats.items():
            n = g["n"]
            if n:
                rows.append({"gate": gate, "n": n, "win_first_pct": g["win"] / n * 100,
                             "loss_first_pct": g["loss"] / n * 100,
                             "flat_pct": (n - g["win"] - g["loss"]) / n * 100, "avg_peak_x": g["peak_sum"] / n})
        return sorted(rows, key=lambda r: (r["gate"] != "(bought)", -r["n"]))

    def _defensive(self) -> bool:
        return self.p.risk_adapt.enabled and self.now < self.defense_until

    def _update_defense(self) -> None:
        R = self.p.risk_adapt
        if not R.enabled:
            return
        recent = [c for c in self.book.closed if c["source"] != "callout"][-R.lookback_trades:]
        streak = 0
        for c in reversed(recent):
            if c["pnl"] > 0:
                break
            streak += 1
        window = sum(c["pnl"] for c in recent)
        if streak >= R.loss_streak or (len(recent) >= R.lookback_trades and window <= -R.window_loss_sol):
            if not self._defensive():
                why = f"{streak} losses in a row" if streak >= R.loss_streak else f"last {len(recent)} trades {window:+.3f} SOL"
                self.defense_reason = why
                self.say("error", f"DEFENSE MODE for {R.minutes} min ({why}): size x{R.size_mult}, "
                                  f"min score +{R.min_score_add}")
            self.defense_until = self.now + R.minutes * 60

    async def _funding_gate(self, s: TokenState) -> str:
        """'' = pass, 'wait' = lookups in flight, otherwise a rejection reason."""
        f = self.p.entry.funding
        if not f.enabled:
            return ""
        wallets = cohort(s, f.cohort_size)
        missing = [w for w in wallets + ([s.creator] if s.creator else []) if w not in self.funders]
        if missing and self.resolver.available and self.feed.realtime:
            started, asked = self.funding_started.get(s.mint, (0.0, frozenset()))
            new = [w for w in missing if w not in asked]
            if new:                                       # new cohort wallets since the last batch
                self.funding_started[s.mint] = (self.now, asked | frozenset(new))
                asyncio.create_task(self._resolve_funders(new))
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

        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        results = await asyncio.gather(*(self.resolver.lookup(self.http, w) for w in wallets))
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
        edge = None
        p = self._predict(s)
        if p is not None and self._model_steers() and self.p.predict.kelly_fraction > 0:
            pr = self.p.predict
            edge = max(kelly(p, pr.up_pct, pr.down_pct, self._cost_pct()), 0.0) * pr.kelly_fraction
        st, _ = strength(score, self.p.entry.min_score if kind == "sniper" else 0, nb / max(ns, 1),
                         s.curve.price / s.peak_price if s.peak_price else 1.0, smart, desk_mult, edge)
        usd, why = size_usd(z, st, kind, s.curve.real_sol, self.sol_price.usd,
                            mult=self.p.late.buy_usd_mult if kind == "late" else 1.0,
                            defense=self.p.risk_adapt.size_mult if self._defensive() else 1.0)
        if p is not None:
            why += f", P(2x) {p:.0%}"
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
                self._audit_start(s, "desk passed")
            return
        moved = (s.curve.price / start_price - 1) * 100 if start_price else 0
        notes = notes + [f"desk x{v.size_mult:.2f}"]
        size = self._size(s, kind, score, buy_sol, v.size_mult, notes)
        why = self.entries_blocked(size) or ("already held" if s.mint in self.positions else "") or \
            (f"price moved {moved:+.0f}% during review" if moved > self.p.desk.max_price_move_pct else "") or \
            ("dev sold" if s.dev_sold else "")
        if why:
            self.say("info", f"desk approved {s.symbol} but skipped: {why}", s.mint)
            if kind == "sniper" and not why.startswith(("max positions", "paused", "low SOL")):
                s.decided = "skipped: " + why          # don't pay for a fresh desk review every tick
            return
        await self._buy(s, score, size, notes, source, leader)

    async def _buy(self, s: TokenState, score: float, sol: float, notes: list[str], source: str = "sniper",
                   leader: str = "", then=None) -> None:
        """Authorize with the final size, reserve the cash, then send. Live, the order runs as its own task,
        so the feed keeps being read (other tokens' stops and dev sells) while it confirms; backtests run it
        inline so replays stay deterministic. `then`: coroutine function to run after a successful fill."""
        if s.mint in self.positions or s.mint in self.pending:   # never stack a second position on one mint
            return
        z = self.p.sizing
        if z.enabled and sol * self.sol_price.usd > z.max_usd + 1e-6:     # hard cap, whatever asked for more
            self.say("error", f"size ${sol * self.sol_price.usd:.2f} over hard cap ${z.max_usd} - clamped", s.mint)
            sol = round(z.max_usd / self.sol_price.usd, 4)
        if sol <= 0:                                      # e.g. the liquidity cap says the curve is too thin
            self.stats["skipped_no_size"] += 1
            return
        why = self._authorize(s.mint, sol, source)
        if why:
            self.stats["skipped_" + why.split(":")[0].replace(" ", "_")] += 1
            return
        self.book.reserved[s.mint] = sol + self._order_overhead(source)
        self.pending.add(s.mint)
        s.decided = "entered"
        await self._dispatch(self._run_buy(s, score, sol, notes, source, leader, then))

    async def _dispatch(self, coro) -> None:
        if self.feed.realtime:
            task = asyncio.create_task(coro)
            self.order_tasks.add(task)
            task.add_done_callback(self._order_done)
        else:
            await coro

    def _order_done(self, task: asyncio.Task) -> None:
        self.order_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:   # never lose an order-path bug silently
            import traceback

            self.say("error", f"internal error in an order task: {task.exception()!r}")
            traceback.print_exception(task.exception())

    async def _run_buy(self, s: TokenState, score, sol, notes, source, leader, then) -> None:
        try:
            fill = await self.ex.buy(s.mint, s.curve, sol,
                                     self.p.callouts.priority_fee_sol if source == "callout" else None)
        except Exception as e:                            # an executor bug must not leave cash reserved forever
            fill = SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        if fill.unknown:                                  # sent, but did it land? keep the cash and the mint held
            self._book_fees_lost(fill, s)
            self._track_unresolved(fill.signature, {"mint": s.mint, "side": "buy", "sol": sol, "score": score,
                                                    "notes": list(notes), "source": source, "leader": leader})
            self.say("error", f"buy {s.symbol}: sent ({fill.signature[:8]}…) but its outcome is unknown - "
                              "its cash stays reserved until the chain says", s.mint)
            return
        self.book.reserved.pop(s.mint, None)
        self.pending.discard(s.mint)
        self._apply_buy(s, fill, score, notes, source, leader)
        if fill.ok and then is not None:
            try:
                await then()
            except Exception as e:                        # e.g. posting a callout card - the buy itself stands
                self.say("error", f"after-buy step for {s.symbol} failed: {e!r}", s.mint)

    def _book_fees_lost(self, fill: SniperFill, s: TokenState) -> None:
        """Failed transactions that landed still burned fees: real cash, and a real loss for today."""
        if fill.fees_lost > 0:
            self.book.sol -= fill.fees_lost
            self.book.day_pnl -= fill.fees_lost
            self.say("error", f"{s.symbol}: failed transaction(s) still cost {fill.fees_lost:.6f} SOL in fees", s.mint)

    def _apply_buy(self, s: TokenState, fill: SniperFill, score, notes, source, leader) -> None:
        self._book_fees_lost(fill, s)
        if not fill.ok:
            self.say("error", f"buy {s.symbol} failed: {fill.error}", s.mint)
            self.save_state()
            return
        self.book.sol -= fill.sol + fill.rent             # rent is cash locked in the token account until reclaimed
        self.positions[s.mint] = SniperPosition(
            mint=s.mint, symbol=s.symbol, opened_at=self.now, entry_price=fill.price, tokens=fill.tokens,
            initial_tokens=fill.tokens, cost_sol=fill.sol, initial_cost_sol=fill.sol, score=score,
            peak_price=fill.price, exits=[], source=source, leader=leader, desk=getattr(s, "desk", ""),
            p=s.p, trough_price=fill.price, rent_sol=fill.rent)
        if source != "callout" and s.mint not in self.audit:        # yardstick row for the gate audit
            self.audit[s.mint] = ["(bought)", self.now, fill.price, fill.price, fill.price, ""]
        self.stats["entries"] += 1
        self.save_state()
        self.stats["entries_" + source.split(":")[0]] += 1
        self.say("buy", f"{s.symbol} {fill.sol:.3f} SOL @ curve {s.curve.progress:.0%} [{source}] | score {score:.0f} | "
                        + ", ".join(notes), s.mint, signature=fill.signature)

    async def _check_exit(self, s: TokenState) -> None:
        pos = self.positions[s.mint]
        if not s.price_known:          # restored after a restart: wait for a real price before any exit logic,
            limit = {"late": self.p.late.max_hold_s,              # but each strategy's time limit still holds
                     "callout": self.p.callouts.hold_s}.get(pos.source, self.p.exit.max_hold_s)
            if self.now - pos.opened_at >= limit:
                await self._sell(s, pos, 1.0, f"max hold {limit:.0f}s (price unknown)")
            return
        px = s.curve.price
        pos.peak_price = max(pos.peak_price, px)
        pos.trough_price = min(pos.trough_price or px, px)
        if pos.source == "callout":         # hold the $1 callout bag; never trade it against followers
            held = self.now - pos.opened_at
            r = (1.0, "dev sold") if s.dev_sold else \
                ((1.0, "callout hold done") if held >= self.p.callouts.hold_s else None)
        elif (why := await self._cluster_watch(s)):
            r = (1.0, why)
        elif pos.source == "late":
            r = evaluate_late_exit(pos, s, self.now, self.p.late, self.p.exit)
        elif pos.leader and not self.p.copy.use_own_exits:
            r = None
            if s.dev_sold and self.p.exit.exit_on_dev_sell:
                r = (1.0, "dev sold")
        else:
            r = evaluate_exit(pos, s, self.now, self.p.exit, self.fee)
        if r:
            await self._sell(s, pos, r[0], r[1])

    async def _sell(self, s: TokenState, pos: SniperPosition, frac: float, reason: str) -> None:
        if s.mint in self.pending or self.positions.get(s.mint) is not pos:   # one order at a time, open positions only
            return
        tokens = pos.tokens if frac >= 1 else pos.tokens * frac
        self.pending.add(s.mint)
        await self._dispatch(self._run_sell(s, pos, tokens, reason))

    async def _run_sell(self, s: TokenState, pos: SniperPosition, tokens: float, reason: str) -> None:
        try:
            fill = await self.ex.sell(s.mint, s.curve, tokens,
                                      self.p.callouts.priority_fee_sol if pos.source == "callout" else None)
        except Exception as e:
            fill = SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        if fill.unknown:              # sending a fresh sell now could sell twice: wait for the chain instead
            self._book_fees_lost(fill, s)
            self._track_unresolved(fill.signature, {"mint": s.mint, "side": "sell", "tokens": tokens, "reason": reason})
            self.say("error", f"sell {s.symbol}: sent ({fill.signature[:8]}…) but its outcome is unknown - no new "
                              "sell until the chain says", s.mint)
            return
        self.pending.discard(s.mint)
        self._apply_sell(s, pos, fill, reason)

    def _apply_sell(self, s: TokenState, pos: SniperPosition, fill: SniperFill, reason: str) -> None:
        self._book_fees_lost(fill, s)
        if not fill.ok:
            self.say("error", f"sell {s.symbol} failed: {fill.error}", s.mint)
            self.save_state()
            return
        self.book.sol += fill.sol + fill.rent_reclaimed   # fill.sol can be negative: fees above proceeds
        if self.positions.get(s.mint) is not pos:          # closed meanwhile (e.g. reconcile): cash is still real
            self.save_state()
            return
        sold = min(fill.tokens, pos.tokens)
        cost_part = pos.cost_sol * sold / pos.tokens if pos.tokens else 0
        pos.tokens -= sold
        pos.cost_sol -= cost_part
        pos.proceeds_sol += fill.sol
        pos.rent_sol = max(pos.rent_sol - fill.rent_reclaimed, 0.0)
        pos.exits.append((self.now, reason, sold, fill.sol))
        self.book.day_pnl += fill.sol - cost_part
        if reason.startswith("initials"):
            pos.initials_taken = True
        elif reason.startswith("ladder ") and "x sell" in reason:
            pos.ladder_hit += 1
            pos.initials_taken = True
        self.say("sell", f"{s.symbol} {sold / pos.initial_tokens:.0%} for {fill.sol:.3f} SOL | {reason}",
                 s.mint, signature=fill.signature)
        if pos.tokens * s.curve.price < DUST_SOL:
            self._close(pos, s)
        self.save_state()

    def _track_unresolved(self, sig: str, order: dict) -> None:
        self.unresolved[sig] = {**order, "sent_at": time.time()}
        self.save_state()

    async def _resolve_unresolved(self) -> None:
        """Ask the chain about orders whose outcome was unknown. Landed: book them (a late buy becomes a
        managed position). Still unknown after the blockhash must have expired: it never landed."""
        resolve = getattr(self.ex, "resolve", None)
        if resolve is None or self._resolving:
            return
        self._resolving = True
        try:
            await self._resolve_each(resolve)
        finally:
            self._resolving = False

    async def _resolve_each(self, resolve) -> None:
        for sig, o in list(self.unresolved.items()):
            mint = o["mint"]
            try:
                fill = await resolve(sig, mint, o["side"])
            except Exception:
                continue
            if fill.unknown and time.time() - o["sent_at"] < UNRESOLVED_EXPIRY_S:
                continue
            del self.unresolved[sig]
            s = self.tokens.get(mint) or self.tokens.setdefault(mint, TokenState(mint, None, self.now))
            if o["side"] == "buy":
                self.book.reserved.pop(mint, None)
                self.pending.discard(mint)
                if fill.unknown:
                    self.say("info", f"buy {s.symbol} ({sig[:8]}…) never landed - cash released", mint)
                    self.save_state()
                    continue
                self._apply_buy(s, fill, o["score"], o["notes"] + ["landed late"], o["source"], o["leader"])
                if fill.ok:
                    await self._watch(mint)
            else:
                self.pending.discard(mint)
                pos = self.positions.get(mint)
                if fill.unknown:
                    self.say("info", f"sell {s.symbol} ({sig[:8]}…) never landed - position kept, exits resume", mint)
                    self.save_state()
                    continue
                if pos is not None:
                    self._apply_sell(s, pos, fill, o["reason"] + " (landed late)")
                else:
                    self._book_fees_lost(fill, s)
                    if fill.ok:
                        self.book.sol += fill.sol + fill.rent_reclaimed
                    self.save_state()

    def _model_id(self) -> str:
        info = (self.model.info or {}) if self.model else {}
        return str(info.get("promoted_from") or info.get("trained_at") or "") if self.model else ""

    def _close(self, pos: SniperPosition, s: TokenState) -> None:
        if self.positions.pop(pos.mint, None) is None:
            return
        s.late_tried = True                        # no graduation-play re-buy of a token we just traded
        if pos.cost_sol > 0:                       # unsold remainder (dust, gone from wallet) is written off -
            self.book.day_pnl -= pos.cost_sol      # once, and today, so the daily loss limit sees all of it
            pos.cost_sol = 0.0
        pnl = pos.proceeds_sol - pos.initial_cost_sol
        row = {
            "mint": pos.mint, "symbol": pos.symbol, "opened": pos.opened_at, "closed": self.now,
            "cost": pos.initial_cost_sol, "proceeds": pos.proceeds_sol, "pnl": pnl,
            "pnl_pct": pnl / max(pos.initial_cost_sol, 1e-12) * 100, "peak_gain_pct": pos.gain_pct(pos.peak_price),
            "score": pos.score, "initials": pos.initials_taken, "exit": pos.exits[-1][1] if pos.exits else "",
            "source": pos.source, "desk": pos.desk, "p": pos.p,
            "mae_pct": pos.gain_pct(pos.trough_price) if pos.trough_price else 0.0,
            # which run produced this trade: demo, paper and live results must never be mixed up
            "mode": self.mode, "session": self.session, "start_sol": self.book.start_sol,
            "config": self.config_id, "model": self._model_id(),
        }
        self.book.closed.append(row)
        self.stats["wins" if pnl > 0 else "losses"] += 1
        self._update_defense()
        self.say("close", f"{pos.symbol} {'+' if pnl >= 0 else ''}{pnl:.3f} SOL ({pnl / max(pos.initial_cost_sol, 1e-12):+.0%}) "
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
        self.book.mark(eq)
        if self.now - self._last_equity >= 5:
            self._last_equity = self.now
            self.book.equity_hist.append((self.now, eq))
        if self.unresolved and self.feed.realtime and not self._resolving and self.now - self._last_resolve >= 5:
            self._last_resolve = self.now
            await self._dispatch(self._resolve_unresolved())
        if self.feed.realtime and self.mode.startswith("live") and self.now - self._last_cash_check >= 120 \
                and not self.pending and not self.book.reserved:
            self._last_cash_check = self.now
            await self._dispatch(self.check_cash())
        dd = (1 - eq / self.book.start_sol) * 100
        if not self.book.halted and dd >= self.p.capital.max_drawdown_pct:
            self.book.halted = f"drawdown {dd:.0f}%"
            self.say("error", f"KILL SWITCH: {self.book.halted} - selling everything")
        await self._price_fallback()
        cleanup = self.now - self._last_cleanup >= 10       # deletions only need a 10 s cadence
        if cleanup:
            self._last_cleanup = self.now
            recent_calls = {c.mint for c in self.callouts.calls if self.now - c.ts < 3660}
            keep_s = max(self.p.entry.max_age_s, self.p.callouts.max_age_s if self.p.callouts.enabled else 0) + 60
            if self.p.late.enabled:
                keep_s = max(keep_s, self.p.late.max_age_s + 60)
        for mint, s in list(self.tokens.items()):
            if mint in self.pending or mint in self.reviewing:
                continue
            if mint in self.positions:
                if self.book.halted:
                    await self._sell(s, self.positions[mint], 1.0, "kill switch")
                else:
                    await self._check_exit(s)
            elif not s.decided:
                await self._check_entry(s)
            elif cleanup and self.now - s.created_ts > keep_s and mint not in recent_calls:
                del self.tokens[mint]                       # (calls keep their token priced until the 1h result)
                if not self.record_file:
                    await self._unwatch(mint)
        if cleanup:
            self._audit_settle()
            self._maybe_reload_model()
        await self._maybe_callout()
        await self._maybe_late()
        if self.record_file and self.now - self._last_flush >= 30:
            self._last_flush = self.now
            self._rotate_record()
            self.record_file.flush()
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
            self.callers.save()
            if self.now - self._last_funders_save >= 600:
                self._last_funders_save = self.now
                if self.feed.realtime:
                    await asyncio.to_thread(self._save_funders)
                else:
                    self._save_funders()
            (DATA / "sniper_summary.json").write_text(json.dumps(
                {"ts": self.now, "summary": self.summary(), "rejects": dict(self.rejects),
                 "leaders": self.leaders.snapshot(), "desk": self.desk_stats()}, default=str, indent=1))

    async def _price_fallback(self) -> None:
        """Held tokens with no price (just restored) or no trade for 90s (quiet / migrated outside our stream):
        fetch a price from DexScreener every 15s so exits and equity keep working."""
        if not self.feed.realtime or self.now - self._last_price_fallback < 15:
            return
        stale = [m for m, p in self.positions.items()
                 if m in self.tokens and (not self.tokens[m].price_known or self.now - self.tokens[m].last_trade_ts > 90)]
        if not stale:
            return
        self._last_price_fallback = self.now
        from .curve import FINAL_V_TOKENS, INITIAL_V_SOL, INITIAL_V_TOKENS
        from ..clients import dexscreener

        try:
            pairs = await asyncio.to_thread(dexscreener.best_pair_by_mint, stale)
        except Exception:
            return
        k = INITIAL_V_SOL * INITIAL_V_TOKENS
        for m, pair in pairs.items():
            px = float(pair.get("priceNative") or 0)
            s = self.tokens.get(m)
            if px <= 0 or s is None:
                continue
            if pair.get("dexId") == "pumpfun":                   # still on the curve: exact reserves from k
                s.curve = Curve((k * px) ** 0.5, (k / px) ** 0.5)
            else:                                               # graduated: price only
                s.migrated = True
                s.curve = Curve(px * FINAL_V_TOKENS, FINAL_V_TOKENS)
            s.price_known = True
            s.peak_price = max(s.peak_price, px)
            s.last_trade_ts = self.now

    # ------------------------------------------------------------------ persistence (live)
    def save_state(self) -> None:
        if not self.persist:
            return
        from dataclasses import asdict

        self._last_state_save = self.now
        b = self.book
        # enough safety context per held token that exits work the same after a restart: who the creator
        # is (dev-sell exits, funding links) and what they've already sold
        tokens = {}
        for m in self.positions.keys() | {o["mint"] for o in self.unresolved.values()}:
            s = self.tokens.get(m)
            if s is not None:
                tokens[m] = {"launch": asdict(s.launch) if s.launch else None, "dev_sold": s.dev_sold}
        state = {"saved_at": time.time(), "mode": self.mode,
                 "book": {"sol": b.sol, "start_sol": b.start_sol, "day": b.day, "day_pnl": b.day_pnl,
                          "halted": b.halted, "closed": b.closed[-500:], "reserved": b.reserved,
                          "peak_equity": b.peak_equity, "max_dd_pct": b.max_dd_pct},
                 "positions": {m: asdict(p) for m, p in self.positions.items()},
                 "tokens": tokens, "unresolved": self.unresolved,
                 "defense": {"until": self.defense_until, "reason": self.defense_reason},
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
        # cash stays reserved only for buys that are still unresolved; anything else was in flight when the
        # process died and is accounted for by the wallet reconcile below
        open_buys = {o["mint"] for o in (d.get("unresolved") or {}).values() if o.get("side") == "buy"}
        self.book.reserved = {m: v for m, v in (b.get("reserved") or {}).items() if m in open_buys}
        self.book.peak_equity = b.get("peak_equity", self.book.start_sol)
        self.book.max_dd_pct = b.get("max_dd_pct", 0.0)
        self.defense_until = (d.get("defense") or {}).get("until", 0.0)
        self.defense_reason = (d.get("defense") or {}).get("reason", "")
        self.callouts.called.update(d.get("called", []))
        self.unresolved = dict(d.get("unresolved") or {})
        saved_tokens = d.get("tokens") or {}
        now = self.feed.now()

        def restore_token(m: str) -> TokenState:
            ctx = saved_tokens.get(m) or {}
            launch = Launch(**ctx["launch"]) if ctx.get("launch") else None
            s = self.tokens[m] = TokenState(m, launch, now)    # price unknown until its next trade
            if launch is not None and launch.dev_buy_tokens > 0:
                s.holders[launch.creator] = launch.dev_buy_tokens
            s.dev_sold = ctx.get("dev_sold", 0.0)
            s.decided = "entered"
            return s
        for m, pd in d["positions"].items():
            pd["exits"] = [tuple(x) for x in pd.get("exits") or []]
            self.positions[m] = SniperPosition(**pd)
            restore_token(m)
            await self._watch(m)
        for o in self.unresolved.values():                    # held until the chain says what happened
            self.pending.add(o["mint"])
            if o["mint"] not in self.tokens:
                restore_token(o["mint"])
                await self._watch(o["mint"])
        self.say("info", f"restored {len(self.positions)} open position(s) and {len(self.unresolved)} unresolved "
                         f"order(s) from {self.state_path.name}")
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
            if m in self.pending:                      # an unresolved order decides this one first
                continue
            have = balances.get(m, 0) / 1e6
            if have <= 0:
                pos.exits.append((self.now, "gone from wallet while offline", pos.tokens, 0.0))
                pos.tokens = 0
                self._close(pos, self.tokens[m])
                self.say("error", f"{pos.symbol}: not in wallet any more (sold/moved while offline) - closed at 0")
            elif abs(have - pos.tokens) / max(pos.tokens, 1e-9) > 0.01:
                self.say("info", f"{pos.symbol}: wallet holds {have:,.0f} tokens, saved {pos.tokens:,.0f} - corrected")
                pos.tokens = have
        orphans = [m for m, raw in balances.items()
                   if m.endswith("pump") and m not in self.positions and m not in self.pending and raw > 0]
        if orphans:
            self.say("error", f"{len(orphans)} pump token(s) in the wallet aren't tracked (sell manually): "
                              + ", ".join(o[:6] + "…" for o in orphans[:8]))
        self.save_state()
        await self.check_cash()

    async def check_cash(self) -> None:
        """Live: the wallet is the truth for cash. Less SOL than the ledger thinks (fees it didn't see, a
        withdrawal) lowers the ledger and counts against today's loss limit; more (a deposit) is NOT
        handed to the bot - its budget stays what you allocated."""
        wallet = getattr(self.ex, "wallet", None)
        if wallet is None:
            return
        expected = self.book.sol
        try:
            bal = await asyncio.to_thread(wallet.sol_balance)
        except Exception as e:
            self.say("error", f"wallet SOL check failed ({e}); ledger unchanged")
            return
        if self.pending or self.book.reserved or self.book.sol != expected:   # cash moved meanwhile: next time
            return
        gap = bal - self.book.sol
        if gap < -CASH_TOLERANCE_SOL:
            self.say("error", f"wallet holds {bal:.4f} SOL but the ledger expected {self.book.sol:.4f}: ledger "
                              f"lowered by {-gap:.4f} SOL (unbooked fees or a withdrawal), counted against today's "
                              "loss limit")
            self.book.sol = bal
            self.book.day_pnl += gap
            self.save_state()
        elif gap > CASH_TOLERANCE_SOL and not self._surplus_noted:
            self._surplus_noted = True
            self.say("info", f"wallet holds {gap:.4f} SOL more than the bot's ledger - extra SOL isn't given to the "
                             "bot automatically; its budget stays what it was allocated")

    # ------------------------------------------------------------------ graduation plays
    async def _maybe_late(self) -> None:
        L = self.p.late
        if not L.enabled or self.now - self._last_late_scan < 2 or self.entries_blocked():
            return
        self._last_late_scan = self.now
        en = self.p.entry
        for s in list(self.tokens.values()):
            if not s.decided or s.late_tried or s.mint in self.positions or s.mint in self.pending \
                    or s.mint in self.reviewing or not s.price_known:
                continue
            ok, why = evaluate_late_entry(s, self.now, L, {
                "max_bundle_pct": en.max_bundle_pct, "max_early_sold_ratio": en.max_early_sold_ratio,
                "creator_launches": len(self.creators.get(s.creator, ())),
                "max_creator_launches_24h": en.max_creator_launches_24h,
                "max_cluster_pct": en.funding.max_cluster_pct})
            if not ok:
                continue
            s.late_tried = True
            await self._enter(s, kind="late", score=max(s.score, 60.0), buy_sol=self.p.capital.buy_sol,
                              notes=[why], source="late")
            if self.entries_blocked():
                break

    # ------------------------------------------------------------------ callouts
    async def _maybe_callout(self) -> None:
        c = self.p.callouts
        if self._callout_inflight and self._callout_inflight not in self.pending:
            self._callout_inflight = ""                # its bag order resolved (filled or not)
        # account-level stops (loss limit, feed down, halt, pause) apply to callout bags like any entry
        if not c.enabled or self._callout_inflight or self._global_block() \
                or self.now - self.callouts.last_ts < c.interval_s:
            return
        if self.now - self._last_callout_scan < 5:      # no candidate last scan: look again in a few seconds,
            return                                       # not on every 1 s tick over every live token
        self._last_callout_scan = self.now
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
            held = self.positions.get(s.mint)
            if held and held.tokens * s.curve.price * self.sol_price.usd < c.min_hold_usd:
                continue            # holding a sub-$1 remainder: can't call it without stacking a second position
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

        async def publish() -> None:          # the card goes out only once we really hold the bag
            text = compose(s, self.now, usd)
            call = Callout(s.mint, s.symbol, self.now, s.curve.market_cap_sol, s.curve.price, text, score)
            self.callouts.add(call)
            self.stats["callouts"] += 1
            self.say("callout", text, s.mint)
            if c.auto_post == "telegram" and self.feed.realtime:
                async def post():
                    call.posted = "telegram" if await post_telegram(text, s.mint) else ""
                asyncio.create_task(post())

        held = self.positions.get(s.mint)
        if held and held.tokens * s.curve.price * usd >= c.min_hold_usd:
            await publish()
            return
        if open_bags >= c.max_open_bags:
            return
        self._callout_inflight = s.mint
        await self._buy(s, score, round(c.position_usd / usd, 5), [f"callout bag ${c.position_usd}"], "callout",
                        then=publish)

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

    def controls(self) -> list[dict]:
        out = []
        for key, typ, lo, hi, label, help_ in CONTROLS:
            node = self.p
            for k in key.split("."):
                node = node[k]
            out.append({"key": key, "type": typ, "min": lo, "max": hi, "label": label, "help": help_, "value": node})
        return out

    def set_control(self, key: str, value) -> str:
        """Validate and apply one dashboard setting. Returns '' or an error message."""
        spec = next((c for c in CONTROLS if c[0] == key), None)
        if spec is None:
            return f"unknown setting {key}"
        _, typ, lo, hi, label, _ = spec
        try:
            if typ == "bool":
                v = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif typ == "int":
                v = int(value)
            elif typ == "float":
                v = float(value)
            else:
                v = str(value)
                if v not in typ.split(":", 1)[1].split(","):
                    return f"{label}: must be one of {typ.split(':', 1)[1]}"
        except (TypeError, ValueError):
            return f"{label}: not a valid {typ}"
        if lo is not None and not (lo <= v <= hi):
            return f"{label}: must be between {lo} and {hi}"
        if key == "sizing.base_usd" and v > self.p.sizing.max_usd:
            return "base buy can't exceed the max buy"
        if key == "sizing.max_usd" and v < self.p.sizing.base_usd:
            return "max buy can't be below the base buy"
        *path, last = key.split(".")
        node = self.p
        for k in path:
            node = node[k]
        old, node[last] = node[last], v
        try:
            validate_sniper(self.p)                   # the whole config must still make sense together
        except ConfigError as e:
            node[last] = old
            return f"{label}: {e}"
        self.say("info", f"setting changed: {label} = {v}")
        return ""

    def save_controls(self, path: Path | None = None) -> str:
        """Write the dashboard-controllable settings into config/params.yaml (other keys untouched)."""
        import yaml

        path = path or ROOT / "config" / "params.yaml"
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
        data = data or {}
        sn = data.setdefault("sniper", {}) or {}
        data["sniper"] = sn
        for c in self.controls():
            *keys, last = c["key"].split(".")
            node = sn
            for k in keys:
                node = node.setdefault(k, {}) or {}
            node[last] = c["value"]
            # re-attach in case setdefault returned a fresh {} for a None value
            parent = sn
            for k in keys[:-1]:
                parent = parent[k]
            if keys:
                parent[keys[-1]] = node
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.say("info", f"settings saved to {path.name}")
        return ""

    def token_detail(self, mint: str) -> dict | None:
        s = self.tokens.get(mint)
        if s is None:
            return None
        ctx = self._ctx(s)
        feats = extract(s, self.now, ctx)
        p = self._predict(s) if self.model else None
        holders = sorted(s.holders.items(), key=lambda kv: -kv[1])[:12]
        pos = self.positions.get(mint)
        L = s.launch
        return {
            "mint": mint, "symbol": s.symbol, "name": L.name if L else "", "age_s": round(s.age(self.now)),
            "status": "held" if pos else (s.decided or "watching"), "score": s.score, "notes": s.score_notes,
            "curve_pct": s.curve.progress * 100, "mcap_usd": s.curve.market_cap_sol * self.sol_price.usd,
            "price": s.curve.price, "price_known": s.price_known,
            "socials": {"twitter": L.twitter if L else "", "telegram": L.telegram if L else "",
                        "website": L.website if L else ""},
            "creator": s.creator, "checklist": gate_checklist(s, self.now, self.p.entry, ctx),
            "p": p, "ev_pct": self._ev(p) if p is not None else None,
            "drivers": self.model.drivers(feats) if self.model else [],
            "features": {k: round(v, 4) for k, v in feats.items()},
            "cluster": s.cluster, "desk": s.desk,
            "holders": [{"wallet": w, "pct": t / 1e9 * 100, "dev": w == s.creator,
                         "funder": self.funders.get(w, ("", ""))[0]} for w, t in holders],
            "tape": [{"ts": t, "side": side, "sol": sol, "trader": tr} for t, _, side, sol, tr in list(s.trades)[-40:]][::-1],
            "chart": s.sparkline(150),
            "position": None if pos is None else {"source": pos.source, "entry": pos.entry_price,
                                                  "gain_pct": pos.gain_pct(s.curve.price), "cost_sol": pos.initial_cost_sol,
                                                  "proceeds_sol": pos.proceeds_sol, "initials": pos.initials_taken,
                                                  "exits": pos.exits},
            "audit": self._audit_view(mint),
        }

    def _audit_view(self, mint: str) -> dict | None:
        a = self.audit.get(mint)
        if a is None:
            return None
        gate, t0, p0, peak, trough, outcome = a
        return {"gate": gate, "since_s": round(self.now - t0), "price0": p0, "peak_x": peak / p0,
                "trough_x": trough / p0, "outcome": outcome or "open"}

    def model_card(self) -> dict | None:
        if not self.model:
            return None
        info = self.model.info or {}
        t = info.get("test", {})
        return {"trained_at": info.get("trained_at"), "label": info.get("label"), "n_train": info.get("n_train"),
                "n_test": info.get("n_test"), "auc": t.get("auc"), "brier_skill": t.get("brier_skill"),
                "base_rate": t.get("base_rate"), "top_decile_rate": t.get("top_decile_rate"),
                "calibration": t.get("calibration", []), "weights": info.get("weights", [])[:12],
                "temperature": info.get("temperature")}

    def _analytics_args(self):
        key = (len(self.book.closed), sum(g["n"] for g in self.gate_stats.values()))
        fresh = self._analytics and self._analytics[0] == key and self.now - self._analytics[1] < 60
        args = (list(self.book.closed), list(self.book.equity_hist), self.book.start_sol,
                self.p.capital.max_drawdown_pct, self.gate_audit(), self.model_card())
        return key, fresh, args

    def _analytics_kw(self) -> dict:
        return {"fee_pct": self.fee, "observed_max_dd_pct": self.book.max_dd_pct}

    def analytics(self) -> dict:
        """Recomputed when a trade closes or the gate audit moves, else at most once a minute (feed time)."""
        from .analytics import compute

        key, fresh, args = self._analytics_args()
        if fresh:
            return self._analytics[2]
        out = compute(*args, **self._analytics_kw())
        self._analytics = (key, self.now, out)
        return out

    async def analytics_async(self) -> dict:
        """Dashboard path: copies the inputs here, computes in a worker thread so trading isn't held up."""
        from .analytics import compute

        key, fresh, args = self._analytics_args()
        if fresh:
            return self._analytics[2]
        now = self.now
        out = await asyncio.to_thread(compute, *args, **self._analytics_kw())
        self._analytics = (key, now, out)
        return out

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
        if self._summary_cache is None or self._summary_cache[0] != len(closed):   # only changes when a trade closes
            st = self._stats(closed)
            st["by_source"] = {src: self._stats([c for c in closed if c["source"].split(":")[0] == src])
                               for src in sorted({c["source"].split(":")[0] for c in closed})}
            self._summary_cache = (len(closed), st)
        out = dict(self._summary_cache[1])
        out.update({"launches": self.stats["launches"], "entries": self.stats["entries"],
                    "equity_sol": self.equity(), "start_sol": self.book.start_sol})
        return out

    def desk_stats(self) -> dict:
        d = self.desk
        return {"enabled": bool(d and d.enabled), "calls": d.calls if d else 0,
                "cost_usd": round(d.cost_usd(), 4) if d else 0.0,
                "model": self.p.desk.model, "personas": list(self.p.desk.personas)}

    def snapshot(self) -> dict:
        if self._snap_cache and self._snap_cache[0] == self.now:      # several dashboard tabs: build once per tick
            return self._snap_cache[1]
        snap = self._snapshot()
        self._snap_cache = (self.now, snap)
        return snap

    def _snapshot(self) -> dict:
        # radar: rank cheaply first, then build the expensive fields (sparklines) for the 40 shown only
        ranked = sorted((s for s in self.tokens.values() if s.mint not in self.positions),
                        key=lambda s: (s.decided != "" and s.mint not in self.reviewing, -s.score, s.age(self.now)))[:40]
        watching = []
        for s in ranked:
            status = "AI desk reviewing" if s.mint in self.reviewing else (s.decided or "watching")
            L = s.launch
            watching.append({
                "mint": s.mint, "symbol": s.symbol, "age": round(s.age(self.now)), "progress": s.curve.progress,
                "mcap_sol": s.curve.market_cap_sol, "buyers": len(s.buyers), "score": s.score,
                "notes": s.score_notes[:3], "status": status, "socials": len(s.socials),
                "links": int(bool(L and L.twitter)) + int(bool(L and L.telegram)) + int(bool(L and L.website)),
                "lk": [int(bool(L and L.twitter)), int(bool(L and L.telegram)), int(bool(L and L.website))],
                "p": s.p, "bundle_pct": s.bundle_pct(), "dev_pct": s.dev_initial_pct(), "spark": s.sparkline(40),
            })
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
                "source": pos.source, "desk": pos.desk, "p": pos.p,
                "spark": [p for t, p, *_ in s.trades if t >= pos.opened_at - 30][-120:],
                "entry_idx": sum(1 for t, *_ in s.trades if pos.opened_at - 30 <= t < pos.opened_at),
            })
        return {
            "type": "snapshot", "now": self.now, "mode": self.mode, "paused": self.paused, "halted": self.book.halted,
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
            "defense": {"on": self._defensive(), "reason": self.defense_reason,
                        "minutes_left": max(0, round((self.defense_until - self.now) / 60))},
            "model": None if not self.model else {"trained_at": (self.model.info or {}).get("trained_at"),
                                                   "auc": (self.model.info or {}).get("test", {}).get("auc")},
            "strategies": {"sniper": self.p.entry.enabled, "copy": self.p.copy.enabled,
                           "callouts": self.p.callouts.enabled, "late": self.p.late.enabled},
            "tracked_tokens": len(self.tokens),
            "feed": {"realtime": self.feed.realtime, "host": getattr(self.feed, "host", ""),
                     "degraded": bool(getattr(self.feed, "degraded", False)),
                     "connected": getattr(self.feed, "ws", True) is not None,
                     "last_event_age_s": round(self.now - self.last_event, 1) if self.last_event else None},
        }

    def save_report(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"summary": self.summary(), "rejects": dict(self.rejects),
                                    "closed": self.book.closed, "params": dict(self.p)}, indent=1, default=str))
