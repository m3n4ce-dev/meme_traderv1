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
import heapq
import json
import secrets
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from ..config import EXAMPLE, ROOT, ConfigError, example_help, validate_sniper
from ..journal import DATA, record
from .callouts import Callout, CalloutBook, compose, eligible, is_red_flag, post_telegram
from .calls import CallLedger, LedgerError
from .exitlab import ExitLab
from .copytrade import LeaderBook
from .curve import Curve
from .events import Event, Funding, Health, Launch, Metadata, Migration, Social, Tick, Trade, dumps
from .execution import SniperFill
from .features import extract
from .funding import FundingResolver, cluster_report, cohort
from .notify import Notifier
from .predictor import LogisticModel, expected_value_pct, kelly
from .signals import CallerBook
from .sizing import SolPrice, size_usd, strength
from .strategy import (SKIP_FOR_GOOD, SniperPosition, evaluate_entry, evaluate_exit, evaluate_late_entry,
                       evaluate_late_exit, evaluate_manual_exit, evaluate_ride_exit, exit_watch, gate_checklist,
                       initials_fraction, late_checklist, late_dev_ok)
from .tracker import TokenState

# handed-over positions: half out at 2x, trail the rest 30% off its peak once it's run 30%, stop 40% down
RIDE_DEFAULTS = {"take_x": 2.0, "take_frac": 0.5, "trail_pct": 30.0, "trail_arm_pct": 30.0, "stop_pct": 40.0}
DUST_SOL = 0.0005
TOKEN_ACCOUNT_RENT = 0.00203928       # refundable SOL locked in each new token account (live)
UNRESOLVED_EXPIRY_S = 150             # a Solana tx can't land once its blockhash expires (~60-90 s)
CASH_TOLERANCE_SOL = 0.002            # ledger vs wallet SOL difference that's still just rounding/timing


def reason_key(note: str) -> str:
    """'dev bought 7.1% > 6%' -> 'dev bought' (for grouping reject stats)."""
    return re.split(r"[\d(]", note, maxsplit=1)[0].strip(" :-") or note


# exits on a falling price or a rug signal: these use execution.urgent_sell_slippage_steps (a failed first try in a dump
# costs a whole extra landing delay: seen 2026-10-05, 24 graduation exits decided at -3..-18% filled at -40..-74%)
URGENT_EXITS = ("dev sold", "stop", "momentum decay", "insider", "cluster", "kill switch", "trail")

# Settings the dashboard may change at runtime: (key under sniper, type, min, max, label, help)
# The risk dial scales exposure around the configured settings (= level 2). It never touches the kill switch,
# the stop losses, the entry rules or the 3%-of-curve liquidity cap. (name, size x, positions x, daily loss x)
RISK_LEVELS = {1: ("Cautious", 0.5, 0.5, 0.5), 2: ("Normal", 1.0, 1.0, 1.0), 3: ("Bold", 1.5, 1.5, 1.5),
               4: ("Aggressive", 2.0, 2.0, 2.0), 5: ("Max", 3.0, 2.5, 3.0)}
RISK_KEYS = ("sizing.base_usd", "sizing.max_usd", "capital.max_open_positions", "capital.daily_loss_limit_sol")

CONTROLS = [
    ("entry.enabled", "bool", None, None, "Sniper entries", "Early-entry sniper buys"),
    ("copy.enabled", "bool", None, None, "Copy trading", "Mirror leader wallets"),
    ("xchain.enabled", "bool", None, None, "Other chains (paper)", "Young DEX coins on BNB Chain, Base and Solana, paper money"),
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


# Every other scalar setting in these sections can be changed live from the dashboard's "All settings" (owner
# only: the AI agent keeps to CONTROLS and its ceilings). Lists, endpoints, keys and paths are config-file only.
ADVANCED_SECTIONS = ("capital", "sizing", "entry", "exit", "late", "callouts", "copy", "execution", "risk_adapt",
                     "predict", "signals", "manual", "xchain")
ADVANCED_SKIP = {"xchain.gas_usd.bsc", "xchain.gas_usd.base", "xchain.gas_usd.eth", "xchain.gas_usd.solana",
                 "capital.starting_sol", "predict.enabled", "predict.model_path", "predict.checkpoints_s",
                 "sizing.enabled", "copy.use_own_exits"}
ADVANCED_ENUMS = {"late.entry_mode": ("rule", "window")}
_EXAMPLE_SNIPER: dict | None = None


def _example_sniper() -> dict:
    global _EXAMPLE_SNIPER
    if _EXAMPLE_SNIPER is None:
        import yaml

        _EXAMPLE_SNIPER = (yaml.safe_load(EXAMPLE.read_text()) or {}).get("sniper") or {}
    return _EXAMPLE_SNIPER


def _leaves(node: dict, prefix: str = ""):
    for k, v in node.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _leaves(v, path + ".")
        else:
            yield path, v


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
        self.deposits: list[tuple[float, float]] = []     # paper top-ups: (ts, SOL)
        self.kill_base = 0.0     # the kill switch's drawdown is measured from this; 0 = the starting balance

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
        # risk dial: the configured exposure is "Normal"; the dial level scales it (dashboard, or Claude with approval)
        self.risk_base = {k: self._get(k) for k in RISK_KEYS}
        self._advanced_set: set[str] = set()               # "All settings" changed this run (saved by Save)
        self.risk_level = 2
        lvl = int((self.p.get("risk") or {}).get("level", 2))
        if lvl != 2:
            self._apply_risk(lvl)
        # one process = one market reading: whose trades are price-only (pump.fun's Mayhem agent by default)
        TokenState.NON_ORGANIC = frozenset((self.p.get("market") or {}).get("non_organic_wallets") or [])
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
        # coin families: launches sharing a ticker or a name (pump.fun "vamps" copy a running coin's name or
        # ticker). The first one launched is the OG. key -> deque[(launch ts, mint)], mint -> (ts, symbol, name)
        self.families: dict[str, deque] = defaultdict(deque)
        self.fam_meta: dict[str, tuple] = {}
        self.fam_since = time.time()
        self.rejects: Counter = Counter()
        self.stats = Counter()
        self.log: deque = deque(maxlen=300)
        # the Desk: what the bots are thinking (live only; never read by a trading decision)
        self.thoughts: deque = deque(maxlen=200)
        self.late_view: list[dict] = []
        self._think: dict[str, tuple[str, str, float]] = {}       # mint -> (verdict, why, when said)
        self._last_think = 0.0
        self.desk_reviews: deque = deque(maxlen=20)
        self.desk_vote_s: deque = deque(maxlen=200)     # how long each desk review took (wall clock), for the Desk and HQ
        self._day_stop_said = False                      # the daily loss limit was announced today
        self.desk_failures = 0                                   # reviews in a row where no persona answered
        self.manual_queue: dict[str, tuple[float, float]] = {}   # mint -> (SOL, when): buy at its first price
        self.hand_after: dict[str, float] = {}       # "Buy & give to bots": mint -> when; handed over once the buy fills
        self.kol_tape: deque = deque(maxlen=600)     # trades by KOLs and the wallet study's wallets, any coin (Charts tab)

        self._kol_first: dict[tuple[str, str], float] = {}   # (wallet, mint) -> its first buy: how long they held
        from .memory import Memory

        self.memory = Memory(DATA / "memory.json" if feed.realtime else None)   # what you've fed the desk
        self.desk_error = ""
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
        # every call, hash-chained (data/calls.jsonl): yours from the 📣 button, plus the bot's own entries
        self.ledger = CallLedger(DATA / "calls.jsonl" if self.persist else None)
        # other exit rules, shadowing every bot entry on the same prices (measurement only; data/exit_lab.jsonl)
        self.lab = ExitLab(DATA / "exit_lab.jsonl" if self.persist else None, self.fee)
        self._last_ledger_tick = 0.0
        self._last_ledger_dex = 0.0
        self._ledger_busy = False
        self.orders: dict[str, dict] = {}                  # your limit orders and alerts, by id
        self.away = False                                  # away mode: the bots manage your positions
        self.migrations: deque = deque(maxlen=120)         # (ts, mint, symbol, last curve market cap USD)
        self.descriptions: dict[str, str] = {}             # the coin's own description (its metadata; creator-written)
        self.linked_x: dict[str, dict] = {}                # what a coin's X link is (read near the graduation window)
        self._x_next_at, self._x_day = 0.0, ("", 0)
        self.xfeed = None                                  # the dashboard's X feed (posts naming a coin), when it runs
        self.note_waiting: dict[str, set] = {}             # memory item id -> personas still writing a reply
        self.note_stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "error": ""}
        self.huddles: deque = deque(maxlen=20)              # the team's meetings (desk.huddle_minutes; Desk tab)
        self._last_huddle, self._huddling, self.huddle_error = 0.0, False, ""
        self.plan: dict = {}                                 # the team's growth plan, updated at every huddle
        from .lab import Lab
        self.xlab = Lab(DATA / "lab" if self.persist else None)   # the team's experiments on the recorded market
        from .xchain import XChain
        self.xchain = XChain(self, DATA / "xchain.json" if self.persist else None)   # paper trades on other chains
        self._lab_task: asyncio.Task | None = None
        self._lab_check, self._lab_auto_at = 0.0, time.time() + 600
        from . import kols as kolmod
        self.kols: dict = kolmod.load(DATA / "kols.json") if self.persist else {"kols": {}}   # named on the charts
        if self.persist and (DATA / "desk_plan.json").exists():
            try:
                self.plan = json.loads((DATA / "desk_plan.json").read_text())
            except (OSError, ValueError):
                pass
        if self.persist and (DATA / "huddles.jsonl").exists():
            try:
                for line in (DATA / "huddles.jsonl").read_text().splitlines()[-20:]:
                    self.huddles.append(json.loads(line))
                if self.huddles:
                    self._last_huddle = self.huddles[-1]["ts"]
            except (OSError, ValueError):
                pass
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
        # per minute: [ts, launches, trades, SOL bought, SOL sold, graduations] (dashboard "market pulse")
        self.pulse: deque = deque(maxlen=90)
        self.pulse_since = 0.0
        self._last_health = 0.0
        self.recorded_degraded = ""                        # replays: the recording says the live feed was bad here
        self.deferred: list = []                           # paper orders still "in flight" (execution.paper_delay_s)
        self._order_seq = 0
        self.last_slot = 0                                 # newest chain slot seen on the feed
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
        return self.book.sol + sum(pos.tokens * self._mark(m, pos) for m, pos in self.positions.items()) + self.xchain.value_sol()

    def _pulse_row(self) -> list:
        if not self.pulse_since:
            self.pulse_since = self.now                       # this run's first minute is only part of one
        minute = int(self.now // 60) * 60
        if not self.pulse or self.pulse[-1][0] != minute:
            self.pulse.append([minute, 0, 0, 0.0, 0.0, 0])
        return self.pulse[-1]

    def deposit_paper(self, sol: float) -> dict:
        """Add pretend SOL to a paper account. It counts as starting capital, not profit: the start, the peak
        and the equity history all move up with the cash, so P&L, returns and drawdown read the same."""
        if self.mode.startswith("live"):
            raise ValueError("deposits are paper only - the live balance is what the wallet holds")
        sol = float(sol)
        if not 0 < sol <= 10_000:
            raise ValueError("deposit must be more than 0 and at most 10000 SOL")
        b = self.book
        b.sol += sol
        b.start_sol += sol
        b.peak_equity += sol
        if b.kill_base:
            b.kill_base += sol
        b.equity_hist = deque(((t, e + sol) for t, e in b.equity_hist), maxlen=b.equity_hist.maxlen)
        b.deposits.append((self.now, sol))
        self._snap_cache = self._analytics = self._summary_cache = None
        self.say("info", f"paper deposit +{sol:g} SOL: cash {b.sol:.3f} SOL, starting balance now {b.start_sol:g} SOL")
        return {"cash_sol": round(b.sol, 6), "start_sol": round(b.start_sol, 6), "equity_sol": round(self.equity(), 6)}

    def reset_paper(self) -> str:
        """Start the paper account over at capital.starting_sol: cash, today's P&L, the peak and the closed
        trades on the dashboard. The trade files keep every past trade. '' or why not."""
        if self.mode.startswith("live"):
            return "paper only"
        if self.positions or self.pending or self.deferred:
            return "close the open positions first"
        start = float(self.p.capital.starting_sol)
        self.book = Book(start)
        self.book.day = time.strftime("%Y-%m-%d", time.gmtime(self.now))
        self.defense_until, self.defense_reason = 0.0, ""
        self._snap_cache = self._analytics = self._summary_cache = None
        self.save_state()
        self.say("info", f"paper account started over at {start:g} SOL")
        return ""

    def _global_block(self, manual: bool = False) -> str:
        """Account-level stops that apply to EVERY entry source (sniper, copy, graduation, callout). Your own
        manual trades skip only "paused", which is about the bot's automatic entries."""
        if self.book.halted:
            return "halted: " + self.book.halted
        if self.paused and not manual:
            return "paused"
        if getattr(self.feed, "degraded", False):
            why = getattr(self.feed, "degraded_reason", "") or f"on backup feed {self.feed.host} (no trade data)"
            return f"{why} - entries paused"
        if self.recorded_degraded:                     # replay of a stretch the live bot couldn't trust either
            return f"recorded feed degraded ({self.recorded_degraded}) - entries paused"
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
        why = self._global_block(manual=source == "manual")
        if why:
            return why
        if source not in ("callout", "manual"):           # your own trades don't take the bot's seats
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

    def _pinned(self) -> set[str]:
        """Coins to keep priced: open orders and alerts, and curve-stage calls still being scored."""
        out = {o["mint"] for o in self.orders.values() if o["status"] == "open"}
        out.update(c["mint"] for c in self.ledger.open_calls(self.now) if c.get("stage") == "curve")
        out.update(self.lab.mints())
        return out

    async def _unwatch(self, mint: str) -> None:
        if mint in self._pinned():
            return
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
        xtask = asyncio.create_task(self.xchain.run()) if self.feed.realtime and self.persist else None   # other chains (paper)
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
            if xtask:
                xtask.cancel()
                await self.xchain.close()
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
        if self.deferred:                                  # paper orders whose landing time has come
            await self._settle_deferred()
        if not isinstance(e, Tick):
            self.last_event = self.now
            if self.record_file:
                self.record_file.write(dumps(e) + "\n")
            if isinstance(e, (Trade, Migration)):
                row = self._pulse_row()
                if isinstance(e, Migration):
                    row[5] += 1
                else:
                    row[2] += 1
                    row[3 if e.side == "buy" else 4] += e.sol
        if isinstance(e, Launch):
            await self._on_launch(e)
        elif isinstance(e, Metadata):
            s = self.tokens.get(e.mint)
            if s is not None and s.launch is not None:
                s.launch.twitter, s.launch.telegram, s.launch.website = e.twitter, e.telegram, e.website
        elif isinstance(e, Trade):
            if e.slot > self.last_slot:
                self.last_slot = e.slot
            await self._on_trade(e)
        elif isinstance(e, Migration):
            s = self.tokens.get(e.mint)
            if s:
                if not s.migrated:
                    self.migrations.appendleft((self.now, s.mint, s.symbol, s.market_cap_sol * self.sol_price.usd))
                s.migrated = True
                await self._evaluate(s)
        elif isinstance(e, Social):
            await self._on_social(e)
        elif isinstance(e, Health):
            if not self.feed.realtime:                  # replay: act like the live bot did at this moment
                self.recorded_degraded = e.degraded
                if e.sol_usd > 0:
                    self.sol_price.usd = e.sol_usd
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
        self._pulse_row()[1] += 1
        self.creators[e.creator].append(e.ts)
        self.symbols[e.symbol.upper()].append(e.ts)
        self._fam_add(e.mint, e.ts, e.symbol, e.name)
        s = TokenState(e.mint, e, e.ts)
        s.on_launch(e)
        self.tokens[e.mint] = s
        await self._watch(e.mint)
        if self.feed.realtime and e.uri and not (e.twitter or e.telegram or e.website):
            asyncio.create_task(self._enrich(e))

    async def _enrich(self, e: Launch) -> None:
        from .feeds import fetch_metadata, ipfs_urls

        md = {}
        for url in ipfs_urls(e.uri)[:3]:                 # ipfs.io rate-limits busy IPs: try other gateways
            md = await fetch_metadata(url, ("twitter", "telegram", "website", "description"))
            if md:
                break
        if md:
            e.twitter, e.telegram, e.website = md.get("twitter", ""), md.get("telegram", ""), md.get("website", "")
            if md.get("description"):
                if len(self.descriptions) > 5000:       # only coins still tracked
                    self.descriptions = {m: d for m, d in self.descriptions.items() if m in self.tokens}
                self.descriptions[e.mint] = " ".join(md["description"].split())
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
        kw = self._known_wallets().get(e.trader)        # a KOL or a study wallet: the Charts tab's tracker
        if kw:
            self._note_known(e, s, *kw)
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
        # the bots' own results only: callout bags aren't trades, and the owner's manual trades (even ones handed to
        # the bots) are the owner's decisions, so their losses must not slow the bots down (or their wins speed them up)
        # (other-chain paper trades are a different game: they don't slow the pump.fun bots either)
        recent = [c for c in self.book.closed if c["source"] not in ("callout", "manual", "chains")][-R.lookback_trades:]
        streak = 0
        for c in reversed(recent):
            if c["pnl"] > 0:
                break
            streak += 1
        window = sum(c["pnl"] for c in recent)
        if streak >= R.loss_streak or (len(recent) >= R.lookback_trades and window <= -R.window_loss_sol):
            if not self._defensive():
                why = (f"{streak} losses in a row by the bots" if streak >= R.loss_streak
                       else f"the bots' last {len(recent)} trades {window:+.3f} SOL")
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
        saved = [{"title": n.get("title", ""), "your_note": n.get("note", ""), "summary": n.get("summary", "")[:300]}
                 for n in self.memory.for_mint(s.mint)]
        if saved:
            extra = {**extra, "owner_notes": saved}
        fam = self.family(s.mint)
        if fam:                      # copies of one name fight over the same buyers: which one is this?
            extra = {**extra, "name_family": {"coins_sharing_name_or_ticker_6h": fam["n"], "is_first_launched": fam["og"],
                                              "launch_order": fam["rank"], "seconds_after_first": fam["after_og_s"],
                                              "biggest_now": fam.get("lead_symbol"), "biggest_mcap_usd": fam.get("lead_mcap_usd")}}
        t0 = time.time()
        try:
            v = await self.desk.review(snapshot_for(s, self.now, kind, extra),
                                       {"narrative": {"narrative_context": self.narrative_context(s)}})
        finally:
            self.reviewing.discard(s.mint)
        if self.feed.realtime:
            self.desk_vote_s.append(round(time.time() - t0, 2))
        s.desk = v.summary
        self.say("desk", f"{s.symbol}: {v.summary}", s.mint, votes=[vars(x) for x in v.votes])
        self.desk_reviews.append({"ts": self.now, "mint": s.mint, "symbol": s.symbol, "kind": kind,
                                  "approve": v.approve, "summary": v.summary, "votes": [vars(x) for x in v.votes]})
        if v.votes and all(x.error for x in v.votes):
            from .desk import friendly_error

            self.desk_failures += 1
            self.desk_error = friendly_error(v.votes[0].error)
            if self.desk_failures >= 3:                  # a broken desk would silently pass every trade
                self.set_desk(False, who="bot")
                self.say("error", f"AI desk put to rest after 3 failed reviews: {self.desk_error}. "
                                  "Entries continue on the rules alone.")
        else:
            self.desk_failures, self.desk_error = 0, ""
        if not v.approve:
            self.rejects["desk passed"] += 1
            if kind == "sniper":
                s.decided = "rejected: desk passed"
                self._audit_start(s, "desk passed")
            elif v.votes and not all(x.error for x in v.votes):
                # follow what the passed coin does next, split by how many personas said buy, so the gate
                # audit shows whether an outvoted majority (3 of 4) does better than a unanimous pass
                nb = sum(1 for x in v.votes if x.vote == "buy" and not x.error)
                label = {"late": "graduation"}.get(kind, kind)
                self._audit_start(s, f"AI desk passed ({label}): {nb} of {len(v.votes)} said buy")
                if kind == "late" and self.feed.realtime:   # what the pass would have made, on the bot's exits
                    self.lab.start(s.mint, s.symbol, "desk-pass", s.curve.price, self.now, self.p, only=("as now",))
            return
        moved = (s.curve.price / start_price - 1) * 100 if start_price else 0
        notes = notes + [f"desk x{v.size_mult:.2f}"]
        size = self._size(s, kind, score, buy_sol, v.size_mult, notes)
        why = self.entries_blocked(size) or ("already held" if s.mint in self.positions else "") or \
            ("the curve is too thin to size a buy" if size <= 0 else "") or \
            (f"price moved {moved:+.0f}% during review" if moved > self.p.desk.max_price_move_pct else "") or \
            ("dev sold" if (not late_dev_ok(s, self.p.late) if kind == "late" else s.dev_sold) else "")
        if why:
            self.say("info", f"desk approved {s.symbol} but skipped: {why}", s.mint)
            if kind == "sniper" and not why.startswith(("max positions", "paused", "low SOL")):
                s.decided = "skipped: " + why          # don't pay for a fresh desk review every tick
            return
        await self._buy(s, score, size, notes, source, leader)

    async def _buy(self, s: TokenState, score: float, sol: float, notes: list[str], source: str = "sniper",
                   leader: str = "", then=None, add: bool = False) -> None:
        """Authorize with the final size, reserve the cash, then send. Live, the order runs as its own task,
        so the feed keeps being read (other tokens' stops and dev sells) while it confirms; backtests run it
        inline so replays stay deterministic. `then`: coroutine function to run after a successful fill."""
        if s.mint in self.pending or (s.mint in self.positions and not add):   # one position per mint; adds merge
            return
        z = self.p.sizing
        if source == "manual":
            if sol > self._manual_cfg().max_sol + 1e-9:
                self.say("error", f"manual size {sol} SOL over manual.max_sol - refused", s.mint)
                return
        elif z.enabled and sol * self.sol_price.usd > z.max_usd + 1e-6:     # hard cap, whatever asked for more
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
        meta = {"quote": s.curve.price, "decided": self.now, "slot": self.last_slot, "add": bool(add), "amm": bool(s.migrated),
                "dev_sold": s.dev_sold}
        if not add and source != "callout":
            meta["feat"] = self._entry_features(s, source)
        if self._paper_delay() > 0:                       # paper: lands later, at the price it lands at
            self._defer({"side": "buy", "mint": s.mint, "score": score, "sol": sol, "notes": list(notes),
                         "source": source, "leader": leader, "then": then, **meta})
            return
        await self._dispatch(self._run_buy(s, score, sol, notes, source, leader, then, meta))

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

    async def _run_buy(self, s: TokenState, score, sol, notes, source, leader, then, meta=None) -> None:
        try:
            fill = await self.ex.buy(s.mint, s.curve, sol,
                                     self.p.callouts.priority_fee_sol if source == "callout" else None)
        except Exception as e:                            # an executor bug must not leave cash reserved forever
            fill = SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        if fill.unknown:                                  # sent, but did it land? keep the cash and the mint held
            self._book_fees_lost(fill, s)
            self._track_unresolved(fill.signature, {"mint": s.mint, "side": "buy", "sol": sol, "score": score,
                                                    "add": bool((meta or {}).get("add")),
                                                    "notes": list(notes), "source": source, "leader": leader})
            self.say("error", f"buy {s.symbol}: sent ({fill.signature[:8]}…) but its outcome is unknown - "
                              "its cash stays reserved until the chain says", s.mint)
            return
        self.book.reserved.pop(s.mint, None)
        self.pending.discard(s.mint)
        self._apply_buy(s, fill, score, notes, source, leader, meta)
        if fill.ok and then is not None:
            try:
                await then()
            except Exception as e:                        # e.g. posting a callout card - the buy itself stands
                self.say("error", f"after-buy step for {s.symbol} failed: {e!r}", s.mint)

    # ------------------------------------------------------------------ paper orders in flight
    def _paper_delay(self) -> float:
        """execution.paper_delay_s: seconds from deciding to landing, in paper and replays (live really waits).
        It should be the feed's lag behind the chain plus the time a transaction takes to land."""
        return 0.0 if self.mode.startswith("live") else float(self.p.execution.get("paper_delay_s", 0) or 0)

    def _defer(self, order: dict) -> None:
        self._order_seq += 1
        heapq.heappush(self.deferred, (self.now + self._paper_delay(), self._order_seq, order))

    async def _settle_deferred(self) -> None:
        while self.deferred and self.deferred[0][0] <= self.now:
            _, _, o = heapq.heappop(self.deferred)
            if o["side"] == "buy":
                await self._land_buy(o)
            else:
                await self._land_sell(o)

    async def _land_buy(self, o: dict) -> None:
        """A paper buy reaches the chain: at the price it finds there, or not at all if that price ran past
        execution.slippage_pct (the transaction fails and still costs its fee), as a live buy would."""
        s = self.tokens.get(o["mint"])
        prio = self.p.callouts.priority_fee_sol if o["source"] == "callout" else None
        tx = (prio if prio is not None else self.p.execution.priority_fee_sol) + 0.000005
        tol = float(self.p.execution.slippage_pct)
        if s is None or not s.price_known or (s.migrated and not o.get("amm")):
            fill = SniperFill(False, error="token gone before the order landed", fees_lost=tx)
        elif s.curve.price > o["quote"] * (1 + tol / 100):
            fill = SniperFill(False, fees_lost=tx, error=f"price up {(s.curve.price / o['quote'] - 1) * 100:.0f}% "
                                                         f"before it landed (slippage limit {tol:g}%)")
        else:
            try:
                fill = await self.ex.buy(o["mint"], s.curve, o["sol"], prio)
            except Exception as e:
                fill = SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        self.book.reserved.pop(o["mint"], None)
        self.pending.discard(o["mint"])
        if s is None:
            self.book.sol -= fill.fees_lost
            self.book.day_pnl -= fill.fees_lost
            return
        self._apply_buy(s, fill, o["score"], o["notes"], o["source"], o["leader"], o)
        if fill.ok and o.get("then") is not None:
            try:
                await o["then"]()
            except Exception as e:
                self.say("error", f"after-buy step for {s.symbol} failed: {e!r}", s.mint)

    async def _land_sell(self, o: dict) -> None:
        """A paper sell reaches the chain. Below its slippage floor it fails (fee paid) and is re-sent with the
        next step of execution.sell_slippage_steps / sell_priority_fee_steps, like the live executor."""
        s, pos = self.tokens.get(o["mint"]), self.positions.get(o["mint"])
        if s is None or pos is not o["pos"]:
            self.pending.discard(o["mint"])
            return
        x = self.p.execution
        steps = list(o.get("steps") or x.sell_slippage_steps)
        prios = [self.p.callouts.priority_fee_sol] * len(steps) if pos.source == "callout" else \
            list(x.sell_priority_fee_steps) + [x.sell_priority_fee_steps[-1]] * len(steps)
        k = o["attempt"]
        if s.curve.price < o["quote"] * (1 - steps[k] / 100):
            lost = prios[k] + 0.000005
            self._book_fees_lost(SniperFill(False, fees_lost=lost), s)
            pos.failed_fees_sol += lost
            self.stats["failed_sell_attempts"] += 1
            if k + 1 < len(steps):
                self._defer({**o, "attempt": k + 1, "quote": s.curve.price})
                return
            self.pending.discard(o["mint"])
            self.say("error", f"sell {s.symbol}: all {len(steps)} attempts failed - the price fell faster than "
                              f"the slippage steps", s.mint)
            return
        try:
            fill = await self.ex.sell(o["mint"], s.curve, o["tokens"], prios[k])
        except Exception as e:
            fill = SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        self.pending.discard(o["mint"])
        self._apply_sell(s, pos, fill, o["reason"], {"quote": o["first_quote"], "decided": o["decided"],
                                                     "slot": o.get("slot")})

    def _book_fees_lost(self, fill: SniperFill, s: TokenState) -> None:
        """Failed transactions that landed still burned fees: real cash, and a real loss for today."""
        if fill.fees_lost > 0:
            self.book.sol -= fill.fees_lost
            self.book.day_pnl -= fill.fees_lost
            self.say("error", f"{s.symbol}: failed transaction(s) still cost {fill.fees_lost:.6f} SOL in fees", s.mint)

    def _merge_add(self, s: TokenState, pos: SniperPosition, fill: SniperFill) -> None:
        """More SOL into an open position: one position, a token-weighted average entry, the added cost in its
        P&L. Its exits stay whatever they were (the bot's rules, or the stop/take profit you set)."""
        held = pos.tokens + fill.tokens
        pos.entry_price = (pos.entry_price * pos.tokens + fill.price * fill.tokens) / held if held else fill.price
        pos.tokens, pos.initial_tokens = held, pos.initial_tokens + fill.tokens
        pos.cost_sol += fill.sol
        pos.initial_cost_sol += fill.sol
        pos.rent_sol += fill.rent
        pos.failed_fees_sol += fill.fees_lost
        pos.adds = (pos.adds or []) + [(self.now, fill.sol, fill.tokens, fill.price)]
        if pos.manual and pos.manual.get("tp_done"):
            pos.manual["tp_done"] = False                     # a fresh take profit applies to the bigger bag
        self.save_state()
        self.say("buy", f"{s.symbol} +{fill.sol:.3f} SOL added: {pos.initial_cost_sol:.3f} SOL in now [{pos.source}]",
                 s.mint, signature=fill.signature, timing=fill.timing)

    def _apply_buy(self, s: TokenState, fill: SniperFill, score, notes, source, leader, meta=None) -> None:
        self._book_fees_lost(fill, s)
        if not fill.ok:
            self.stats["failed_buys"] += 1
            self.say("error", f"buy {s.symbol} failed: {fill.error}", s.mint, timing=fill.timing)
            self.save_state()
            return
        self.book.sol -= fill.sol + fill.rent             # rent is cash locked in the token account until reclaimed
        if (meta or {}).get("add") and s.mint in self.positions:
            self._merge_add(s, self.positions[s.mint], fill)
            return
        self.positions[s.mint] = SniperPosition(
            mint=s.mint, symbol=s.symbol, opened_at=self.now, entry_price=fill.price, tokens=fill.tokens,
            initial_tokens=fill.tokens, cost_sol=fill.sol, initial_cost_sol=fill.sol, score=score,
            peak_price=fill.price, exits=[], source=source, leader=leader, desk=getattr(s, "desk", ""),
            p=s.p, trough_price=fill.price, rent_sol=fill.rent,
            entry_quote=(meta or {}).get("quote", 0.0),
            entry_delay_s=round(self.now - (meta or {}).get("decided", self.now), 3),
            failed_fees_sol=fill.fees_lost,
            bot="ride" if source == "manual" and self.away else "", feat=(meta or {}).get("feat"),
            dev_sold_at_entry=(meta or {}).get("dev_sold", 0.0) if source == "late" else 0.0)
        if source != "callout" and s.mint not in self.audit:        # yardstick row for the gate audit
            self.audit[s.mint] = ["(bought)", self.now, fill.price, fill.price, fill.price, ""]
        if source not in ("callout", "manual") and self.feed.realtime:
            self.lab.start(s.mint, s.symbol, "late" if source == "late" else "sniper", fill.price, self.now, self.p)
            try:                                                     # the bot's call, on the record
                self.ledger.call(s.mint, s.symbol, "bot", s.market_cap_sol * self.sol_price.usd, fill.price,
                                 self.sol_price.usd, thesis="; ".join(notes)[:280], source=source,
                                 mode=self.mode, ts=self.now)
            except LedgerError:
                pass
        self.stats["entries"] += 1
        self.save_state()
        self.stats["entries_" + source.split(":")[0]] += 1
        self.say("buy", f"{s.symbol} {fill.sol:.3f} SOL @ curve {s.curve.progress:.0%} [{source}] | score {score:.0f} | "
                        + ", ".join(notes), s.mint, signature=fill.signature, timing=fill.timing,
                 decided_slot=(meta or {}).get("slot"))

    async def _check_exit(self, s: TokenState) -> None:
        pos = self.positions[s.mint]
        if not s.price_known:          # restored after a restart: wait for a real price before any exit logic,
            limit = {"late": self.p.late.max_hold_s,              # but each strategy's time limit still holds
                     "callout": self.p.callouts.hold_s}.get(pos.bot or pos.source, self.p.exit.max_hold_s)
            if (pos.source != "manual" or pos.bot) and self.now - pos.opened_at >= limit:
                await self._sell(s, pos, 1.0, f"max hold {limit:.0f}s (price unknown)")
            return
        px = s.curve.price
        pos.peak_price = max(pos.peak_price, px)
        pos.trough_price = min(pos.trough_price or px, px)
        rules = "ride" if pos.source == "manual" and pos.bot else pos.source    # handed over: the bots ride it
        if rules == "ride":
            if not pos.handed_price:                   # handed over before these rules existed
                pos.handed_price = pos.handed_peak = px
            r = evaluate_ride_exit(pos, s, self._ride_cfg(), self.mode.startswith("live"))
        elif rules == "manual":            # yours: only the exits you set, plus leaving the curve
            mc = self._manual_cfg()
            # a graduated coin keeps a price on paper (DexScreener's pool); live orders only reach the curve
            r = evaluate_manual_exit(pos, s, {"sl": mc.stop_loss_pct, "tp": mc.take_profit_pct,
                                              "tp_frac": mc.take_profit_frac, "trail": mc.trail_pct,
                                              "sell_on_graduation": mc.sell_on_graduation and self.mode.startswith("live"),
                                              **(pos.manual or {})})
            if r and r[1].startswith("manual take profit"):
                pos.manual = {**(pos.manual or {}), "tp_done": True}      # once per position
        elif rules == "callout":              # hold the $1 callout bag; never trade it against followers
            held = self.now - pos.opened_at
            r = (1.0, "dev sold") if s.dev_sold else \
                ((1.0, "callout hold done") if held >= self.p.callouts.hold_s else None)
        elif (why := await self._cluster_watch(s)):
            r = (1.0, why)
        elif rules == "late":
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
        meta = {"quote": s.curve.price, "decided": self.now, "slot": self.last_slot}
        if self._paper_delay() > 0:
            self._defer({"side": "sell", "mint": s.mint, "pos": pos, "tokens": tokens, "reason": reason,
                         "first_quote": s.curve.price, "attempt": 0, "steps": self._sell_steps(reason), **meta})
            return
        await self._dispatch(self._run_sell(s, pos, tokens, reason, meta))

    def _sell_steps(self, reason: str) -> list:
        x = self.p.execution
        urgent = x.get("urgent_sell_slippage_steps")
        return list(urgent) if urgent and any(k in reason for k in URGENT_EXITS) else list(x.sell_slippage_steps)

    async def _run_sell(self, s: TokenState, pos: SniperPosition, tokens: float, reason: str, meta=None) -> None:
        steps = self._sell_steps(reason)
        kw = {"steps": steps} if steps != list(self.p.execution.sell_slippage_steps) else {}
        try:
            fill = await self.ex.sell(s.mint, s.curve, tokens,
                                      self.p.callouts.priority_fee_sol if pos.source == "callout" else None, **kw)
        except Exception as e:
            fill = SniperFill(False, error=f"{type(e).__name__}: {e}"[:240])
        if fill.unknown:              # sending a fresh sell now could sell twice: wait for the chain instead
            self._book_fees_lost(fill, s)
            self._track_unresolved(fill.signature, {"mint": s.mint, "side": "sell", "tokens": tokens, "reason": reason})
            self.say("error", f"sell {s.symbol}: sent ({fill.signature[:8]}…) but its outcome is unknown - no new "
                              "sell until the chain says", s.mint)
            return
        self.pending.discard(s.mint)
        self._apply_sell(s, pos, fill, reason, meta)

    def _apply_sell(self, s: TokenState, pos: SniperPosition, fill: SniperFill, reason: str, meta=None) -> None:
        self._book_fees_lost(fill, s)
        if self.positions.get(s.mint) is pos:
            pos.failed_fees_sol += fill.fees_lost
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
        if meta and meta.get("quote") and sold > 0:
            pos.exit_quote, pos.exit_fill = meta["quote"], fill.sol / sold
            pos.exit_delay_s = round(self.now - meta.get("decided", self.now), 3)
        self.book.day_pnl += fill.sol - cost_part
        if reason.startswith("initials"):
            pos.initials_taken = True
        elif reason.startswith("ladder ") and "x sell" in reason:
            pos.ladder_hit += 1
            pos.initials_taken = True
        self.say("sell", f"{s.symbol} {sold / pos.initial_tokens:.0%} for {fill.sol:.3f} SOL | {reason}",
                 s.mint, signature=fill.signature, timing=fill.timing, decided_slot=(meta or {}).get("slot"))
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
                self._apply_buy(s, fill, o["score"], o["notes"] + ["landed late"], o["source"], o["leader"],
                                {"add": o.get("add", False)})
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
        for o in self.orders.values():
            if o["mint"] == pos.mint and o["side"] == "sell" and o["status"] == "open":
                o.update(status="cancelled: position closed", done_at=self.now)
        s.late_tried = True                        # no graduation-play re-buy of a token we just traded
        if pos.cost_sol > 0:                       # unsold remainder (dust, gone from wallet) is written off -
            self.book.day_pnl -= pos.cost_sol      # once, and today, so the daily loss limit sees all of it
            pos.cost_sol = 0.0
        pnl = pos.proceeds_sol - pos.initial_cost_sol
        usd = self.sol_price.usd
        mc_in = self._mc_at(s, pos.entry_price)
        mc_out = s.market_cap_sol if s.price_known else None
        row = {
            "entry_mcap_sol": round(mc_in, 2) if mc_in else None, "exit_mcap_sol": round(mc_out, 2) if mc_out else None,
            "entry_mcap_usd": round(mc_in * usd) if mc_in and usd else None, "exit_mcap_usd": round(mc_out * usd) if mc_out and usd else None,
            "mint": pos.mint, "symbol": pos.symbol, "opened": pos.opened_at, "closed": self.now,
            "cost": pos.initial_cost_sol, "proceeds": pos.proceeds_sol, "pnl": pnl,
            "pnl_pct": pnl / max(pos.initial_cost_sol, 1e-12) * 100, "peak_gain_pct": pos.gain_pct(pos.peak_price),
            "score": pos.score, "initials": pos.initials_taken, "exit": pos.exits[-1][1] if pos.exits else "",
            "source": pos.source, "desk": pos.desk, "p": pos.p,
            "mae_pct": pos.gain_pct(pos.trough_price) if pos.trough_price else 0.0,
            # which run produced this trade: demo, paper and live results must never be mixed up
            "mode": self.mode, "session": self.session, "start_sol": self.book.start_sol,
            "config": self.config_id, "model": self._model_id(),
            # execution: all-in fill vs the price when we decided (fees, impact, slippage, delay), and timing
            "entry_vs_signal_pct": round((pos.entry_price / pos.entry_quote - 1) * 100, 2) if pos.entry_quote else None,
            "exit_vs_signal_pct": round((pos.exit_fill / pos.exit_quote - 1) * 100, 2) if pos.exit_quote else None,
            "entry_delay_s": pos.entry_delay_s, "exit_delay_s": pos.exit_delay_s,
            "failed_fees_sol": round(pos.failed_fees_sol, 6),
            "feat": pos.feat,
        }
        self.record_close(row, pos.mint)
        if pos.leader:
            paused = self.leaders.on_copy_closed(pos.leader, pnl, self.p.copy.pause_after_losses)
            if paused:
                self.say("error", f"paused copying {self.leaders.label(pos.leader)}: {paused}")
        asyncio.ensure_future(self._unwatch(pos.mint))

    def record_close(self, row: dict, mint: str = "") -> None:
        """Book a closed trade: the session's list, win/loss counts, defense mode, the log line and the trade journal."""
        pnl = row["pnl"]
        self.book.closed.append(row)
        self.stats["wins" if pnl > 0 else "losses"] += 1
        self._update_defense()
        where = f" on {row['chain']}" if row.get("chain") else ""
        self.say("close", f"{row['symbol']}{where} {'+' if pnl >= 0 else ''}{pnl:.3f} SOL ({row['pnl_pct'] / 100:+.0%}) "
                          f"[{row['source']}]", mint if not row.get("chain") else "")
        if self.journal:
            DATA.mkdir(exist_ok=True)
            with (DATA / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(row['closed']))}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")

    async def _tick(self) -> None:
        if self.deferred:
            await self._settle_deferred()
        if self.now - self._lab_check >= 15:
            self._lab_check = self.now
            self._lab_step()
        for m, t in list(self.hand_after.items()):       # a "Buy & give to bots" whose buy has filled
            pos = self.positions.get(m)
            if pos is not None and pos.source == "manual":
                del self.hand_after[m]
                self.hand_over(m, True)
            elif self.now - t > 900 and m not in self.pending and m not in self.manual_queue:
                del self.hand_after[m]                   # the buy never happened
        if self.persist and self.now - self._last_state_save >= 10:
            self.save_state()
        if self.feed.realtime and self.p.sizing.enabled and self.now - self._last_price_refresh >= 300:
            self._last_price_refresh = self.now
            asyncio.create_task(self.sol_price.refresh())
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now))
        if day != self.book.day:
            if self.book.day and self.feed.realtime:      # the day that just closed, in one message (phone + log)
                self.say("digest", self.daily_digest(self.book.day))
            self.book.day, self.book.day_pnl = day, 0.0
        lim = self.p.capital.daily_loss_limit_sol
        stopped = -self.book.day_pnl >= lim
        if stopped != self._day_stop_said and self.feed.realtime:   # each change, once (the stop used to be silent)
            self._day_stop_said = stopped
            if stopped:
                self.say("error", f"DAILY LOSS LIMIT: {self.book.day_pnl:+.3f} SOL today (limit {lim:g}): no new entries "
                                  f"until 00:00 UTC unless a close brings it back; open positions are still managed")
            else:
                self.say("info", f"Entries open again: today {self.book.day_pnl:+.3f} SOL is inside the {lim:g} SOL daily limit")
        eq = self.equity()
        self.book.mark(eq)
        if self.record_file and self.feed.realtime and self.now - self._last_health >= 60:
            self._last_health = self.now                # feed condition into the recording, once a minute
            f = self.feed
            self.record_file.write(dumps(Health(self.now, getattr(f, "host", ""), getattr(f, "gap_pct", None),
                                                getattr(f, "lag_s", None), getattr(f, "degraded_reason", ""),
                                                self.sol_price.usd)) + "\n")
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
        dd = (1 - eq / (self.book.kill_base or self.book.start_sol)) * 100
        if not self.book.halted and dd >= self.p.capital.max_drawdown_pct:
            self.book.halted = f"drawdown {dd:.0f}%"
            self.say("error", f"KILL SWITCH: {self.book.halted} - selling everything")
        await self._price_fallback()
        self._maybe_huddle()
        cleanup = self.now - self._last_cleanup >= 10       # deletions only need a 10 s cadence
        if cleanup:
            self._last_cleanup = self.now
            recent_calls = {c.mint for c in self.callouts.calls if self.now - c.ts < 3660} | self._pinned()
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
        await self._manual_queue_tick()
        await self._orders_tick()
        if self.lab.open:
            self.lab.tick(self.tokens, self.now, self.p)
        if self.now - self._last_ledger_tick >= 15:
            self._last_ledger_tick = self.now
            self._ledger_tick()
        if self.feed.realtime and self.now - self._last_think >= 2:
            self._last_think = self.now
            self._late_think()
        if self.record_file and self.now - self._last_flush >= 30:
            self._last_flush = self.now
            self._rotate_record()
            self.record_file.flush()
        self.callouts.settle(self.now, lambda m: self.tokens[m].curve.price if m in self.tokens else None)
        for mint, until in list(self.watch_until.items()):
            if until <= self.now and mint not in self.positions and (mint not in self.tokens or self.tokens[mint].decided):
                await self._unwatch(mint)
        cut = self.now - self.FAMILY_S
        for k in list(self.families):
            q = self.families[k]
            while q and q[0][0] < cut:
                self.fam_meta.pop(q.popleft()[1], None)
            if not q:
                del self.families[k]
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

    async def _graduated_ok(self, s: TokenState) -> str:
        """A coin that left the curve for PumpSwap: paper trades it at DexScreener's pool price (refreshed
        every 10 s while held, curve fees modelled - a little worse than the pool's real ~0.3%). Live refuses:
        its orders would route to PumpSwap (pool=auto), but a price that updates every 10 s is too stale to
        trade real money on. '' or why not."""
        if self.mode.startswith("live"):
            return "it has graduated to PumpSwap: live trading of graduated coins is off, since their price only updates every 10 s (paper can trade it)"
        if not self.feed.realtime:                        # a replay or demo: no pool to ask
            return "" if s.price_known else "it has graduated and there's no pool price for it here"
        if s.mint not in await self._dex_price([s.mint]):   # a fresh price to buy at
            return "it has graduated, and DexScreener has no SOL pool price for it right now"
        return ""

    async def _price_fallback(self) -> None:
        """Held tokens with no price (just restored), no trade for 90s (quiet), or graduated to PumpSwap (outside
        our stream): fetch a price from DexScreener every 10s so exits and equity keep working."""
        if not self.feed.realtime or self.now - self._last_price_fallback < 10:
            return
        stale = [m for m, p in self.positions.items()
                 if m in self.tokens and (not self.tokens[m].price_known or self.tokens[m].migrated
                                          or self.now - self.tokens[m].last_trade_ts > 90)]
        if not stale:
            return
        self._last_price_fallback = self.now
        await self._dex_price(stale)

    async def _dex_price(self, mints: list[str]) -> set[str]:
        """DexScreener's price for these coins: exact curve reserves while on pump.fun, the pool price after.
        Returns the mints it priced."""
        from .curve import FINAL_V_TOKENS, INITIAL_V_SOL, INITIAL_V_TOKENS
        from ..clients import dexscreener

        try:
            pairs = await asyncio.to_thread(dexscreener.best_pair_by_mint, mints)
        except Exception:
            return set()
        done: set[str] = set()
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
            done.add(m)
        return done

    # ------------------------------------------------------------------ persistence (live, and the real-feed paper bot)
    def save_state(self) -> None:
        if not self.persist:
            return
        from dataclasses import asdict

        self._last_state_save = self.now
        b = self.book
        # enough safety context per held token that exits work the same after a restart: who the creator
        # is (dev-sell exits, funding links) and what they've already sold
        tokens = {}
        for m in self.positions.keys() | {o["mint"] for o in self.unresolved.values()} | \
                {o["mint"] for o in self.orders.values() if o["status"] == "open"}:
            s = self.tokens.get(m)
            if s is not None:
                tokens[m] = {"launch": asdict(s.launch) if s.launch else None, "dev_sold": s.dev_sold}
        state = {"saved_at": time.time(), "mode": self.mode,
                 "book": {"sol": b.sol, "start_sol": b.start_sol, "day": b.day, "day_pnl": b.day_pnl,
                          "halted": b.halted, "closed": b.closed[-500:], "reserved": b.reserved,
                          "peak_equity": b.peak_equity, "max_dd_pct": b.max_dd_pct, "kill_base": b.kill_base,
                          "deposits": b.deposits[-200:], "equity_hist": list(b.equity_hist)},
                 "positions": {m: asdict(p) for m, p in self.positions.items()},
                 "tokens": tokens, "unresolved": self.unresolved, "orders": self.orders, "away": self.away,
                 "defense": {"until": self.defense_until, "reason": self.defense_reason},
                 "called": sorted(self.callouts.called)[-2000:], "pulse": [list(r) for r in self.pulse]}
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
        from .mcapfill import apply as fill_mcaps
        fill_mcaps(self.book.closed, DATA, self.sol_price.usd)          # market caps for trades from before they were logged
        # cash stays reserved only for buys that are still unresolved; anything else was in flight when the
        # process died and is accounted for by the wallet reconcile below
        open_buys = {o["mint"] for o in (d.get("unresolved") or {}).values() if o.get("side") == "buy"}
        self.book.reserved = {m: v for m, v in (b.get("reserved") or {}).items() if m in open_buys}
        self.book.peak_equity = b.get("peak_equity", self.book.start_sol)
        self.book.max_dd_pct = b.get("max_dd_pct", 0.0)
        self.book.kill_base = b.get("kill_base", 0.0)
        self.book.deposits = [tuple(x) for x in b.get("deposits") or []]
        self.book.equity_hist.extend(tuple(x) for x in b.get("equity_hist") or [])
        self.defense_until = (d.get("defense") or {}).get("until", 0.0)
        self.defense_reason = (d.get("defense") or {}).get("reason", "")
        self.callouts.called.update(d.get("called", []))
        cut = time.time() - 90 * 60                          # the Market Pulse chart keeps its last hour across a restart
        self.pulse.extend(r for r in d.get("pulse") or [] if isinstance(r, list) and len(r) == 6 and r[0] >= cut)
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
        self.orders = {k: o for k, o in (d.get("orders") or {}).items() if o.get("status") == "open"}
        self.away = bool(d.get("away"))
        for o in self.orders.values():                        # keep pricing coins with open orders
            if o["mint"] not in self.tokens:
                restore_token(o["mint"]).decided = "watching: open order"
                await self._watch(o["mint"])
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
        if not L.enabled or self.now - self._last_late_scan < L.get("scan_interval_s", 2) or self.entries_blocked():
            return
        self._last_late_scan = self.now
        en = self.p.entry
        for s in list(self.tokens.values()):
            if self.feed.realtime and s.mint not in self.linked_x and s.launch is not None and s.launch.twitter \
                    and not s.migrated and s.curve.progress * 100 >= L.min_curve_pct - 15:
                self._read_x_link(s)
            if not s.decided or s.late_tried or s.mint in self.positions or s.mint in self.pending \
                    or s.mint in self.reviewing or not s.price_known:
                continue
            ok, why = evaluate_late_entry(s, self.now, L, {
                "max_bundle_pct": en.max_bundle_pct, "max_early_sold_ratio": en.max_early_sold_ratio,
                "creator_launches": len(self.creators.get(s.creator, ())),
                "max_creator_launches_24h": en.max_creator_launches_24h,
                "max_cluster_pct": en.funding.max_cluster_pct})
            if not ok:
                if why in SKIP_FOR_GOOD:                   # it qualified but looks like the dump profile: never this coin
                    s.late_tried = True
                continue
            s.late_tried = True
            await self._enter(s, kind="late", score=max(s.score, 60.0), buy_sol=self.p.capital.buy_sol,
                              notes=[why], source="late")
            if self.entries_blocked():
                break

    def _read_x_link(self, s) -> None:
        """Read a coin's X link once, as it nears the graduation window, so the narrative persona has the story
        when the desk votes (reading it then would delay the buy). One read every 2 s, 800 a day, through
        FxTwitter: no X login, no cost."""
        from .memory import fetch_x_link

        day = time.strftime("%Y-%m-%d", time.gmtime())
        n = self._x_day[1] if self._x_day[0] == day else 0
        if time.time() < self._x_next_at or n >= 800:
            return
        self._x_next_at, self._x_day = time.time() + 2.0, (day, n + 1)
        if len(self.linked_x) > 3000:
            self.linked_x = {m: d for m, d in self.linked_x.items() if m in self.tokens}
        self.linked_x[s.mint] = {"kind": "reading"}

        async def go(mint=s.mint, link=s.launch.twitter):
            try:
                self.linked_x[mint] = await fetch_x_link(link)
            except Exception as e:                       # network, rate limit: the persona is told it's unread
                self.linked_x[mint] = {"kind": f"couldn't read it ({type(e).__name__})"}
        asyncio.create_task(go())

    def narrative_context(self, s) -> dict:
        """What the narrative persona reads on top of the snapshot: the coin's own story (its description and the
        X post or account it links to) and what is drawing attention now (this hour's copycat waves, coins that just
        graduated, X posts naming this coin). Its texts are written by strangers: data, never instructions."""
        L = s.launch
        out: dict = {"description": self.descriptions.get(s.mint, "")[:300]}
        x = dict(self.linked_x.get(s.mint) or {})
        if x.get("ts") and L is not None:
            x["minutes_before_launch"] = round((L.ts - float(x.pop("ts"))) / 60)
        x.pop("ts", None)
        if x:
            out["linked_x"] = x
        elif L is not None and L.twitter:
            out["linked_x"] = {"kind": "not read yet"}
        else:
            out["linked_x"] = {"kind": "no X link"}
        out["hot_names_last_hour"] = [{"name": r["key"], "launches": r["n"], "biggest_mcap_usd": r["lead_mc_usd"]}
                                      for r in self.hot_names(60, 8)]
        out["graduated_last_2h"] = [sym for ts, _, sym, _ in self.migrations if self.now - ts <= 7200][:12]
        posts = getattr(self.xfeed, "posts", None) or {}
        sym = (s.symbol or "").upper()
        hits = [p for p in list(posts.values()) if s.mint in (p.get("mints") or ())
                or (sym and sym in {c.upper() for c in (p.get("cashtags") or ())})]
        out["x_posts_naming_it"] = [{"by": "@" + str((p.get("author") or {}).get("handle", "")),
                                     "followers": (p.get("author") or {}).get("followers"), "likes": p.get("likes"),
                                     "minutes_ago": round((self.now - float(p.get("ts") or 0)) / 60),
                                     "text": " ".join(str(p.get("text") or "").split())[:200]}
                                    for p in sorted(hits, key=lambda p: -float(p.get("ts") or 0))[:3]]
        return out

    FEATS = ("age_s", "curve_progress_pct", "market_cap_sol", "unique_buyers", "buys", "sells", "buys_last_20s",
             "sells_last_20s", "net_flow_sol_20s", "dev_initial_buy_pct", "dev_sold", "bundle_pct", "early_buyers_sold_ratio",
             "top10_holders_pct", "price_vs_peak")

    def _entry_features(self, s: TokenState, source: str) -> dict | None:
        """What the bot saw when it decided to buy, kept with the trade: the coins that dumped can later be told
        apart from the ones that didn't (the desk's own view, plus flow over the graduation play's window)."""
        try:
            from .desk import snapshot_for
            d = snapshot_for(s, self.now, "late" if source == "late" else "sniper", {})
            f = {k: d.get(k) for k in self.FEATS}
            t = d.get("token") or {}
            w = float(self.p.late.flow_window_s)
            f.update(links=sum(bool(t.get(k)) for k in ("twitter", "telegram", "website")),
                     net_flow_sol_window=round(s.net_flow_sol(self.now, w), 3), buyers_window=s.buyers_in(self.now, w),
                     net_flow_sol_60s=round(s.net_flow_sol(self.now, 60), 3), buyers_60s=s.buyers_in(self.now, 60),
                     secs_since_high=round(self.now - s.last_high_ts(), 1) if s.last_high_ts() else None,
                     creator_launches=len(self.creators.get(s.creator, ())))
            fam = self.family(s.mint)
            f["family"] = ("og" if fam.get("og") else "copy") if fam else "alone"
            f["family_n"] = fam.get("n", 1) if fam else 1
            return f
        except Exception:                                # research data must never stop a trade
            return None

    def _late_red(self, s: TokenState) -> dict:
        en = self.p.entry
        return {"max_bundle_pct": en.max_bundle_pct, "max_early_sold_ratio": en.max_early_sold_ratio,
                "creator_launches": len(self.creators.get(s.creator, ())),
                "max_creator_launches_24h": en.max_creator_launches_24h, "max_cluster_pct": en.funding.max_cluster_pct}

    def think(self, agent: str, text: str, mint: str = "", symbol: str = "", mood: str = "info") -> None:
        self.thoughts.append({"ts": self.now, "agent": agent, "text": text, "mint": mint, "symbol": symbol,
                              "mood": mood})

    def _late_think(self) -> None:
        """The graduation scanner's view for the Desk: every token near or in its window, checked the same
        way evaluate_late_entry checks it, plus a thought whenever a token's verdict changes."""
        L = self.p.late
        view, seen = [], set()
        blocked = self.entries_blocked() if L.enabled else "graduation plays are off"
        for s in self.tokens.values():
            if s.migrated or not s.price_known or s.curve.progress * 100 < L.min_curve_pct - 15 \
                    or s.age(self.now) > L.max_age_s + 120:
                continue
            c = late_checklist(s, self.now, L, self._late_red(s))
            held = s.mint in self.positions or s.mint in self.pending
            if held:
                c["verdict"], c["why"] = "holding", "bought: the exit manager has it"
            elif s.late_tried and c["verdict"] in ("buy", "wait"):
                c["verdict"], c["why"] = "done", "already tried once (no re-buys)"
            elif c["verdict"] == "buy" and blocked:
                c["verdict"], c["why"] = "blocked", f"would buy, but {blocked}"
            view.append({"mint": s.mint, "symbol": s.symbol, "age": round(s.age(self.now)),
                         "mcap_sol": s.market_cap_sol, "spark": s.sparkline(30), **c})
            seen.add(s.mint)
            prev = self._think.get(s.mint)
            v, why = c["verdict"], c["why"]
            if prev is None and v in ("early", "out", "holding", "done"):
                self._think[s.mint] = (v, why, self.now)          # nothing worth saying yet
            elif prev is None or prev[0] != v or (v == "wait" and why != prev[1] and self.now - prev[2] >= 20):
                text = {"early": f"{s.symbol} is filling: {c['progress']:.0f}%, the window opens at {L.min_curve_pct}%",
                        "wait": f"{s.symbol} in the window ({c['progress']:.0f}%), waiting on {why}",
                        "buy": f"{s.symbol}: every check passes, buying",
                        "blocked": f"{s.symbol}: {why}",
                        "pass": f"{s.symbol}: passing, {why}",
                        "out": f"{s.symbol} is out: {why}",
                        "holding": f"{s.symbol}: bought, watching the exits",
                        "done": f"{s.symbol}: already traded once, letting it go"}[v]
                mood = {"buy": "act", "blocked": "warn", "pass": "pass", "out": "pass", "holding": "act",
                        "done": "pass"}.get(v, "spot" if v == "early" else "wait")
                self.think("scanner", text, s.mint, s.symbol, mood)
                self._think[s.mint] = (v, why, self.now)
        for m in [m for m in self._think if m not in seen]:
            del self._think[m]
        order = {"buy": 0, "blocked": 0, "holding": 1, "wait": 2, "early": 3, "done": 4, "pass": 5, "out": 6}
        view.sort(key=lambda r: (order.get(r["verdict"], 9), -r["readiness"], -r["progress"]))
        self.late_view = view

    def vote_speed(self) -> dict | None:
        """How long the desk's reviews take: median and slowest 10%, over the last 200."""
        v = sorted(self.desk_vote_s)
        if not v:
            return None
        return {"n": len(v), "median": v[len(v) // 2], "p90": v[min(len(v) - 1, int(len(v) * 0.9))]}

    def desk_view(self) -> dict:
        """Everything the Desk tab shows that lives in the engine."""
        L, x = self.p.late, self.p.exit
        holding = []
        for m, pos in self.positions.items():
            s = self.tokens.get(m)
            if not s or pos.source == "callout":
                continue
            price = self._mark(m, pos)
            own = None
            if pos.source == "manual":                    # yours: no time limit, only your rules (or the ride's)
                mc = self._manual_cfg()
                own = {"ride": self._ride_cfg()} if pos.bot else {"sl": mc.stop_loss_pct, "tp": mc.take_profit_pct,
                                                                   "trail": mc.trail_pct, **(pos.manual or {})}
            holding.append({"mint": m, "symbol": pos.symbol, "source": pos.source, "gain_pct": pos.gain_pct(price),
                            "peak_gain_pct": pos.gain_pct(pos.peak_price), "held_s": round(self.now - pos.opened_at),
                            "value_sol": pos.tokens * price, "watch": exit_watch(pos, s, self.now, L, x, own),
                            "manual": pos.source == "manual", "bot": pos.bot})
        agent_log = [l for l in self.log if l["level"] in ("buy", "sell", "close", "desk", "agent", "error")][-60:]
        d = self.desk
        return {"now": self.now, "blocked": self.entries_blocked(), "paused": self.paused,
                "strategies": {"sniper": self.p.entry.enabled, "copy": self.p.copy.enabled,
                               "callouts": self.p.callouts.enabled, "late": L.enabled},
                "late": {"window": [L.min_curve_pct, L.max_curve_pct], "max_age_s": L.max_age_s,
                         "flow_window_s": L.flow_window_s, "min_net_flow_sol": L.min_net_flow_sol,
                         "min_buyers": L.min_buyers, "scan_interval_s": L.get("scan_interval_s", 2),
                         "candidates": self.late_view[:14], "in_window": sum(1 for r in self.late_view
                                                                             if r["verdict"] in ("wait", "buy", "blocked")),
                         "tracked": len(self.tokens)},
                "holding": holding, "thoughts": list(self.thoughts)[-80:], "log": agent_log,
                "rejects": self.rejects.most_common(5),
                "huddles": list(self.huddles)[::-1][:5], "huddle_minutes": float(self.p.desk.get("huddle_minutes", 30) or 0), "plan": self.plan,
                "huddle_error": self.huddle_error, "huddling": self._huddling,
                "desk": {"enabled": bool(d and d.enabled), "configured": bool(self.p.desk.enabled), "brain": self.desk_brain(),
                         "personas": list(self.p.desk.personas), "model": self.p.desk.model,
                         "calls": d.calls if d else 0, "cost_usd": round(d.cost_usd(), 4) if d else 0.0,
                         "vote_s": self.vote_speed(),
                         "error": self.desk_error, "failures": self.desk_failures,
                         "reviews": list(self.desk_reviews)[::-1]},
                "risk": self.risk_info(), "day_pnl": self.book.day_pnl,
                "limits": {"daily_loss_sol": self.p.capital.daily_loss_limit_sol,
                           "kill_dd_pct": self.p.capital.max_drawdown_pct,
                           "max_open": self.p.capital.max_open_positions},
                "drawdown_pct": (1 - self.equity() / self.book.start_sol) * 100 if self.book.start_sol else 0.0,
                "open": len(self.positions), "defense": {"on": self._defensive(), "reason": self.defense_reason},
                "feed": {"host": getattr(self.feed, "host", ""), "lag_s": getattr(self.feed, "lag_s", None),
                         "gap_pct": getattr(self.feed, "gap_pct", None),
                         "degraded": bool(getattr(self.feed, "degraded", False)),
                         "degraded_reason": getattr(self.feed, "degraded_reason", ""),
                         "non_sol_skipped": getattr(self.feed, "non_sol_skipped", 0)}}

    def set_desk(self, on: bool, who: str = "dashboard") -> str:
        """Wake or rest the AI desk. '' or the reason it can't. The owner's choice (who="dashboard") is saved
        to params.yaml so a restart keeps it; the bot resting a failing desk lasts until the next restart."""
        err = self._set_desk(on)
        if not err and who == "dashboard" and self.persist:      # the real bot, not a demo or a test
            try:
                self.save_setting("desk.enabled", bool(on))
            except OSError as e:
                return f"applied, but not saved: {e}"
        return err

    def _set_desk(self, on: bool) -> str:
        from .desk import provider_ready

        if on:
            why = provider_ready(self.p.desk)
            if why:
                return why
            try:                                        # always a fresh client: the key or workspace may be new
                from .desk import Desk

                self.p.desk["enabled"] = True
                self.desk = Desk(self.p.desk)
            except Exception as e:                      # e.g. the anthropic package is missing
                self.p.desk["enabled"] = False
                return f"couldn't start the desk: {type(e).__name__}: {e}"[:200]
            self.desk_failures, self.desk_error = 0, ""
            self.desk_reviews.clear()                   # votes from before this wake (maybe another key) are stale
            self.p.desk["enabled"] = True
            self.desk.enabled = True
            self.say("info", "AI desk is on: its personas now vote on every entry (Anthropic API, billed per call)")
        else:
            self.p.desk["enabled"] = False
            if self.desk:
                self.desk.enabled = False
            self.say("info", "AI desk is resting")
        return ""

    def token_uri(self, mint: str) -> str:
        s = self.tokens.get(mint)
        return (s.launch.uri if s and s.launch else "") or ""

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
            call = Callout(s.mint, s.symbol, self.now, s.market_cap_sol, s.curve.price, text, score)
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
    # ------------------------------------------------------------------ manual trading (the owner, from the dashboard)
    def _manual_cfg(self):
        from ..config import Params

        d = {"max_sol": 2.0, "presets_sol": [0.05, 0.1, 0.25, 0.5, 1.0], "stop_loss_pct": 0,
             "take_profit_pct": 0, "take_profit_frac": 0.5, "trail_pct": 0, "sell_on_graduation": True, "queue_s": 60,
             "handover": RIDE_DEFAULTS}
        return Params({**d, **(self.p.get("manual") or {})})

    def _ride_cfg(self) -> dict:
        """How the bots ride a position you hand them (manual.handover in the config)."""
        return {**RIDE_DEFAULTS, **dict((self.p.get("manual") or {}).get("handover") or {})}

    async def manual_buy(self, mint: str, sol: float) -> str:
        """Your buy: '' when sent (or queued for the coin's first priced trade), else why not."""
        from .tracker import TokenState

        mc = self._manual_cfg()
        try:
            sol = round(float(sol), 6)
        except (TypeError, ValueError):
            return "amount must be a number"
        if not 0 < sol <= mc.max_sol:
            return f"amount must be more than 0 and at most {mc.max_sol:g} SOL (manual.max_sol)"
        mint = str(mint or "").strip()
        if not 32 <= len(mint) <= 44:
            return "that isn't a contract address"
        if mint in self.pending:
            return "an order for that coin is already in flight: wait a moment"
        if mint in self.positions:                       # you already hold it: this adds to the position
            s = self.tokens.get(mint)
            if s is not None and s.migrated:
                why = await self._graduated_ok(s)
                if why:
                    return why
            if s is None or not s.price_known:
                return "no live price for it right now, so nothing to add at"
            why = self._authorize(mint, sol, "manual")
            if why:
                return why
            await self._buy(s, 50.0, sol, [f"manual add {sol:g} SOL"], source="manual", add=True)
            return ""
        s = self.tokens.get(mint)
        if s is None:
            s = self.tokens[mint] = TokenState(mint, None, self.now)
            await self._watch(mint)
        if not s.price_known and self.feed.realtime:      # not seen trading yet: maybe it graduated long ago
            await self._dex_price([mint])
        if s.migrated:
            why = await self._graduated_ok(s)
            if why:
                return why
        why = self._authorize(mint, sol, "manual")
        if why:
            return why
        if not s.price_known:
            self.manual_queue[mint] = (sol, self.now)
            self.say("info", f"manual buy {sol:g} SOL of {s.symbol or mint[:6]} queued: waiting for its first trade "
                             f"to show a price ({mc.queue_s:.0f}s)", mint)
            return ""
        if s.dev_sold:
            self.say("info", f"note: {s.symbol}'s creator has sold; buying anyway because you asked", mint)
        s.late_tried = True                               # the bot's own strategies leave your coin alone
        await self._buy(s, 50.0, sol, [f"manual {sol:g} SOL"], source="manual")
        return ""

    async def _manual_queue_tick(self) -> None:
        if not self.manual_queue:
            return
        limit = self._manual_cfg().queue_s
        for mint, (sol, at) in list(self.manual_queue.items()):
            s = self.tokens.get(mint)
            if s is not None and s.price_known:
                del self.manual_queue[mint]
                why = await self.manual_buy(mint, sol)
                if why:
                    self.say("error", f"queued manual buy of {s.symbol or mint[:6]} not sent: {why}", mint)
            elif self.now - at > limit:
                del self.manual_queue[mint]
                self.say("info", f"queued manual buy of {mint[:6]}… dropped: no trade in {limit:.0f}s "
                                 "(not on the pump.fun curve, or very quiet)", mint)

    async def manual_sell(self, mint: str, fraction: float) -> str:
        pos, s = self.positions.get(mint), self.tokens.get(mint)
        if pos is None or s is None:
            return "no open position in that coin"
        if mint in self.pending:
            return "an order for that coin is already in flight"
        try:
            frac = float(fraction)
        except (TypeError, ValueError):
            return "fraction must be a number"
        if not 0 < frac <= 1:
            return "fraction must be more than 0 and at most 1"
        if not s.price_known:
            return "no live price for it yet"
        await self._sell(s, pos, frac, "manual sell" if frac >= 1 else f"manual sell {frac:.0%}")
        return ""

    async def take_initials(self, mint: str) -> str:
        """Sell just enough to get the initial cost back; the rest rides for free."""
        pos, s = self.positions.get(mint), self.tokens.get(mint)
        if pos is None or s is None or not s.price_known:
            return "no open position with a live price"
        frac = initials_fraction(pos, s.curve.price, self.fee + self.p.execution.paper_latency_slippage_pct)
        if frac is None:
            return "not enough profit to take initials (it would mean selling the whole bag): use Exit instead"
        if mint in self.pending:
            return "an order for that coin is already in flight"
        await self._sell(s, pos, min(frac * 1.02, 1.0), "manual: initials")     # 2% margin for the fill
        pos.initials_taken = True
        return ""

    def set_manual_exits(self, mint: str, sl=None, tp=None, tp_frac=None, trail=None) -> str:
        pos = self.positions.get(mint)
        if pos is None:
            return "no open position in that coin"
        m = dict(pos.manual or {})
        for k, v, hi in (("sl", sl, 99), ("tp", tp, 10000), ("tp_frac", tp_frac, 1), ("trail", trail, 99)):
            if v is None or v == "":
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                return f"{k} must be a number"
            if not 0 <= v <= hi:
                return f"{k} must be between 0 and {hi}"
            m[k] = v
        if tp is not None:
            m["tp_done"] = False
        pos.manual = m
        return ""

    # ------------------------------------------------------------------ limit orders and alerts (the owner)
    MAX_ORDERS = 30

    @staticmethod
    def order_label(o: dict) -> str:
        cond = f"MC {'≤' if o['op'] == 'le' else '≥'} ${o['mcap_usd']:,.0f}"
        what = {"buy": f"buy {o.get('sol', 0):g} SOL", "sell": f"sell {o.get('frac', 1):.0%}",
                "alert": "alert"}[o["side"]]
        return f"{what} of {o['symbol']} when {cond}"

    async def place_order(self, mint: str, side: str, op: str, mcap_usd, sol=None, frac=None,
                          ttl_h=24.0) -> tuple[str, dict | None]:
        """A limit buy, a limit sell or an alert at a market cap (USD). ('', order) or (why not, None)."""
        import secrets

        mint = str(mint or "").strip()
        if not 32 <= len(mint) <= 44:
            return "that isn't a contract address", None
        if side not in ("buy", "sell", "alert") or op not in ("le", "ge"):
            return "pick buy, sell or alert, and ≤ or ≥", None
        try:
            target, ttl = float(mcap_usd), min(max(float(ttl_h or 24), 0.1), 168.0)
        except (TypeError, ValueError):
            return "the market cap must be a number", None
        if not 100 <= target <= 1e10:
            return "the market cap must be between $100 and $10B", None
        if sum(1 for o in self.orders.values() if o["status"] == "open") >= self.MAX_ORDERS:
            return f"at most {self.MAX_ORDERS} open orders: cancel one first", None
        o = {"side": side, "op": op, "mcap_usd": target}
        if side == "buy":
            mc = self._manual_cfg()
            try:
                o["sol"] = round(float(sol), 6)
            except (TypeError, ValueError):
                return "the amount must be a number", None
            if not 0 < o["sol"] <= mc.max_sol:
                return f"the amount must be more than 0 and at most {mc.max_sol:g} SOL (manual.max_sol)", None
        elif side == "sell":
            if mint not in self.positions:
                return "no open position in that coin to sell", None
            try:
                o["frac"] = float(frac if frac not in (None, "") else 1.0)
            except (TypeError, ValueError):
                return "the fraction must be a number", None
            if not 0 < o["frac"] <= 1:
                return "sell between 1% and 100%", None
        s = self.tokens.get(mint)
        if s is None:
            s = self.tokens[mint] = TokenState(mint, None, self.now)
            s.decided = "watching: open order"
            await self._watch(mint)
        if s.migrated:
            return "it has graduated off the bonding curve: orders follow curve coins only", None
        now_mc = s.market_cap_sol * self.sol_price.usd if s.price_known else None
        o.update(id=secrets.token_hex(4), mint=mint, symbol=s.symbol or mint[:6], created=self.now,
                 expires=self.now + ttl * 3600, status="open", mcap_at_place=now_mc)
        self.orders[o["id"]] = o
        self.say("info", f"order placed: {self.order_label(o)}"
                 + (f" (now ${now_mc:,.0f})" if now_mc else " (no price yet)"), mint)
        self.save_state()
        return "", o

    def cancel_order(self, oid: str) -> str:
        o = self.orders.get(str(oid or ""))
        if o is None or o["status"] != "open":
            return "no open order with that id"
        o.update(status="cancelled", done_at=self.now)
        self.say("info", f"order cancelled: {self.order_label(o)}", o["mint"])
        self.save_state()
        return ""

    async def _orders_tick(self) -> None:
        if not self.orders:
            return
        usd = self.sol_price.usd
        for o in list(self.orders.values()):
            if o["status"] != "open":
                if self.now - o.get("done_at", o["created"]) > 3600:
                    del self.orders[o["id"]]
                continue
            if self.now > o["expires"]:
                o.update(status="expired", done_at=self.now)
                continue
            s = self.tokens.get(o["mint"])
            if s is None or not s.price_known or o["mint"] in self.pending:
                continue
            if s.symbol and o["symbol"] == o["mint"][:6]:
                o["symbol"] = s.symbol
            if s.migrated:
                o.update(status="cancelled: graduated", done_at=self.now)
                continue
            mc = s.market_cap_sol * usd
            if not (mc <= o["mcap_usd"] if o["op"] == "le" else mc >= o["mcap_usd"]):
                continue
            o["fired_mcap"] = mc
            if o["side"] == "alert":
                why = ""
                self.say("alert", f"🔔 {o['symbol']} market cap ${mc:,.0f} "
                                  f"({'≤' if o['op'] == 'le' else '≥'} ${o['mcap_usd']:,.0f})", o["mint"])
            elif o["side"] == "buy":
                why = await self.manual_buy(o["mint"], o["sol"])
            else:
                why = await self.manual_sell(o["mint"], o["frac"]) if o["mint"] in self.positions \
                    else "no open position any more"
            o.update(status="done" if not why else f"failed: {why}", done_at=self.now)
            if o["side"] != "alert":
                self.say("info" if not why else "error",
                         f"limit order fired at MC ${mc:,.0f}: {self.order_label(o)}" + (f": {why}" if why else ""),
                         o["mint"])
            self.save_state()

    # ------------------------------------------------------------------ the call ledger
    async def make_call(self, mint: str, thesis: str = "", caller: str = "you") -> dict:
        """Put a call on the record at the current market cap: the live curve price when the bot tracks the
        coin, else a lookup (DexScreener for graduated coins)."""
        from .tracker import TokenState

        mint = str(mint or "").strip()
        s = self.tokens.get(mint)
        stage, symbol, mcap, price = "curve", "", None, 0.0
        if s is not None and s.price_known and not s.migrated:
            symbol, mcap, price = s.symbol, s.market_cap_sol * self.sol_price.usd, s.curve.price
        else:
            from .lookup import lookup

            info = await lookup(mint, self)
            symbol, mcap = info.get("symbol") or "", info.get("mcap_usd")
            price = info.get("price_sol") or 0.0
            stage = "curve" if (info.get("curve") and not info["curve"].get("complete")) else "graduated"
            if stage == "curve" and mint not in self.tokens:
                self.tokens[mint] = TokenState(mint, None, self.now)
                self.tokens[mint].decided = "watching: called"
                await self._watch(mint)
        rec = self.ledger.call(mint, symbol, caller, mcap or 0, price, self.sol_price.usd, thesis=thesis,
                               source="manual", mode=self.mode, stage=stage)
        self.say("info", f"📣 call on the record: {rec['symbol']} at ${rec['mcap_usd']:,.0f} market cap "
                         f"(#{rec['n']}, {rec['hash'][:10]}…)", mint)
        return rec

    def _ledger_tick(self) -> None:
        """Score open calls: curve coins from the live feed; the rest (graduated, untracked) from DexScreener."""
        usd = self.sol_price.usd
        rest = []
        for c in self.ledger.open_calls(self.now):
            s = self.tokens.get(c["mint"])
            if s is not None and s.price_known and not s.migrated:
                self.ledger.observe(c["id"], self.now, s.market_cap_sol * usd)
            else:
                rest.append(c)
        if rest and self.feed.realtime and not self._ledger_busy and self.now - self._last_ledger_dex >= 60:
            self._last_ledger_dex = self.now
            self._ledger_busy = True
            asyncio.create_task(self._ledger_dex(rest))
        self.ledger.save()

    async def _ledger_dex(self, calls: list[dict]) -> None:
        import httpx

        try:
            mints = sorted({c["mint"] for c in calls})
            caps: dict[str, float] = {}
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "meme_trader/1.0"}) as http:
                for i in range(0, len(mints), 30):              # DexScreener: up to 30 tokens per request
                    r = await http.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
                    if r.status_code != 200:
                        continue
                    best: dict[str, tuple[float, float]] = {}
                    for p in r.json() or []:
                        m = (p.get("baseToken") or {}).get("address")
                        liq = float((p.get("liquidity") or {}).get("usd") or 0)
                        mc = p.get("marketCap") or p.get("fdv")
                        if m and mc and liq >= best.get(m, (-1, 0))[0]:
                            best[m] = (liq, float(mc))
                    caps.update({m: v[1] for m, v in best.items()})
            for c in calls:
                if c["mint"] in caps:
                    self.ledger.observe(c["id"], self.now, caps[c["mint"]], graduated=True)
            self.ledger.save()
        except Exception as e:                                   # scoring is best effort; trading never waits
            self.say("error", f"call scoring: DexScreener lookup failed ({type(e).__name__})")
        finally:
            self._ledger_busy = False

    # ------------------------------------------------------------------ Pulse: new, final stretch, graduated
    FAMILY_S = 6 * 3600          # how far back a launch can be another's OG

    @staticmethod
    def _fam_keys(symbol: str, name: str) -> set[str]:
        """'$GIZMO' / 'Gizmo' / 'gizmo!' are one family: lowercase letters and digits only. Ticker and name share
        one namespace, since a copy often keeps the name and changes the ticker, or the other way round."""
        t, n = re.sub(r"[^a-z0-9]+", "", (symbol or "").lower()), re.sub(r"[^a-z0-9]+", "", (name or "").lower())
        return {k for k in (t if len(t) >= 2 else "", n if len(n) >= 3 else "") if k}

    def _fam_add(self, mint: str, ts: float, symbol: str, name: str) -> None:
        if mint in self.fam_meta:
            return
        self.fam_meta[mint] = (ts, symbol, name)
        for k in self._fam_keys(symbol, name):
            self.families[k].append((ts, mint))

    def family(self, mint: str, full: bool = False) -> dict | None:
        """This coin's family (coins launched in the last 6 h sharing its ticker or name), or None if it's alone.
        rank 1 = the OG: the first launched since the bot started watching (fam_since)."""
        meta = self.fam_meta.get(mint)
        if meta is None:
            return None
        members: dict[str, float] = {}
        for k in self._fam_keys(meta[1], meta[2]):
            for ts, m in self.families.get(k, ()):
                members[m] = ts
        if len(members) < 2:
            return None
        order = sorted(members, key=lambda m: (members[m], m))
        og = order[0]
        usd = self.sol_price.usd

        def mc(m: str) -> float | None:
            s = self.tokens.get(m)
            return s.market_cap_sol * usd if s is not None and s.price_known else None
        lead = max(order, key=lambda m: mc(m) or 0.0)
        out = {"n": len(order), "rank": order.index(mint) + 1, "og": og == mint, "og_mint": og,
               "og_symbol": self.fam_meta[og][1], "after_og_s": round(members[mint] - members[og]),
               "og_known": members[og] - self.fam_since > 120}      # the OG launched after the bot started watching
        if mc(lead):
            out.update(lead_mint=lead, lead_symbol=self.fam_meta[lead][1], lead_mcap_usd=round(mc(lead)))
        if full:
            out["members"] = [{"mint": m, "symbol": self.fam_meta[m][1], "name": self.fam_meta[m][2][:40],
                               "rank": order.index(m) + 1, "age_s": round(self.now - members[m]), "mcap_usd": mc(m),
                               "migrated": bool(self.tokens.get(m) and self.tokens[m].migrated)}
                              for m in [og] + [x for x in sorted(order, key=lambda m: -(mc(m) or 0.0)) if x != og][:9]]
        return out

    def _pulse_token(self, s: TokenState, usd: float, spark: bool) -> dict:
        L = s.launch
        f = self.family(s.mint)
        return {"fam": f and {k: f.get(k) for k in ("n", "rank", "og", "og_symbol", "after_og_s", "og_known", "lead_symbol")},"mint": s.mint, "symbol": s.symbol, "name": (L.name if L else "")[:40], "age": round(s.age(self.now)),
                "mcap_usd": s.market_cap_sol * usd, "progress": s.curve.progress, "buyers": len(s.buyers),
                "buys": s.buys, "sells": s.sells, "flow30": round(s.net_flow_sol(self.now, 30), 2),
                "vol_sol": round(s.volume_sol, 2), "top10": round(s.top_holders_pct(10), 1),
                "dev_pct": round(s.dev_initial_pct(), 1), "dev_sold": s.dev_sold > 0,
                "bundle_pct": round(s.bundle_pct(), 1), "sniper_pct": round(s.sniper_pct(), 1),
                "lk": [int(bool(L and L.twitter)), int(bool(L and L.telegram)), int(bool(L and L.website))],
                "calls": len(s.socials), "score": s.score, "p": s.p, "status": s.decided or "watching",
                "held": s.mint in self.positions, "mayhem": s.mayhem,
                "last_trade_s": round(self.now - s.last_trade_ts) if s.last_trade_ts else None,
                "spark": s.sparkline(30) if spark else []}

    def pulse_view(self, per_column: int = 120) -> dict:
        usd = self.sol_price.usd
        new, stretch = [], []
        for s in self.tokens.values():
            if not s.price_known or s.migrated:
                continue
            (stretch if s.curve.progress >= 0.5 else new).append(s)
        new.sort(key=lambda s: -s.created_ts)
        stretch.sort(key=lambda s: -s.curve.progress)
        grad = []
        for ts, mint, sym, mc in list(self.migrations)[:per_column]:
            if mc < 20_000:                  # an "instant" migration of a coin that never filled its curve: not a graduate
                continue
            s = self.tokens.get(mint)
            grad.append({"mint": mint, "symbol": sym, "graduated_s": round(self.now - ts), "mcap_usd": mc,
                         "age": round(s.age(self.now)) if s else None, "held": mint in self.positions,
                         "top10": round(s.top_holders_pct(10), 1) if s else None,
                         "dev_pct": round(s.dev_initial_pct(), 1) if s else None,
                         "buyers": len(s.buyers) if s else None,
                         "lk": [int(bool(s and s.launch and x)) for x in ((s.launch.twitter, s.launch.telegram,
                                s.launch.website) if s and s.launch else ("", "", ""))]})
        return {"now": self.now, "sol_usd": usd,
                "new": [self._pulse_token(s, usd, i < 40) for i, s in enumerate(new[:per_column])],
                "stretch": [self._pulse_token(s, usd, i < 40) for i, s in enumerate(stretch[:per_column])],
                "graduated": grad}

    # ------------------------------------------------------------------ the desk discusses your notes
    async def discuss_note(self, item_id: str, personas: list[str] | None = None) -> str:
        """Each persona reads a memory item (and the discussion so far) and replies. '' or why not."""
        from . import desk as deskmod

        it = next((i for i in self.memory.items_ if i.get("id") == item_id), None)
        if it is None:
            return "that note is gone"
        pd = self.p.desk
        if not pd.get("note_replies", True):
            return "note replies are off (desk.note_replies)"
        why = deskmod.provider_ready(pd)
        if why:
            return why + " so the desk can reply"
        personas = [p for p in (personas or list(pd.personas)) if p in deskmod.PERSONAS]
        if not personas:
            return "nobody to ask"
        live = None
        s = self.tokens.get(it.get("mint") or "")
        if s is not None and s.price_known:
            live = deskmod.snapshot_for(s, self.now, "note")
        from ..config import Params

        brain = deskmod.Desk(Params({**pd, "enabled": True}))     # the desk's model, awake or not
        self.note_waiting[item_id] = set(personas)
        try:
            brief = await self.desk_brief(item_id)
        except Exception as e:                                     # a briefing problem never blocks the replies
            brief = {"error": f"couldn't build the briefing: {type(e).__name__}"}

        async def one(persona: str) -> None:
            r = await deskmod.reply_note(brain, pd, persona, it, live, brief)
            self.note_stats["calls"] += 1
            self.note_stats["input_tokens"] += r.get("input_tokens", 0)
            self.note_stats["output_tokens"] += r.get("output_tokens", 0)
            self.note_stats["error"] = deskmod.friendly_error(r["error"]) if r.get("error") else ""
            self.memory.add_reply(item_id, persona, r.get("reply", ""), r.get("stance", ""),
                                  deskmod.friendly_error(r["error"]) if r.get("error") else "")
            self.note_waiting.get(item_id, set()).discard(persona)
        try:
            await asyncio.gather(*(one(p) for p in personas))
        finally:
            self.note_waiting.pop(item_id, None)
        self.say("desk", f"the desk replied to “{it.get('title', '')[:60]}”")
        return ""

    async def huddle(self, reason: str = "scheduled") -> str:
        """The team meets: one model call writes a discussion between the bots from the bot's real state, which the
        room plays out at the AI table. Measurement and talk only: a suggestion is for the owner, never applied.
        '' or why not."""
        from . import desk as deskmod
        from ..config import Params

        pd = self.p.desk
        why = deskmod.provider_ready(pd)
        if why:
            return why + " so the desk can meet"
        if self._huddling:
            return "the team is already meeting"
        self._huddling = True
        try:
            brief = await self.desk_brief("")
            brain = deskmod.Desk(Params({**pd, "enabled": True}))
            r = await deskmod.run_huddle(brain, pd, brief, [h.get("takeaway", "") for h in self.huddles], reason, self.plan)
            pin, pout = brain.prices()
            cost = r.get("input_tokens", 0) / 1e6 * pin + r.get("output_tokens", 0) / 1e6 * pout
            self.note_stats["calls"] += 1
            self.note_stats["input_tokens"] += r.get("input_tokens", 0)
            self.note_stats["output_tokens"] += r.get("output_tokens", 0)
            self._last_huddle = self.now
            if r.get("error"):
                self.huddle_error = deskmod.friendly_error(r["error"])
                self.say("error", f"desk huddle failed: {self.huddle_error}")
                return self.huddle_error
            self.huddle_error = ""
            known = {c["key"] for c in self.controls()} | {c["key"] for c in self.advanced_controls()}
            actions = [{**a, "applied": False} for a in r.get("actions", []) if a["key"] in known]   # real settings only
            if r.get("plan") and r["plan"].get("goal"):
                self.plan = {**r["plan"], "updated": time.time()}
                n = 0
                for ex in r["plan"].get("experiments") or []:          # the team's tests go to the lab
                    t = ex.get("test")
                    if t and ex.get("status") in ("proposed", "running") and n < 2:
                        if self.lab_add(t.get("key", ""), t.get("value"), ex.get("name", ""), "team")[0]:
                            n += 1
                if self.persist:
                    try:
                        (DATA / "desk_plan.json").write_text(json.dumps(self.plan))
                    except OSError:
                        pass
            h = {"id": secrets.token_hex(4), "ts": self.now, "wall": time.time(), "reason": reason, "lines": r["lines"],
                 "takeaway": r["takeaway"], "suggestion": r["suggestion"], "actions": actions,
                 "model": deskmod.model_of(pd), "cost_usd": round(cost, 4)}
            self.huddles.append(h)
            if self.persist:
                try:
                    with open(DATA / "huddles.jsonl", "a") as f:
                        f.write(json.dumps(h) + "\n")
                except OSError:
                    pass
            self.say("desk", f"desk huddle: {h['takeaway'][:160]}")
            return ""
        finally:
            self._huddling = False

    def apply_huddle_action(self, hid: str, i: int, who: str = "dashboard") -> str:
        """The owner approves one of the team's setting changes: applied now and saved. Never the agent's call. '' or why not."""
        if who != "dashboard":
            return "only the owner can apply the team's changes"
        h = next((x for x in self.huddles if x.get("id") == hid), None)
        acts = (h or {}).get("actions") or []
        if not h or not 0 <= i < len(acts):
            return "that suggestion is gone"
        a = acts[i]
        if a.get("applied"):
            return "already applied"
        err = self.set_control(a["key"], a["value"])
        if err:
            return err
        err = self.save_controls() if self.persist else ""
        a["applied"] = True
        self.say("info", f"the owner applied the team's change: {a['key']} = {a['value']} ({a['why'][:80]})")
        if self.persist:
            try:
                (DATA / "huddles.jsonl").write_text("".join(json.dumps(x) + "\n" for x in self.huddles))
            except OSError:
                pass
        return err

    def _maybe_huddle(self) -> None:
        """Every desk.huddle_minutes while the AI desk is awake (0 = only when the owner calls one)."""
        mins = float(self.p.desk.get("huddle_minutes", 30) or 0)
        if mins <= 0 or not self.feed.realtime or self._huddling or not (self.desk and self.desk.enabled):
            return
        if self.now - self._last_huddle >= mins * 60:
            self._last_huddle = self.now                         # (also on failure: no retry storm)
            asyncio.ensure_future(self.huddle("scheduled"))

    async def desk_brief(self, item_id: str = "") -> dict:
        """The bot's real state, compact, so the personas can answer questions about it ("what's holding us
        back?") with numbers instead of guessing. Only facts the dashboard already shows; no keys or wallets."""
        from .edge import check, exit_whatifs

        if self.persist:
            from .report import load_trades
            rows = await asyncio.to_thread(load_trades, DATA, 14, self.mode)
        else:
            rows = list(self.book.closed)
        edge = []
        for src in ("late", "sniper", "copy", "callout", "manual"):
            if any((t.get("source") or "").split(":")[0] == src for t in rows):
                v = await asyncio.to_thread(check, rows, src, 1000)
                edge.append({k: (round(x, 3) if isinstance(x, float) else x) for k, x in v.items()
                             if k in ("label", "n", "ret_pct", "mean_pct", "median_pct", "win_rate", "lo_pct", "hi_pct",
                                      "without_best3_sol", "pnl_sol", "verdict", "caveats", "span_days")})
        whatifs = await asyncio.to_thread(exit_whatifs, rows, "late")
        mine = [t for t in rows if t.get("source") == "manual"]
        owner = {} if not mine else {
            "trades": len(mine), "total_sol": round(sum(t["pnl"] for t in mine), 3),
            "sold_at_50pct_plus": {"trades": sum(1 for t in mine if t["pnl_pct"] >= 50), "sol": round(sum(t["pnl"] for t in mine if t["pnl_pct"] >= 50), 3)},
            "losers_below_minus_40pct": {"trades": sum(1 for t in mine if t["pnl_pct"] <= -40), "sol": round(sum(t["pnl"] for t in mine if t["pnl_pct"] <= -40), 3),
                                         "their_best_peak_pct": round(max([t.get("peak_gain_pct") or 0 for t in mine if t["pnl_pct"] <= -40] or [0]))},
            "own_stop_set_pct": self._manual_cfg().stop_loss_pct or None}
        s = self.summary()
        recent = [{"symbol": c.get("symbol"), "strategy": "manual: the owner's own trade, on the owner's exits only"
                   if c.get("source") == "manual" else (c.get("source") or "").split(":")[0],
                   "pnl_pct": round(c.get("pnl_pct") or 0, 1), "exit": (c.get("exit") or "")[:60],
                   "held_s": round((c.get("closed") or 0) - (c.get("opened") or 0))} for c in self.book.closed[-12:]]
        lab = self.lab.view("late")
        notes = [{"title": (i.get("title") or "")[:80], "note": (i.get("note") or "")[:200], "kind": i.get("kind"),
                  "mint": i.get("mint") or None} for i in self.memory.items_[-8:] if i.get("id") != item_id]
        return {
            "mode": self.mode, "equity_sol": round(self.equity(), 3), "start_sol": round(self.book.start_sol, 3),
            "halted": self.book.halted or None, "paused": self.paused, "entries_blocked": self.entries_blocked() or None,
            "risk": self.risk_info().get("name"), "today_pnl_sol": round(self.book.day_pnl, 3),
            "strategies_on": [k for k, on in (("graduation plays", self.p.late.enabled), ("sniper", self.p.entry.enabled),
                                              ("copy", self.p.copy.enabled), ("callouts", self.p.callouts.enabled)) if on],
            "edge_check_14d": edge, "session": {k: s.get(k) for k in ("closed", "win_rate", "realized_pnl_sol", "launches", "entries")},
            "top_rejections": [[why, n] for why, n in self.rejects.most_common(10)],
            "recent_trades": recent,
            "exit_lab": [{"rule": v["variant"], "n": v["n"], "mean_pct": round(v["mean_pct"], 1)} for v in (lab.get("variants") or [])[:7]],
            # plain names next to the keys: a model read "entry.enabled: false" as "all entries are off" (it's only the sniper)
            "strategy_switches": {"graduation plays (late.enabled)": self.p.late.enabled, "early sniper (entry.enabled)": self.p.entry.enabled,
                                  "copy trading (copy.enabled)": self.p.copy.enabled, "callouts (callouts.enabled)": self.p.callouts.enabled},
            "settings": {f"{c['label']} ({c['key']})": c["value"] for c in self.controls()},
            "feed": {"host": getattr(self.feed, "host", ""), "lag_s": getattr(self.feed, "lag_s", None),
                     "degraded": getattr(self.feed, "degraded_reason", "") or None},
            "ai_desk_voting": bool(self.desk and self.desk.enabled), "recent_notes": notes,
            "graduation_exit_whatifs": whatifs, "owner_manual_trading": owner, "lab": self.lab_brief(),
            "trending_on_other_chains": self.chains.names() if getattr(self, "chains", None) else {},
            "other_chain_bot": self.xchain.brief(),      # paper trades on BNB Chain / Base / Solana DEX coins
        }

    # ------------------------------------------------------------------ hand your positions to the bots

    def hand_over(self, mint: str = "", on: bool = True, who: str = "dashboard") -> tuple[int, str]:
        """Give your positions (one, or all with mint='') to the bots, or take them back. (count, message)."""
        mints = [mint] if mint else [m for m, p in self.positions.items() if p.source == "manual"]
        n = 0
        for m in mints:
            pos = self.positions.get(m)
            if pos is None or pos.source != "manual":
                continue
            if on and not pos.bot:
                s = self.tokens.get(m)
                pos.bot = "ride"
                pos.handed_price = pos.handed_peak = s.curve.price if s is not None and s.price_known else pos.entry_price
                pos.ride_tp = False
                n += 1
            elif not on and pos.bot:
                pos.bot = ""
                n += 1
        if mint and not n:
            pos = self.positions.get(mint)
            return 0, ("no open position in that coin" if pos is None else
                       "that's the bot's own position: it already manages it" if pos.source != "manual" else
                       "already handed over" if on else "you already manage it")
        if n:
            self.save_state()
            self.say("agent" if who == "agent" else "info",
                     f"{n} of your position(s) {'handed to the bots: they ride it for a runner' if on else 'back to you: only your exits apply'}")
        return n, ""

    def set_away(self, on: bool, who: str = "dashboard") -> str:
        """Away: the bots manage all your positions, and any your limit orders open; back: you take them back."""
        self.away = bool(on)
        n, _ = self.hand_over("", on, who)
        self.save_state()
        return (f"Away mode on: the bots manage your {n} position(s) and any your orders open" if on
                else f"Welcome back: {n} position(s) are yours again")

    async def sell_now(self, mint: str) -> None:
        if mint in self.positions and mint in self.tokens:
            await self._sell(self.tokens[mint], self.positions[mint], 1.0, "manual sell")

    def daily_digest(self, day: str) -> str:
        """One day in a few lines: each bot's P&L, the account, the lab, the other-chain checklist."""
        rows = [c for c in self.book.closed if time.strftime("%Y-%m-%d", time.gmtime(c["closed"])) == day]
        lines = [f"📊 {day} (UTC) closed"]
        by: dict[str, list] = {}
        for c in rows:
            by.setdefault("other chains" if c.get("source") == "chains" else c["source"], []).append(c)
        for src, cs in sorted(by.items(), key=lambda kv: sum(c["pnl"] for c in kv[1])):
            pnl = sum(c["pnl"] for c in cs)
            extra = ""
            reals = [c["real"]["pnl_usd"] for c in cs if (c.get("real") or {}).get("complete")]
            if src == "other chains" and reals:
                extra = f" (real router prices ${sum(reals):+.2f} on {len(reals)})"
            lines.append(f"• {src}: {len(cs)} trades, {pnl:+.3f} SOL, {sum(c['pnl'] > 0 for c in cs)} won{extra}")
        if not rows:
            lines.append("• no trades closed")
        eq = self.equity()
        lines.append(f"Balance {eq:.3f} SOL (start {self.book.start_sol:g}) · kill switch at {self.kill_at():.3f} · "
                     f"day {self.book.day_pnl:+.3f} SOL of a {self.p.capital.daily_loss_limit_sol:g} limit · risk {self.risk_info().get('name')}")
        done = [x for x in self.xlab.items if x.get("finished") and time.strftime("%Y-%m-%d", time.gmtime(x["finished"])) == day]
        for x in done[:3]:
            r = x.get("result") or {}
            lines.append(f"Lab: {x['key']} {x['now']} → {x['value']}: {r.get('verdict') or r.get('error') or '?'}"
                         + (f" (better on {r['days_better']} of {r['days']} days)" if r.get("days") else ""))
        try:
            rd = self.xchain.readiness()
            lines.append(f"Other chains real-money checklist: {rd['passed']} of {len(rd['checks'])}")
        except Exception:
            pass
        if self.book.halted:
            lines.append(f"HALTED: {self.book.halted}")
        return "\n".join(lines)

    def kill_at(self) -> float:
        """Equity (SOL) at which the drawdown kill switch trips."""
        return (self.book.kill_base or self.book.start_sol) * (1 - self.p.capital.max_drawdown_pct / 100)

    def clear_halt(self, who: str = "dashboard") -> str:
        """The owner lifts the kill switch. If the account is still past the drawdown limit, the limit is measured
        from today's equity from now on, so it doesn't trip again at once. Never the agent's call. '' or why not."""
        if who != "dashboard":
            return "only the owner can lift the kill switch, from the dashboard"
        if not self.book.halted:
            return "trading isn't halted"
        eq, base = self.equity(), self.book.kill_base or self.book.start_sol
        if base and (1 - eq / base) * 100 >= self.p.capital.max_drawdown_pct - 1:
            self.book.kill_base = eq
        was, self.book.halted = self.book.halted, ""
        self._snap_cache = None
        self.save_state()
        self.say("info", f"kill switch lifted by the owner (was: {was}); it trips again at {self.kill_at():.3f} SOL")
        return ""

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

    def advanced_controls(self) -> list[dict]:
        """Every other live-adjustable setting: type, current and default value, help text from the example file."""
        curated = {c[0] for c in CONTROLS} | set(RISK_KEYS)
        help_ = example_help()
        out = []
        for path, default in _leaves(_example_sniper()):
            section = path.split(".")[0]
            if section not in ADVANCED_SECTIONS or path in ADVANCED_SKIP or path in curated:
                continue
            if isinstance(default, bool):
                typ = "bool"
            elif isinstance(default, int):
                typ = "int"
            elif isinstance(default, float):
                typ = "float"
            elif path in ADVANCED_ENUMS:
                typ = "enum:" + ",".join(ADVANCED_ENUMS[path])
            else:
                continue                                # lists, URLs, names: config file only
            try:
                cur = self._get(path)
            except (KeyError, TypeError):
                continue
            out.append({"key": path, "section": section, "type": typ, "value": cur, "default": default,
                        "changed": cur != default, "help": help_.get(f"sniper.{path}", "")})
        return out

    def set_control(self, key: str, value, owner: bool = True) -> str:
        """Validate and apply one dashboard setting. Returns '' or an error message. owner=False (the AI agent):
        only the curated CONTROLS, and the risk dial's Normal baseline stays the owner's."""
        spec = next((c for c in CONTROLS if c[0] == key), None)
        if spec is None and owner:
            adv = next((a for a in self.advanced_controls() if a["key"] == key), None)
            if adv is not None:
                lo = 0 if adv["type"] in ("int", "float") and float(adv["default"]) >= 0 else None
                spec = (key, adv["type"], lo, 1e12 if lo is not None else None, key, adv["help"])
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
        if not any(c[0] == key for c in CONTROLS):
            self._advanced_set.add(key)                  # saved by "Save to config" from now on
        if owner and key in self.risk_base:              # the owner set it: the dial's baseline follows
            mult = {"sizing.base_usd": 1, "sizing.max_usd": 1, "capital.max_open_positions": 2,
                    "capital.daily_loss_limit_sol": 3}[key]
            m = RISK_LEVELS[self.risk_level][mult]
            self.risk_base[key] = round(v / m) if key == "capital.max_open_positions" else v / m
        self.say("info", f"setting changed: {label} = {v}")
        return ""

    # ------------------------------------------------------------------ risk dial
    def _get(self, key: str):
        node = self.p
        for k in key.split("."):
            node = node[k]
        return node

    def _set(self, key: str, value) -> None:
        *path, last = key.split(".")
        node = self.p
        for k in path:
            node = node[k]
        node[last] = value

    def risk_values(self, level: int) -> dict:
        _, m, pm, dm = RISK_LEVELS[level]
        b = self.risk_base
        return {"sizing.base_usd": round(b["sizing.base_usd"] * m, 2),
                "sizing.max_usd": round(b["sizing.max_usd"] * m, 2),
                "capital.max_open_positions": max(1, round(b["capital.max_open_positions"] * pm)),
                "capital.daily_loss_limit_sol": round(b["capital.daily_loss_limit_sol"] * dm, 4)}

    @property
    def risk_max_level(self) -> int:
        return int((self.p.get("risk") or {}).get("max_level", 5))

    def _apply_risk(self, level: int) -> None:
        for k, v in self.risk_values(level).items():
            self._set(k, v)
        self.risk_level = level

    def set_risk(self, level, who: str = "dashboard", reason: str = "") -> str:
        """Turn the dial. Returns '' or why not. The dashboard's change is saved to params.yaml; anyone else's
        lasts until a restart."""
        try:
            level = int(level)
        except (TypeError, ValueError):
            return "risk level must be 1-5"
        if level not in RISK_LEVELS:
            return "risk level must be 1-5"
        if level > self.risk_max_level:
            return f"risk level {level} is above risk.max_level ({self.risk_max_level}) in the config"
        old = self.risk_level
        self._apply_risk(level)
        v = self.risk_values(level)
        self.say("agent" if who == "agent" else "info",
                 f"risk dial {old} -> {level} {RISK_LEVELS[level][0]}: "
                 f"${v['sizing.base_usd']:g}-{v['sizing.max_usd']:g} per trade, {v['capital.max_open_positions']} positions, {v['capital.daily_loss_limit_sol']:g} SOL "
                 f"daily loss limit" + (f" | {reason}" if reason else ""))
        if who == "dashboard":
            try:
                self.save_setting("risk.level", level)
            except OSError as e:
                return f"applied, but not saved: {e}"
        return ""

    def risk_info(self) -> dict:
        levels = [{"level": lv, "name": RISK_LEVELS[lv][0],
                   **{k.split(".")[1]: v for k, v in self.risk_values(lv).items()}} for lv in RISK_LEVELS]
        return {"level": self.risk_level, "name": RISK_LEVELS[self.risk_level][0], "max_level": self.risk_max_level,
                "levels": levels}

    def save_controls(self, path: Path | None = None) -> str:
        """Write the dashboard-controllable settings into config/params.yaml (other keys untouched). Dial-scaled
        settings are saved at their Normal value, with the dial level beside them, so a restart doesn't scale twice."""
        import yaml

        path = path or ROOT / "config" / "params.yaml"
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
        data = data or {}
        sn = data.setdefault("sniper", {}) or {}
        data["sniper"] = sn
        adv = [{"key": a["key"], "value": a["value"]} for a in self.advanced_controls() if a["key"] in self._advanced_set]
        for c in self.controls() + adv + [{"key": "risk.level", "value": self.risk_level}]:
            *keys, last = c["key"].split(".")
            node = sn
            for k in keys:
                node = node.setdefault(k, {}) or {}
            node[last] = self.risk_base.get(c["key"], c["value"])
            # re-attach in case setdefault returned a fresh {} for a None value
            parent = sn
            for k in keys[:-1]:
                parent = parent[k]
            if keys:
                parent[keys[-1]] = node
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.say("info", f"settings saved to {path.name}")
        return ""

    def save_setting(self, key: str, value, path: Path | None = None) -> None:
        """Write one sniper.* value into config/params.yaml (other keys untouched)."""
        import yaml

        path = path or ROOT / "config" / "params.yaml"
        data = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
        node = data.setdefault("sniper", {}) or {}
        data["sniper"] = node
        *keys, last = key.split(".")
        for k in keys:
            nxt = node.get(k) or {}
            node[k] = nxt
            node = nxt
        node[last] = value
        path.write_text(yaml.safe_dump(data, sort_keys=False))

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
            "curve_pct": s.curve.progress * 100, "mcap_usd": s.market_cap_sol * self.sol_price.usd,
            "price": s.curve.price, "price_known": s.price_known,
            "socials": {"twitter": L.twitter if L else "", "telegram": L.telegram if L else "",
                        "website": L.website if L else ""},
            "creator": s.creator, "checklist": gate_checklist(s, self.now, self.p.entry, ctx),
            "p": p, "ev_pct": self._ev(p) if p is not None else None,
            "drivers": self.model.drivers(feats) if self.model else [],
            "features": {k: round(v, 4) for k, v in feats.items()},
            "cluster": s.cluster, "desk": s.desk, "family": self.family(mint, full=True),
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

    WHALE_SOL = 2.0          # a single trade this big gets a marker on the live chart

    @staticmethod
    def _mc_at(s: TokenState, price: float) -> float | None:
        """The coin's market cap (SOL) at a given price: market cap scales with price (Mayhem coins included)."""
        return s.market_cap_sol * price / s.curve.price if s is not None and s.price_known and s.curve.price > 0 and price else None

    def _study_wallets(self) -> dict[str, str]:
        """Wallets the wallet study qualified (data/wallets/view.json), re-read at most every 5 minutes."""
        c = getattr(self, "_study_cache", None)
        if c and time.time() - c[0] < 300:
            return c[1]
        out: dict[str, str] = {}
        try:
            from ..wallets.recorder import DATA as WDATA
            v = json.loads((WDATA / "view.json").read_text())
            for r in v.get("wallets", []):
                out[r["wallet"]] = f"study wallet{' (cluster ' + str(r['cluster']) + ')' if r.get('cluster') else ''}"
        except (OSError, ValueError, KeyError, ImportError):
            pass
        self._study_cache = (time.time(), out)
        return out

    # ------------------------------------------------------------------ the lab: the team tests one change at a time
    def lab_baseline(self) -> dict:
        """The settings the lab compares against: what the graduation play runs now."""
        from .lab import EXECUTION, TESTABLE
        out = {}
        for k in TESTABLE:
            sec, key = k.split(".", 1)
            v = (self.p.get(sec) or {}).get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = v
        for k in EXECUTION:                              # how its orders land, so a replay fills like this bot
            sec, key = k.split(".", 1)
            v = (self.p.get(sec) or {}).get(key)
            if v is not None and not isinstance(v, bool):
                out[k] = list(v) if isinstance(v, (list, tuple)) else v
        return out

    def lab_add(self, key: str, value, why: str, by: str) -> tuple[dict | None, str]:
        x, err = self.xlab.add(key, value, why, by, time.time(), self.lab_baseline())
        if x:
            self.say("info", f"lab: queued {key} {x['now']} -> {x['value']} ({by}: {why[:80]})")
        return x, err

    def _lab_step(self) -> None:
        from .lab import free_mb
        cfg = self.p.get("lab") or {}
        if self.xlab.dir is None or not cfg.get("enabled", True) or (self._lab_task and not self._lab_task.done()):
            return
        if self.xlab.started_today(time.time()) >= int(cfg.get("max_per_day", 4)):
            return
        mb = free_mb()
        if mb is not None and mb < float(cfg.get("min_free_mb", 8000)):   # a replay holds ~5 GB: never squeeze the bot
            return
        if not self.xlab.queued() and time.time() >= self._lab_auto_at:
            self._lab_auto_at = time.time() + float(cfg.get("auto_every_min", 360)) * 60
            c = self.xlab.auto_candidate(self.lab_baseline(), time.time())
            if c:
                self.lab_add(c[0], c[1], "routine check: one step either side of the current setting", "quant")
        r = self.xlab.running()                       # a run in its own service, from before a restart: pick it up
        if r is not None:
            self._lab_task = asyncio.create_task(self._lab_finish(r, self._lab_poll(r)))
            return
        q = self.xlab.queued()
        if q:
            self._lab_task = asyncio.create_task(self._lab_run(q[0]))

    async def _lab_poll(self, x: dict) -> dict:
        """Wait for a run in its own service: its result file, or the service stopping without one."""
        out = DATA / "lab" / f"{x['id']}.result.json"
        gone = 0
        limit = float((self.p.get("lab") or {}).get("timeout_min", 180)) * 60    # a test replays several days
        while time.time() - (x.get("started") or time.time()) < limit:
            if out.exists():
                try:
                    return json.loads(out.read_text())
                except ValueError:
                    pass                                 # (still being written)
            p = await asyncio.create_subprocess_exec("systemctl", "--user", "is-active", "--quiet", x["unit"])
            gone = gone + 1 if await p.wait() != 0 else 0
            if gone >= 2 and not out.exists():           # stopped twice in a row and nothing written
                return {"error": "the run stopped without a result (out of memory, or killed)"}
            await asyncio.sleep(10)
        stop = await asyncio.create_subprocess_exec("systemctl", "--user", "stop", x["unit"])
        await stop.wait()
        return {"error": f"took over {limit / 60:.0f} minutes: stopped"}

    async def _lab_finish(self, x: dict, work) -> None:
        try:
            res = await work
        except Exception as e:                          # never leave it "running"
            res = {"error": f"{type(e).__name__}: {e}"}
        x["finished"] = time.time()
        if res.get("error"):
            x["status"], x["result"] = "failed", {"error": str(res["error"])[:300]}
            self.say("error", f"lab: the {x['key']} test failed: {x['result']['error'][:120]}")
        else:
            x["status"], x["result"] = "done", res
            n, c = res["now"], res["change"]
            self.say("info", f"lab: {x['key']} {x['now']} -> {x['value']}: {res['verdict']} "
                             f"({c['pnl_sol']:+.3f} vs {n['pnl_sol']:+.3f} SOL, {c['trades']} vs {n['trades']} trades, "
                             f"{res['better_blocks']} of {res['blocks']} blocks better)")
        self.xlab.save()

    async def _lab_run(self, x: dict) -> None:
        import shutil
        import sys

        cfg = self.p.get("lab") or {}
        x["status"], x["started"] = "running", time.time()
        x["baseline"] = self.lab_baseline()              # compared with the settings as they are now, landing like the bot
        x["now"] = x["baseline"].get(x["key"], x["now"])
        out = DATA / "lab" / f"{x['id']}.result.json"
        out.unlink(missing_ok=True)
        cmd = [sys.executable, "-m", "meme_trader.sniper", "lab-run", x["id"]]
        if cfg.get("detach", True) and shutil.which("systemd-run"):
            # its own short-lived service: survives the bot restarting, and can't take more than memory_max_mb
            x["unit"] = f"meme-lab-{x['id']}"
            self.xlab.save()
            self.think("quant" if x["by"] in ("quant", "team", "you") else x["by"],
                       f"lab: testing {x['key'].split('.')[-1]} {x['now']} -> {x['value']} on each recorded day", "", "", "work")
            try:
                p = await asyncio.create_subprocess_exec(
                    "systemd-run", "--user", "--quiet", "--collect", f"--unit={x['unit']}",
                    "-p", f"MemoryMax={int(cfg.get('memory_max_mb', 6000))}M", "-p", "Nice=19",
                    f"--working-directory={ROOT}", *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await p.communicate()
                if p.returncode:
                    raise RuntimeError((err or b"").decode(errors="replace")[-200:] or f"systemd-run exit {p.returncode}")
            except Exception as e:
                await self._lab_finish(x, asyncio.sleep(0, {"error": f"couldn't start the run: {e}"}))
                return
            await self._lab_finish(x, self._lab_poll(x))
            return
        self.xlab.save()
        self.think("quant" if x["by"] in ("quant", "team", "you") else x["by"],
                   f"lab: testing {x['key'].split('.')[-1]} {x['now']} -> {x['value']} on each recorded day", "", "", "work")
        out = DATA / "lab" / f"{x['id']}.result.json"
        out.unlink(missing_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec("nice", "-n", "19", sys.executable, "-m", "meme_trader.sniper", "lab-run", x["id"],
                                                        cwd=str(ROOT), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=float(cfg.get("timeout_min", 180)) * 60)
            res = json.loads(out.read_text()) if out.exists() else {"error": (err or b"").decode(errors="replace")[-300:] or f"exit {proc.returncode}"}
        except Exception as e:                          # never leave it "running"
            res = {"error": f"{type(e).__name__}: {e}"}
        x["finished"] = time.time()
        if res.get("error"):
            x["status"], x["result"] = "failed", {"error": str(res["error"])[:300]}
            self.say("error", f"lab: the {x['key']} test failed: {x['result']['error'][:120]}")
        else:
            x["status"], x["result"] = "done", res
            n, c = res["now"], res["change"]
            self.say("info", f"lab: {x['key']} {x['now']} -> {x['value']}: {res['verdict']} "
                             f"({c['pnl_sol']:+.3f} vs {n['pnl_sol']:+.3f} SOL, {c['trades']} vs {n['trades']} trades, "
                             f"{res['better_blocks']} of {res['blocks']} blocks better)")
        self.xlab.save()

    def lab_brief(self) -> dict:
        """What the team reads about its lab at a meeting."""
        from .lab import TESTABLE, landed_like_bot
        def row(x):
            r = x.get("result") or {}
            return {"test": f"{x['key']}: {x['now']} -> {x['value']}", "by": x["by"], "why": x.get("why", "")[:120], "status": x["status"],
                    "verdict": r.get("verdict"), "now": r.get("now"), "change": r.get("change"),
                    "blocks_better_of": f"{r.get('better_blocks')} of {r.get('blocks')}" if r.get("blocks") else None,
                    "error": r.get("error"),
                    "days_better_of": f"{r.get('days_better')} of {r.get('days')}" if r.get("days") else None,
                    "caveat": None if landed_like_bot(x) else "judged with instant fills (before replays landed orders like the bot): weaker"}
        v = self.xlab.view()
        return {"testable": {k: f"{lo:g}-{hi:g}" for k, (lo, hi) in TESTABLE.items()}, "current": self.lab_baseline(),
                "tries": v["tries"], "running": row(v["running"]) if v["running"] else None,
                "queued": [row(x) for x in v["queued"]], "results": [row(x) for x in v["done"][:8]]}

    def _known_wallets(self) -> dict[str, tuple[str, str]]:
        """wallet -> (kind, name) for the KOL list and the wallet study's qualified wallets, rebuilt every 5 min."""
        c = getattr(self, "_known_cache", None)
        if c and time.time() - c[0] < 300:
            return c[1]
        out = {w: ("smart", lab) for w, lab in self._study_wallets().items()}
        out.update({w: ("kol", n) for w, n in (self.kols.get("kols") or {}).items()})
        self._known_cache = (time.time(), out)
        return out

    def _note_known(self, e, s: TokenState, kind: str, name: str) -> None:
        held = None
        k = (e.trader, e.mint)
        if e.side == "buy":
            self._kol_first.setdefault(k, e.ts)
            if len(self._kol_first) > 20000:
                for x in list(self._kol_first)[:5000]:
                    del self._kol_first[x]
        elif k in self._kol_first:
            held = round(e.ts - self._kol_first.pop(k))
        self.kol_tape.append({"t": round(e.ts, 1), "wallet": e.trader, "kind": kind, "name": name, "mint": e.mint,
                              "symbol": s.symbol, "side": e.side, "sol": round(e.sol, 3),
                              "mc": round(s.market_cap_sol, 1) if s.price_known else None, "held_s": held})

    def kol_view(self, minutes: float = 60) -> dict:
        """The Charts tab's KOL tracker: their latest trades on any coin the bot sees, and the coins they're in."""
        cut = self.now - minutes * 60
        recent = [x for x in self.kol_tape if x["t"] >= cut]
        coins: dict[str, dict] = {}
        for x in recent:
            c = coins.setdefault(x["mint"], {"mint": x["mint"], "symbol": x["symbol"], "names": [], "buys": 0, "sells": 0,
                                              "net_sol": 0.0, "first_t": x["t"], "first_mc": x["mc"], "kols": 0})
            if x["name"] not in c["names"]:
                c["names"].append(x["name"])
                c["kols"] += x["kind"] == "kol"
            c["buys" if x["side"] == "buy" else "sells"] += 1
            c["net_sol"] = round(c["net_sol"] + (x["sol"] if x["side"] == "buy" else -x["sol"]), 3)
        for c in coins.values():
            s = self.tokens.get(c["mint"])
            c["mc_now"] = round(s.market_cap_sol, 1) if s is not None and s.price_known else None
            c["migrated"] = bool(s and s.migrated)
            f = self.family(c["mint"])
            c["fam"] = f and {k: f.get(k) for k in ("n", "rank", "og", "og_symbol", "after_og_s", "og_known")}
        held = [x["held_s"] for x in recent if x["held_s"] is not None]
        return {"tape": [x for x in reversed(recent)][:80],
                "coins": sorted(coins.values(), key=lambda c: (-len(c["names"]), -c["buys"]))[:15],
                "active": len({x["wallet"] for x in recent}), "known": len(self._known_wallets()),
                "kols_loaded": len(self.kols.get("kols") or {}), "median_hold_s": sorted(held)[len(held) // 2] if held else None,
                "minutes": minutes}

    def hot_names(self, minutes: float = 60, limit: int = 12) -> list[dict]:
        """Names and tickers launched most in the last hour (the copycat waves), with the OG and the biggest now."""
        cut, usd = self.now - minutes * 60, self.sol_price.usd
        seen: set[str] = set()
        out = []
        for k, q in self.families.items():
            members = [(ts, m) for ts, m in q if ts >= cut]
            if len(members) < 3:
                continue
            ms = frozenset(m for _, m in members)
            og = min(q, key=lambda x: x[0])[1]
            if og in seen:
                continue
            seen.add(og)
            def mc(m):
                s = self.tokens.get(m)
                return s.market_cap_sol if s is not None and s.price_known else 0.0
            lead = max(ms, key=mc)
            out.append({"key": k, "n": len(members), "og_mint": og, "og_symbol": self.fam_meta.get(og, (0, "?"))[1],
                        "lead_mint": lead if mc(lead) else None, "lead_symbol": self.fam_meta.get(lead, (0, "?"))[1],
                        "lead_mc": round(mc(lead), 1) if mc(lead) else None, "lead_mc_usd": round(mc(lead) * usd) if mc(lead) and usd else None,
                        "newest_s": round(self.now - max(ts for ts, _ in members))})
        return sorted(out, key=lambda r: -r["n"])[:limit]

    def chart_data(self, mint: str, since: float = 0.0) -> dict | None:
        """A coin's live chart: every trade the bot has seen on it (price in SOL per token, newest last), and
        markers: your and the bots' entries and exits on it (this run's ledger), and trades by wallets worth
        seeing (the dev, tracked leaders, the wallet study's qualified wallets, smart-money signals, whales).
        since: only trades after this time (the dashboard streams updates); markers always come whole."""
        s = self.tokens.get(mint)
        if s is None:
            return None
        tr = list(s.trades)
        pts = [[round(t, 2), p] for t, p, *_ in tr if t > since and p > 0]
        # who's who on this coin
        named: dict[str, tuple[str, str]] = {}
        for w, lab in self._study_wallets().items():
            named[w] = ("smart", lab)
        for x in s.socials:
            if x.source == "wallet":
                named[x.author] = ("smart", "smart-money wallet")
        for w, name in (self.kols.get("kols") or {}).items():
            named[w] = ("kol", f"KOL {name}")
        for w, lead in self.leaders.leaders.items():
            named[w] = ("smart", f"copy leader {getattr(lead, 'label', '') or ''}".strip())
        if s.creator:
            named[s.creator] = ("dev", "dev")
        marks = []
        for t, p, side, sol, who in tr:
            kind, lab = named.get(who, (None, ""))
            if kind is None and sol >= self.WHALE_SOL:
                kind, lab = "whale", "whale"
            if kind:
                marks.append({"t": round(t, 2), "p": p, "side": side, "who": kind,
                              "label": f"{lab} {'bought' if side == 'buy' else 'sold'} {sol:.2f} SOL", "wallet": who})
        keep = [m for m in marks if m["who"] != "whale"]   # dev, KOLs and smart wallets always; whales, the latest
        marks = sorted(keep[-80:] + [m for m in marks if m["who"] == "whale"][-30:], key=lambda m: m["t"])

        def at(t: float) -> float:                  # the coin's price at a moment (nearest trade before it)
            best = tr[0][1] if tr else 0.0
            for tt, p, *_ in tr:
                if tt > t:
                    break
                best = p
            return best

        def owner(src: str, bot: str = "") -> tuple[str, str]:
            if src == "manual":
                return ("you", "you") if not bot else ("you", "you (bots riding it)")
            return ("bot", f"bot ({src})")

        for r in self.book.closed:
            if r["mint"] != mint:
                continue
            k, lab = owner(r["source"])
            marks.append({"t": round(r["opened"], 2), "p": at(r["opened"]), "side": "buy", "who": k, "label": f"{lab} bought {r['cost']:.2f} SOL"})
            marks.append({"t": round(r["closed"], 2), "p": at(r["closed"]), "side": "sell", "who": k,
                          "label": f"{lab} sold, {r['pnl_pct']:+.0f}% ({r['exit'][:40]})"})
        pos = self.positions.get(mint)
        if pos is not None:
            k, lab = owner(pos.source, pos.bot or "")
            marks.append({"t": round(pos.opened_at, 2), "p": pos.entry_price, "side": "buy", "who": k,
                          "label": f"{lab} bought {pos.initial_cost_sol:.2f} SOL (open)"})
            for t, _sol, _tok, p in pos.adds or []:
                marks.append({"t": round(t, 2), "p": p, "side": "buy", "who": k, "label": f"{lab} added {_sol:.2f} SOL"})
            for t, why, _tok, sol in pos.exits or []:
                marks.append({"t": round(t, 2), "p": at(t), "side": "sell", "who": k, "label": f"{lab} sold part for {sol:.3f} SOL ({why[:40]})"})
        marks.sort(key=lambda m: m["t"])
        return {"mint": mint, "symbol": s.symbol, "pts": pts, "marks": marks, "now": round(self.now, 2), "fam": self.family(mint),
                "mcap_usd": round(s.market_cap_sol * self.sol_price.usd), "curve_pct": round(s.curve.progress * 100, 1),
                "mc_per_px": s.market_cap_sol / s.curve.price if s.price_known and s.curve.price > 0 else None,
                "age_s": round(s.age(self.now)), "migrated": s.migrated,
                "price": s.curve.price, "entry": pos.entry_price if pos else None,
                "position": None if pos is None else {"source": pos.source, "bot": pos.bot or "",
                                                       "gain_pct": round(pos.gain_pct(s.curve.price), 1),
                                                       "cost_sol": pos.initial_cost_sol}}

    async def refresh_kols(self) -> str:
        """The owner asked for the KOL list: fetch kolscan.io's leaderboard once. '' or why not."""
        from . import kols as kolmod
        self.kols, err = await kolmod.refresh(DATA / "kols.json")
        self._known_cache = None                                   # the tracker picks up the new list now
        return err

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

    def desk_brain(self) -> dict:
        """Which model the desk uses, and what a review (one vote per persona) costs on it."""
        from .desk import CLAUDE_MODELS, PROVIDERS, Desk, model_of, provider_of, provider_ready

        pin, pout = Desk(self.p.desk, client=object()).prices()   # (a client stand-in: just for the price list)
        calls = int(getattr(self.desk, "calls", 0) or 0)
        # measured once there are votes; before that, what a vote used here on 2026-10-04 (~2¢ a review on Opus)
        tin = getattr(self.desk, "input_tokens", 0) / calls if calls else 700
        tout = getattr(self.desk, "output_tokens", 0) / calls if calls else 150
        per_vote = tin / 1e6 * pin + tout / 1e6 * pout
        return {"provider": provider_of(self.p.desk), "model": model_of(self.p.desk), "base_url": self.p.desk.get("base_url") or "",
                "ready": provider_ready(self.p.desk), "per_vote_usd": round(per_vote, 5),
                "per_review_usd": round(per_vote * len(self.p.desk.personas), 4), "measured": bool(calls),
                "providers": {k: {kk: v[kk] for kk in ("label", "key", "base_url", "model", "note")} for k, v in PROVIDERS.items()},
                "claude_models": {k: v[0] for k, v in CLAUDE_MODELS.items()}}

    def set_desk_prices(self, pin: float, pout: float, who: str = "dashboard") -> None:
        """Another provider's price for its cost estimate ($ per million tokens), saved with the model."""
        self.p.desk["other_price_in_per_mtok"], self.p.desk["other_price_out_per_mtok"] = round(pin, 4), round(pout, 4)
        if who == "dashboard" and self.persist:
            try:
                for k in ("other_price_in_per_mtok", "other_price_out_per_mtok"):
                    self.save_setting(f"desk.{k}", self.p.desk[k])
            except OSError:
                pass

    def set_desk_brain(self, provider: str, model: str = "", base_url: str = "", who: str = "dashboard") -> str:
        from .desk import PROVIDERS

        if provider not in PROVIDERS:
            return f"unknown provider {provider!r}"
        model, base_url = str(model or "").strip()[:120], str(base_url or "").strip()[:300]
        if base_url and not base_url.startswith(("http://", "https://")):
            return "the server URL must start with http:// or https://"
        self.p.desk["provider"], self.p.desk["model"] = provider, model or PROVIDERS[provider]["model"]
        self.p.desk["base_url"] = base_url
        if who == "dashboard" and self.persist:
            try:
                for k in ("provider", "model", "base_url"):
                    self.save_setting(f"desk.{k}", self.p.desk[k])
            except OSError as e:
                return f"applied, but not saved: {e}"
        if self.desk and self.desk.enabled:
            err = self._set_desk(True)                            # rebuild the desk on the new model now
            if err:
                return err
        self.say("info", f"AI desk now thinks with {PROVIDERS[provider]['label']} · {self.p.desk['model']}")
        return ""

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
                "mcap_sol": s.market_cap_sol, "buyers": len(s.buyers), "score": s.score,
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
                "source": pos.source, "desk": pos.desk, "p": pos.p, "manual": pos.manual, "bot": pos.bot,
                "adds": len(pos.adds or []), "ride_tp": pos.ride_tp, "fam": self.family(m),
                "entry_mcap_sol": self._mc_at(s, pos.entry_price), "mcap_sol": s.market_cap_sol if s.price_known else None, "last_trade_s": round(self.now - s.last_trade_ts) if s.last_trade_ts else None,
                "spark": [p for t, p, *_ in s.trades if t >= pos.opened_at - 30][-120:],
                "entry_idx": sum(1 for t, *_ in s.trades if pos.opened_at - 30 <= t < pos.opened_at),
            })
        return {
            "type": "snapshot", "now": self.now, "mode": self.mode, "paused": self.paused, "halted": self.book.halted,
            "xchain": self.xchain.view(),
            "kols": {"n": len(self.kols.get("kols") or {}), "fetched": self.kols.get("fetched")},
            "lab": {"running": (lambda x: x and {"id": x["id"], "key": x["key"], "now": x["now"], "value": x["value"], "by": x["by"],
                                                 "started": x["started"]})(self.xlab.running()),
                    "last": (lambda d: d and {"id": d[0]["id"], "key": d[0]["key"], "now": d[0]["now"], "value": d[0]["value"], "by": d[0]["by"],
                                              "status": d[0]["status"], "verdict": (d[0].get("result") or {}).get("verdict"),
                                              "finished": d[0]["finished"]})(self.xlab.view()["done"]),
                    "tries": self.xlab.tries(), "queued": len(self.xlab.queued())},
            "sol": self.book.sol, "equity": self.equity(), "day_pnl": self.book.day_pnl,
            "summary": self.summary(), "positions": positions, "watching": watching[:40],
            "manual": {**{k: v for k, v in self._manual_cfg().items()}, "queue": sorted(self.manual_queue)},
            "orders": sorted(self.orders.values(), key=lambda o: -o["created"])[:40], "away": self.away,
            "ledger": {"calls": len(self.ledger.calls()), "head": self.ledger.head[:12]},
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
            "pulse": [list(r) for r in self.pulse][-60:], "pulse_since": self.pulse_since, "deposits": self.book.deposits[-20:],
            "risk": self.risk_info(),
            "limits": {"daily_loss_sol": self.p.capital.daily_loss_limit_sol,
                       "kill_dd_pct": self.p.capital.max_drawdown_pct, "kill_base": self.book.kill_base,
                       "kill_at_sol": self.kill_at(), "start_sol": self.book.start_sol},
            "feed": {"realtime": self.feed.realtime, "host": getattr(self.feed, "host", ""),
                     "degraded": bool(getattr(self.feed, "degraded", False)),
                     "degraded_reason": getattr(self.feed, "degraded_reason", ""),
                     "gap_pct": getattr(self.feed, "gap_pct", None), "lag_s": getattr(self.feed, "lag_s", None),
                     **(self.feed.stream_stats() if hasattr(self.feed, "stream_stats") else {}),
                     "connected": getattr(self.feed, "ws", True) is not None,
                     "last_event_age_s": round(self.now - self.last_event, 1) if self.last_event else None},
        }

    def save_report(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"summary": self.summary(), "rejects": dict(self.rejects),
                                    "closed": self.book.closed, "params": dict(self.p)}, indent=1, default=str))
