"""Second-life momentum: a live forward test on established coins. Nothing is traded.

Found in the wallet study's recording of PumpSwap trades (pump.fun coins 2+ days old with $5k+ liquidity, Oct 4-6):
when such a coin jumps 40%+ in 5 minutes on 4x its usual volume, buying a minute later and holding 1-2 hours made money
on each of the three days, after 1.2% costs, and more with the fill 60 s late than 5 s late. Unlike the bonding curve,
speed doesn't decide it. But a few revivals carried it (+664% to +1318% on real volume): without each day's best trade
it was about flat. Three days can be luck, so this follows every signal from now on, on trades it never saw.

Reads the recorder's live file (data/wallets/trades-YYYY-MM-DD.jsonl, flushed every ~5 s). Each signal opens a follow
per fill delay; entries and exits fill at the first trade at least that long after the signal (or the exit's trigger),
costs COST round trip. Results: data/revival.jsonl, Analytics -> Exit lab -> Second-life momentum.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import deque
from pathlib import Path

RULES = {"+40% in 5 min, volume x4": (0.40, 4.0), "+30% in 5 min, volume x4": (0.30, 4.0),
         "+20% in 5 min, volume x4": (0.20, 4.0)}
DELAYS = (5, 60)                                   # seconds from the signal to the fill
EXITS = {"hold 1 h": (None, 3600), "hold 2 h": (None, 7200), "stop 30%, hold 1 h": (0.30, 3600)}
COST = 0.012                                       # round trip: 0.3% fee + ~0.3% price impact, each side
HISTORY_S = 3900                                   # a pool's last 65 min: 5-min move against the hour before
CHECK_S = 30                                       # a pool is checked at most this often
COOLDOWN_S = 7200                                  # one signal per pool and rule in this long
WARMUP_BYTES = 20_000_000                          # on start, the file's last ~20 MB rebuild the pools' recent history


class Revival:
    def __init__(self, data_dir: Path, now: float | None = None):
        self.src = Path(data_dir) / "wallets"
        self.out = Path(data_dir) / "revival.jsonl"
        self.started = now if now is not None else time.time()
        self.path: Path | None = None
        self.offset = 0
        self.rest = b""
        self.pools: dict[str, deque] = {}            # pool -> (t, px, sol) of its last HISTORY_S
        self.meta: dict[str, tuple] = {}             # pool -> (symbol, mint)
        self.checked: dict[str, float] = {}
        self.fired: dict[tuple, float] = {}          # (pool, rule) -> last signal time
        self.open: dict[str, list[dict]] = {}       # pool -> its follows still running
        self.done: list[dict] = []
        self.newest_t = 0.0
        try:
            self.done = [json.loads(x) for x in self.out.read_text().splitlines() if x.strip()]
        except (OSError, ValueError):
            self.done = []

    # ------------------------------------------------------------------ reading
    def _file_for(self, now: float) -> Path:
        return self.src / f"trades-{time.strftime('%Y-%m-%d', time.gmtime(now))}.jsonl"

    def tick(self, now: float | None = None, max_bytes: int = 8_000_000) -> int:
        """Read what the recorder wrote since the last call; returns the rows processed."""
        now = now if now is not None else time.time()
        want = self._file_for(now)
        n = 0
        if self.path is None:
            self.path = want
            try:
                size = want.stat().st_size
            except OSError:
                return 0
            self.offset = max(0, size - WARMUP_BYTES)
            self.rest = b""
            if self.offset:
                with want.open("rb") as f:          # start on a whole line
                    f.seek(self.offset)
                    self.offset += len(f.readline())
        if want != self.path:                        # a new UTC day: finish yesterday's file, then switch
            n += self._read(max_bytes)
            self.path, self.offset, self.rest = want, 0, b""
        n += self._read(max_bytes)
        self._expire(now)
        return n

    def _read(self, max_bytes: int) -> int:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read(max_bytes)
        except OSError:
            return 0
        if not chunk:
            return 0
        self.offset += len(chunk)
        data = self.rest + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self.rest = data
            return 0
        self.rest = data[cut + 1:]
        n = 0
        for line in data[:cut].split(b"\n"):
            try:
                r = json.loads(line)
                t, px, sol = float(r["t"]), float(r["px"]), float(r["sol"])
            except (ValueError, KeyError, TypeError):
                continue
            if px > 0 and sol > 0:
                self.on_trade(r["pool"], t, px, sol, r.get("sym"), r.get("mint"))
                n += 1
        return n

    # ------------------------------------------------------------------ the rule and the follows
    def on_trade(self, pool: str, t: float, px: float, sol: float, sym=None, mint=None) -> None:
        self.newest_t = max(self.newest_t, t)
        q = self.pools.get(pool)
        if q is None:
            q = self.pools[pool] = deque()
            self.meta[pool] = (sym, mint)
        q.append((t, px, sol))
        while q and q[0][0] < t - HISTORY_S - 60:
            q.popleft()
        fs = self.open.get(pool)
        if fs:
            for f in fs:
                self._follow(f, t, px)
            fs[:] = [f for f in fs if not f.get("finished")]
            if not fs:
                del self.open[pool]
        if t - self.checked.get(pool, -1e18) < CHECK_S or t < self.started or not q or t - q[0][0] < HISTORY_S - 60:
            return
        self.checked[pool] = t
        ret5, surge = self._signal(q, t, px)
        if ret5 is None:
            return
        for name, (need, vol) in RULES.items():
            if ret5 >= need and surge >= vol and t - self.fired.get((pool, name), -1e18) >= COOLDOWN_S:
                self.fired[(pool, name)] = t
                for d in DELAYS:
                    self.open.setdefault(pool, []).append({"pool": pool, "symbol": self.meta[pool][0], "mint": self.meta[pool][1], "rule": name,
                                      "delay": d, "signal_t": t, "signal_px": px, "ret5": round(ret5, 3),
                                      "surge": round(surge, 1), "exits": {}})

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

    def _follow(self, f: dict, t: float, px: float) -> None:
        if "p0" not in f:
            if t >= f["signal_t"] + f["delay"]:
                f["p0"], f["fill_t"] = px, t
            return
        for name, (stop, hold) in EXITS.items():
            x = f["exits"].setdefault(name, {})
            if "pnl" in x:
                continue
            if "sell_at" in x:
                if t >= x["sell_at"]:
                    x["pnl"] = round((px / f["p0"] - 1 - COST) * 100, 2)
                    self._close(f, name, x, t)
                continue
            why = "stop" if stop is not None and px <= f["p0"] * (1 - stop) else "time" if t - f["fill_t"] >= hold else ""
            if why:
                x["why"], x["sell_at"] = why, t + f["delay"]
        if len(f["exits"]) == len(EXITS) and all("pnl" in x for x in f["exits"].values()):
            f["finished"] = True

    def _close(self, f: dict, name: str, x: dict, t: float) -> None:
        row = {"closed": t, "signal_t": f["signal_t"], "pool": f["pool"], "mint": f["mint"], "symbol": f["symbol"],
               "rule": f["rule"], "delay": f["delay"], "exit": name, "why": x["why"], "pnl_pct": x["pnl"],
               "ret5": f["ret5"], "surge": f["surge"]}
        self.done.append(row)
        try:
            with self.out.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def _expire(self, now: float) -> None:
        """A pool that stopped trading: close what's due at its last price (a dead coin is a real result)."""
        for f in [f for fs in self.open.values() for f in fs]:
            q = self.pools.get(f["pool"])
            if not q or "p0" not in f or self.newest_t - q[-1][0] < 1800:
                continue
            for name, (stop, hold) in EXITS.items():
                x = f["exits"].setdefault(name, {})
                if "pnl" not in x and self.newest_t - f["fill_t"] >= hold:
                    x["why"] = x.get("why") or "no trades"
                    x["pnl"] = round((q[-1][1] / f["p0"] - 1 - COST) * 100, 2)
                    self._close(f, name, x, self.newest_t)
            if all("pnl" in x for x in f["exits"].values()) and len(f["exits"]) == len(EXITS):
                f["finished"] = True
        self.open = {p: [f for f in fs if not f.get("finished")] for p, fs in self.open.items()}
        self.open = {p: fs for p, fs in self.open.items() if fs}
        if len(self.pools) > 6000:                     # pools quiet for a while: their history is stale anyway
            cut = self.newest_t - HISTORY_S
            self.pools = {p: q for p, q in self.pools.items() if (q and q[-1][0] >= cut) or p in self.open}

    # ------------------------------------------------------------------ the view
    def view(self) -> dict:
        rows = self.done
        table = []
        for rule in RULES:
            for d in DELAYS:
                for ex in EXITS:
                    xs = sorted(r["pnl_pct"] for r in rows if r["rule"] == rule and r["delay"] == d and r["exit"] == ex)
                    if not xs:
                        continue
                    table.append({"rule": rule, "delay_s": d, "exit": ex, "n": len(xs), "mean_pct": statistics.fmean(xs),
                                  "median_pct": statistics.median(xs), "win_rate": sum(1 for x in xs if x > 0) / len(xs),
                                  "mean_without_best3": statistics.fmean(xs[:-3]) if len(xs) > 3 else None})
        signals = len({(r["pool"], r["rule"], r["signal_t"]) for r in rows})
        return {"since": min((r["signal_t"] for r in rows), default=None), "signals": signals,
                "open": len({(f["pool"], f["rule"], f["signal_t"]) for fs in self.open.values() for f in fs}),
                "lag_s": round(time.time() - self.newest_t) if self.newest_t else None, "table": table,
                "best": sorted(rows, key=lambda r: -r["pnl_pct"])[:5]}
