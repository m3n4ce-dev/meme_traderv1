"""Second-life momentum: an exploratory forward test on established coins. Nothing is traded.

Found in the wallet study's recording of PumpSwap trades (pump.fun coins 2+ days old with $5k+ liquidity, Oct 4-6):
when such a coin jumps 40%+ in 5 minutes on 4x its usual volume, buying a minute later and holding 1-2 hours made money
on each of the three days, after 1.2% costs, and more with the fill 60 s late than 5 s late. A few revivals carried it
(+664% to +1318% on real volume): without each day's best trade it was about flat. Three days can be luck.

EXPLORATORY: 3 rules x 2 fill delays x 3 exits are correlated looks at the same signals, not independent tests. A
confirmatory test freezes ONE rule, delay, exit and order size, then starts fresh (a second review, 2026-10-06).

How it follows, so that a result means something a bot could have done:
* Clocks. Trades carry their chain time; the evaluator also knows when it READ them (the recorder flushes every ~5 s,
  and after a restart it reads what it missed). A signal is decided when it was seen; the buy fills at the first trade
  at least `delay` after that decision; a stop is noticed when its trade is read and sells `delay` after; a hold sells at
  the first trade `delay` after its time is up. Nothing fills before it could have been decided.
* Costs per follow: 0.3% fee a side plus price impact for ORDER_SOL against the pool's SOL depth then (its last
  liquidity reading at or before the signal, from the recorder's pools.json); a flat 1.2% only when depth is unknown.
* No sale, no result. A pool that stops trading leaves its follow CENSORED (no executable sale was seen), counted
  apart with its last-trade mark, never booked as a P&L.
* A control. Each signal also follows a random other active, established pool at the same moment, on the same delays
  and exits: what "any coin, same time" made.
* Restarts. Open follows, cooldowns and the file position are saved (data/revival_state.json); on restart it resumes
  where it stopped. Every signal stays in the books: closed, censored, or open.

Results: data/revival.jsonl, Analytics -> Exit lab -> Second-life momentum.
"""
from __future__ import annotations

import json
import random
import sqlite3
import statistics
import time
from collections import deque
from pathlib import Path

from .replay_exec import is_censored, timer_due     # one execution and censoring rule set with the replays


class Store:
    """The forward test's results and checkpoint, in one SQLite file (a fourth review, 2026-10-07): each result is
    keyed (follow id, exit) and committed in the same transaction as the checkpoint it belongs to, so a result is
    durable exactly once, and a failed write raises (the caller stops) instead of being taken as done. The JSONL file
    is only an export."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS results (id TEXT NOT NULL, exit TEXT NOT NULL, row TEXT NOT NULL, "
                        "PRIMARY KEY (id, exit))")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL)")

    def results(self) -> list[dict]:
        return [json.loads(r) for (r,) in self.db.execute("SELECT row FROM results ORDER BY rowid")]

    def checkpoint(self) -> dict | None:
        r = self.db.execute("SELECT v FROM state WHERE k = 'checkpoint'").fetchone()
        return json.loads(r[0]) if r else None

    def commit(self, rows: list[dict], checkpoint: dict | None) -> list[dict]:
        """Write rows (new ones only) and the checkpoint atomically; returns the rows actually new. Raises on failure."""
        new = []
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                cur = self.db.execute("INSERT OR IGNORE INTO results (id, exit, row) VALUES (?, ?, ?)",
                                      (row["id"], row["exit"], json.dumps(row)))
                if cur.rowcount:
                    new.append(row)
            if checkpoint is not None:
                self.db.execute("INSERT OR REPLACE INTO state (k, v) VALUES ('checkpoint', ?)", (json.dumps(checkpoint),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return new

RULES = {"+40% in 5 min, volume x4": (0.40, 4.0), "+30% in 5 min, volume x4": (0.30, 4.0),
         "+20% in 5 min, volume x4": (0.20, 4.0)}
DELAYS = (5, 60)                                   # seconds from the decision to the fill
EXITS = {"hold 1 h": (None, 3600), "hold 2 h": (None, 7200), "stop 30%, hold 1 h": (0.30, 3600)}
FEE = 0.003                                        # PumpSwap: ~0.25% to the pool + protocol, each side
ORDER_SOL = 0.25                                   # the order size the impact is costed for
FLAT_COST = 0.012                                  # round trip, when the pool's depth isn't known
HISTORY_S = 3900                                   # a pool's last 65 min: 5-min move against the hour before
CHECK_S = 30                                       # a pool is checked at most this often
COOLDOWN_S = 7200                                  # one signal per pool and rule in this long
ACTIVE_S = 300                                     # a control pool traded within this long
WARMUP_BYTES = 20_000_000                          # history (no signals) read before the resume point


class Revival:
    def __init__(self, data_dir: Path, now: float | None = None, seed: int = 7):
        self.src = Path(data_dir) / "wallets"
        self.out = Path(data_dir) / "revival-v2.jsonl"           # v2: decision clocks, censoring, costs, control
        self.state_path = Path(data_dir) / "revival-v2-state.json"    # (before the store: read once, then ignored)
        self.store = Store(Path(data_dir) / "revival-v2.db")
        self.ckpt_offset = 0                           # the file position every processed row is before
        self.started = now if now is not None else time.time()
        self.path: Path | None = None
        self.offset = 0
        self.warm_from: int | None = None              # history-only bytes to read first (offset after a restart)
        self.rest = b""
        self.pools: dict[str, deque] = {}            # pool -> (t, px, sol) of its last HISTORY_S
        self.meta: dict[str, tuple] = {}             # pool -> (symbol, mint)
        self.checked: dict[str, float] = {}
        self.fired: dict[str, float] = {}            # "pool|rule" -> last signal time (chain)
        self.open: dict[str, list[dict]] = {}        # pool -> follows still running
        self.done: list[dict] = []
        self.newest_t = 0.0
        self.lags: deque = deque(maxlen=500)          # seconds between a trade's chain time and our reading it
        self.liq: dict = {}
        self.liq_at = 0.0
        self.sol_usd = 0.0
        self.rng = random.Random(seed)
        self._dirty = False
        self._saved_at = 0.0
        self.gaps: list = []                           # (from, to) wall times this evaluator wasn't reading
        self.last_wall = 0.0
        self.done = self.store.results()
        if not self.done:
            self._import_jsonl()
        self.done_keys = {(r.get("id"), r.get("exit")) for r in self.done if r.get("id")}
        try:
            st = self.store.checkpoint() or json.loads(self.state_path.read_text())
            self.path = Path(st["path"]) if st.get("path") else None
            self.offset = self.ckpt_offset = int(st.get("offset", 0))
            self.warm_from = max(0, self.offset - WARMUP_BYTES) if self.path else None
            self.fired = dict(st.get("fired") or {})
            self.open = {p: fs for p, fs in (st.get("open") or {}).items() if fs}
            if st.get("saved") and self.started - float(st["saved"]) > 120:
                self.gaps.append((float(st["saved"]), self.started))     # down: no sell could go out then
        except (OSError, ValueError, KeyError, TypeError):
            pass

    # ------------------------------------------------------------------ persistence
    def _import_jsonl(self) -> None:
        """The export from before the store: its whole rows go in, an unfinished last row goes to a quarantine file,
        and a broken row inside the file is counted (never a reason to drop the rest)."""
        try:
            lines = self.out.read_text().split("\n")
        except OSError:
            return
        rows, bad = [], 0
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                if i == len(lines) - 1:
                    with self.out.with_suffix(".quarantine.jsonl").open("a") as q:
                        q.write(line + "\n")
                else:
                    bad += 1
                continue
            if r.get("id") and r.get("exit"):
                rows.append(r)
        self.import_bad = bad
        if rows:
            self.store.commit(rows, None)
            self.done = self.store.results()

    def _state(self) -> dict:
        return {"path": str(self.path) if self.path else "", "offset": self.ckpt_offset,
                "fired": {k: v for k, v in self.fired.items() if self.newest_t - v < COOLDOWN_S},
                "open": self.open, "saved": time.time()}

    def save(self, force: bool = False) -> None:
        """The checkpoint, committed through the store (raises if it can't be written: the caller stops)."""
        if not (self._dirty or force) or (not force and time.time() - self._saved_at < 10):
            return
        self.store.commit([], self._state())
        self._dirty, self._saved_at = False, time.time()

    # ------------------------------------------------------------------ reading
    def _file_for(self, now: float) -> Path:
        return self.src / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(now))}.jsonl"

    def tick(self, now: float | None = None, max_bytes: int = 8_000_000) -> int:
        """Read what the recorder wrote since the last call (seen at wall time `now`); returns rows processed."""
        now = now if now is not None else time.time()
        if self.last_wall and now - self.last_wall > 120:
            self.gaps.append((self.last_wall, now))
            self.gaps = self.gaps[-20:]
        self.last_wall = now
        self._load_liq(now)
        want = self._file_for(now)
        n = 0
        if self.path is None:
            self.path = want
            try:
                size = want.stat().st_size
            except OSError:
                return 0
            self.offset, self.warm_from = size, max(0, size - WARMUP_BYTES)
            self.ckpt_offset = size
        if self.warm_from is not None:                 # history before the resume point: pools only
            self._warm(max_bytes)
        if want != self.path:                          # a new UTC day: finish yesterday's file first
            for _ in range(8):
                got = self._read(max_bytes, now)
                n += got
                if self._at_end():
                    break
            if not self._at_end():                     # more backlog than a tick reads: keep at it next tick
                self._expire(now)
                self.save()
                return n
            self.path, self.offset, self.rest, self.ckpt_offset = want, 0, b"", 0
            self._dirty = True
        n += self._read(max_bytes, now)
        self._expire(now)
        self.save()
        return n

    def _at_end(self) -> bool:
        try:
            return self.offset >= self.path.stat().st_size
        except OSError:
            return True                                # gone (compressed): nothing more to read from it

    def _warm(self, max_bytes: int) -> None:
        """History before the resume point, a chunk a tick (pools only: no signals, no follows)."""
        try:
            with self.path.open("rb") as f:
                f.seek(self.warm_from)
                chunk = f.read(min(max_bytes, self.offset - self.warm_from))
        except OSError:
            self.warm_from = None
            return
        if not chunk:
            self.warm_from = None
            return
        start, end = 0, chunk.rfind(b"\n")
        if self.warm_from and not getattr(self, "_warm_aligned", False):
            start = chunk.find(b"\n") + 1             # start on a whole line
            self._warm_aligned = True
        if end < start:
            self.warm_from = None
            return
        self.warm_from += end + 1
        if self.warm_from >= self.offset:
            self.warm_from = None
        for line in chunk[start:end].split(b"\n"):
            r = self._row(line)
            if r:
                self._remember(*r)

    def _read(self, max_bytes: int, seen: float) -> int:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read(max_bytes)
        except OSError:
            return 0
        if not chunk:
            return 0
        self.ckpt_offset = self.offset - len(self.rest)  # a crash mid-chunk re-reads it (results are keyed: once)
        self.offset += len(chunk)
        self._dirty = True
        data = self.rest + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self.rest = data
            return 0
        self.rest = data[cut + 1:]
        n = 0
        for line in data[:cut].split(b"\n"):
            r = self._row(line)
            if r:
                self.on_trade(*r, seen=seen)
                n += 1
        self.ckpt_offset = self.offset - len(self.rest)
        return n

    @staticmethod
    def _row(line: bytes):
        try:
            r = json.loads(line)
            t, px, sol = float(r["t"]), float(r["px"]), float(r["sol"])
        except (ValueError, KeyError, TypeError):
            return None
        return (r["pool"], t, px, sol, r.get("sym"), r.get("mint")) if px > 0 and sol > 0 else None

    def _load_liq(self, now: float) -> None:
        if now - self.liq_at < 600:
            return
        self.liq_at = now
        try:
            pools = json.loads((self.src / "pools.json").read_text())
            self.liq = {p: r.get("liq") or [] for p, r in pools.items()}
            self.sol_usd = float(json.loads((self.src / "status.json").read_text()).get("sol_usd") or 0)
        except (OSError, ValueError, AttributeError):
            pass

    def _cost(self, pool: str, t: float) -> tuple[float, str]:
        """Round-trip cost of an ORDER_SOL order on `pool` at chain time `t`: fees + impact in and out."""
        snaps = [usd for ts, usd in self.liq.get(pool, []) if ts <= t]
        if not snaps or not self.sol_usd or snaps[-1] <= 0:
            return FLAT_COST, "flat (depth unknown)"
        sol_depth = snaps[-1] / 2 / self.sol_usd       # the pool's SOL side
        impact = ORDER_SOL / max(sol_depth, 1e-9)
        return 2 * FEE + 2 * impact, f"fees + impact on {sol_depth:.0f} SOL depth"

    # ------------------------------------------------------------------ the rule and the follows
    def _remember(self, pool: str, t: float, px: float, sol: float, sym=None, mint=None) -> deque:
        self.newest_t = max(self.newest_t, t)
        q = self.pools.get(pool)
        if q is None:
            q = self.pools[pool] = deque()
            self.meta[pool] = (sym, mint)
        q.append((t, px, sol))
        while q and q[0][0] < t - HISTORY_S - 60:
            q.popleft()
        return q

    def on_trade(self, pool: str, t: float, px: float, sol: float, sym=None, mint=None, seen: float | None = None) -> None:
        seen = t if seen is None else max(seen, t)
        self.lags.append(seen - t)
        q = self._remember(pool, t, px, sol, sym, mint)
        fs = self.open.get(pool)
        if fs:
            for f in fs:
                self._follow(f, t, px, seen)
            fs[:] = [f for f in fs if not f.get("finished")]
            if not fs:
                del self.open[pool]
            self._dirty = True
        if t - self.checked.get(pool, -1e18) < CHECK_S or seen < self.started or not q or t - q[0][0] < HISTORY_S - 60:
            return
        self.checked[pool] = t
        ret5, surge = self._signal(q, t, px)
        if ret5 is None:
            return
        for name, (need, vol) in RULES.items():
            key = f"{pool}|{name}"
            if ret5 >= need and surge >= vol and t - self.fired.get(key, -1e18) >= COOLDOWN_S:
                self.fired[key] = t
                self._open(pool, name, t, seen, ret5, surge, control=False)
                twin = self._control_pool(pool, t)
                if twin:
                    self._open(twin, name, t, seen, None, None, control=True)
                self._dirty = True

    def _open(self, pool: str, rule: str, t: float, seen: float, ret5, surge, control: bool) -> None:
        cost, how = self._cost(pool, t)
        sym, mint = self.meta.get(pool, (None, None))
        for d in DELAYS:
            self.open.setdefault(pool, []).append({
                "id": f"{pool}|{rule}|{t:.0f}|{d}|{'c' if control else 's'}", "pool": pool, "symbol": sym, "mint": mint, "rule": rule, "delay": d, "control": control,
                "signal_t": t, "decided_at": seen, "lag_s": round(seen - t, 1), "ret5": ret5 and round(ret5, 3),
                "surge": surge and round(surge, 1), "cost": round(cost, 4), "cost_how": how, "exits": {}})

    def _control_pool(self, pool: str, t: float) -> str:
        """A random other pool trading now with an hour of history: the same moment, any established coin."""
        live = [p for p, q in self.pools.items() if p != pool and q and t - q[-1][0] <= ACTIVE_S
                and t - q[0][0] >= HISTORY_S - 60]
        return self.rng.choice(sorted(live)) if live else ""

    @staticmethod
    def _signal(q: deque, t: float, px: float) -> tuple:
        px5, v5, v60 = None, 0.0, 0.0
        for (tt, p, sol) in q:
            if tt <= t - 300:
                px5 = p
                if tt > t - 3900:
                    v60 += sol
            else:
                v5 += sol
        if not px5 or v60 <= 0:
            return None, 0.0
        return px / px5 - 1, v5 / (v60 / 12)

    def _follow(self, f: dict, t: float, px: float, seen: float) -> None:
        if "p0" not in f:
            if t >= f["decided_at"] + f["delay"]:      # the first trade after the decision plus the delay
                f["p0"], f["fill_t"] = px, t
            return
        if t < f["fill_t"]:                            # a chunk re-read after a crash: trades from before its fill
            return
        for name, (stop, hold) in EXITS.items():
            x = f["exits"].setdefault(name, {})
            if "pnl" in x or x.get("censored"):
                continue
            if "sell_at" in x:
                if t >= x["sell_at"]:
                    x["pnl"] = round((px / f["p0"] - 1 - f["cost"]) * 100, 2)
                    self._close(f, name, x, t)
                continue
            if t >= f["fill_t"] + hold:                  # the timer came due first: its sale goes, whatever this price
                due = timer_due(f["fill_t"], hold, self.gaps)   # due while this wasn't running: sells when it's back
                x["why"], x["sell_at"] = "time", due + f["delay"]
                if t >= x["sell_at"]:                  # the timer was set when it filled: this trade is the first after
                    x["pnl"] = round((px / f["p0"] - 1 - f["cost"]) * 100, 2)
                    self._close(f, name, x, t)
            elif stop is not None and px <= f["p0"] * (1 - stop):
                x["why"], x["sell_at"] = "stop", seen + f["delay"]           # noticed when read
        if len(f["exits"]) == len(EXITS) and all("pnl" in x or x.get("censored") for x in f["exits"].values()):
            f["finished"] = True

    def _close(self, f: dict, name: str, x: dict, t: float) -> None:
        if (f.get("id"), name) in self.done_keys:       # already committed (a restart replayed it): once only
            return
        row = {"id": f.get("id") or f"{f['pool']}|{f['rule']}|{f['signal_t']:.0f}|{f['delay']}|{'c' if f.get('control') else 's'}",
               "closed": t, "signal_t": f["signal_t"], "decided_at": f.get("decided_at"), "lag_s": f.get("lag_s"),
               "pool": f["pool"], "mint": f["mint"], "symbol": f["symbol"], "rule": f["rule"], "delay": f["delay"],
               "control": f.get("control", False), "exit": name, "why": x.get("why", ""), "pnl_pct": x.get("pnl"),
               "censored": bool(x.get("censored")), "mark_pct": x.get("mark"), "cost": f.get("cost"),
               "cost_how": f.get("cost_how"), "ret5": f.get("ret5"), "surge": f.get("surge")}
        new = self.store.commit([row], self._state())   # raises if it can't be written: nothing is taken as done
        self.done_keys.add((row["id"], name))
        if new:
            self.done.append(row)
            try:                                       # the export (the store is the record)
                with self.out.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
            except OSError:
                pass

    def _expire(self, now: float) -> None:
        """A follow whose pool went quiet: no trade means no sale was possible. Censored, with its last-trade mark
        kept apart; never booked as a P&L (and not as a zero either)."""
        for fs in self.open.values():
            for f in fs:
                q = self.pools.get(f["pool"])
                last = q[-1][0] if q else f["signal_t"]
                due = (f.get("fill_t") or f["decided_at"] + f["delay"])
                for name, (stop, hold) in EXITS.items():
                    x = f["exits"].setdefault(name, {})
                    if "pnl" in x or x.get("censored"):
                        continue
                    if is_censored(self.newest_t, last, due + hold):
                        x["censored"], x["why"] = True, "no trades: no executable sale"
                        if "p0" in f and q:
                            x["mark"] = round((q[-1][1] / f["p0"] - 1) * 100, 2)
                        self._close(f, name, x, self.newest_t)
                        self._dirty = True
                if len(f["exits"]) == len(EXITS) and all("pnl" in x or x.get("censored") for x in f["exits"].values()):
                    f["finished"] = True
        self.open = {p: [f for f in fs if not f.get("finished")] for p, fs in self.open.items()}
        self.open = {p: fs for p, fs in self.open.items() if fs}
        if len(self.pools) > 6000:                     # pools quiet for a while: their history is stale anyway
            cut = self.newest_t - HISTORY_S
            self.pools = {p: q for p, q in self.pools.items() if (q and q[-1][0] >= cut) or p in self.open}

    # ------------------------------------------------------------------ the view
    @staticmethod
    def _range90(rows: list[dict], boots: int = 1000, seed: int = 7) -> tuple:
        """90% range of the mean, resampled by UTC day and then by follow within each day."""
        days: dict[str, list] = {}
        for r in rows:
            days.setdefault(time.strftime("%Y-%m-%d", time.gmtime(r["signal_t"])), []).append(r["pnl_pct"])
        groups = list(days.values())
        if len(groups) < 2:
            return None, None, len(groups)
        rng = random.Random(seed)
        means = sorted(statistics.fmean([x for g in rng.choices(groups, k=len(groups)) for x in rng.choices(g, k=len(g))])
                       for _ in range(boots))
        return means[int(0.05 * boots)], means[int(0.95 * boots) - 1], len(groups)

    def view(self) -> dict:
        table = []
        for control in (False, True):
            for rule in RULES:
                for d in DELAYS:
                    for ex in EXITS:
                        rows = [r for r in self.done if r["rule"] == rule and r["delay"] == d and r["exit"] == ex
                                and bool(r.get("control")) == control]
                        done = [r for r in rows if not r.get("censored") and r.get("pnl_pct") is not None]
                        if not rows:
                            continue
                        xs = sorted(r["pnl_pct"] for r in done)
                        lo, hi, ndays = self._range90(done) if done else (None, None, 0)
                        table.append({"rule": rule, "delay_s": d, "exit": ex, "control": control, "n": len(xs),
                                      "censored": len(rows) - len(done), "days": ndays,
                                      "mean_pct": statistics.fmean(xs) if xs else None,
                                      "median_pct": statistics.median(xs) if xs else None,
                                      "win_rate": sum(1 for x in xs if x > 0) / len(xs) if xs else None,
                                      "mean_without_best3": statistics.fmean(xs[:-3]) if len(xs) > 3 else None,
                                      "lo_pct": lo, "hi_pct": hi})
        mine = [r for r in self.done if not r.get("control")]
        signals = len({(r["pool"], r["rule"], r["signal_t"]) for r in mine})
        lags = sorted(self.lags)
        return {"status": "exploratory", "variants": len(RULES) * len(DELAYS) * len(EXITS),
                "order_sol": ORDER_SOL, "since": min((r["signal_t"] for r in mine), default=None), "signals": signals,
                "open": len({(f["pool"], f["rule"], f["signal_t"]) for fs in self.open.values() for f in fs
                             if not f.get("control")}),
                "lag_s": round(time.time() - self.newest_t) if self.newest_t else None,
                "read_lag_p50_s": round(lags[len(lags) // 2], 1) if lags else None, "table": table,
                "best": sorted((r for r in mine if r.get("pnl_pct") is not None), key=lambda r: -r["pnl_pct"])[:5]}
