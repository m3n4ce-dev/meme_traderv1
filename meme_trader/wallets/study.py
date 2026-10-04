"""The wallet study: does following repeat-early wallets into 14+ day-old coins beat buying a random coin?

    register  fix the rules (research/policies/<name>.yaml) before looking at any data; later edits are refused
    select    period A so far: the moves found and the wallets that currently qualify (read-only)
    freeze    after `selection.period_days` of data: lock the wallet list; everything after is period B
    eval      period B: signals, the article's gates, simulated trades, random-coin and random-wallet baselines,
              and the pass/fail verdict fixed in the policy

No hindsight crosses the freeze: the list uses only period A, signals and trades only period B.
"""
from __future__ import annotations

import bisect
import collections
import gzip
import hashlib
import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import ROOT
from .recorder import DATA

POLICY_DIR = ROOT / "research" / "policies"
RESEARCH = ROOT / "data" / "research"
RULE_KEYS = ("selection", "signal", "gates", "trade", "pass")
DAY = 86400.0


class StudyError(Exception):
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------- policy, registration, lock
def load_policy(name: str, policy_dir: Path = POLICY_DIR) -> dict:
    path = policy_dir / f"{name}.yaml"
    if not path.exists():
        raise StudyError(f"no policy file {path}")
    pol = yaml.safe_load(path.read_text())
    missing = [k for k in RULE_KEYS if k not in pol]
    if missing:
        raise StudyError(f"{name}: missing sections {missing}")
    pol["_dir"] = str(policy_dir)
    return pol


def signature(pol: dict) -> str:
    return hashlib.sha256(json.dumps({k: pol[k] for k in RULE_KEYS}, sort_keys=True).encode()).hexdigest()[:12]


def lock_path(pol: dict) -> Path:
    return Path(pol["_dir"]) / f"{pol['name']}.lock.json"


def read_lock(pol: dict) -> dict | None:
    p = lock_path(pol)
    return json.loads(p.read_text()) if p.exists() else None


def _write_lock(pol: dict, lock: dict) -> None:
    lock_path(pol).write_text(json.dumps(lock, indent=1) + "\n")


def register(pol: dict, now: float | None = None) -> dict:
    sig = signature(pol)
    lock = read_lock(pol)
    if lock:
        if lock["rules_signature"] != sig:
            raise StudyError(f"{pol['name']}'s rules changed since registration ({lock['rules_signature']} -> {sig}). "
                             f"Copy the file to a new version instead.")
        return lock
    lock = {"policy": pol["name"], "rules_signature": sig, "registered_at": _iso(now or time.time())}
    _write_lock(pol, lock)
    return lock


def checked_lock(pol: dict) -> dict:
    lock = read_lock(pol)
    if not lock:
        raise StudyError(f"{pol['name']} isn't registered: run `register {pol['name']}` first")
    if lock["rules_signature"] != signature(pol):
        raise StudyError(f"{pol['name']}'s rules changed since registration; results would not be the registered test")
    return lock


# ---------------------------------------------------------------------- data
class Data:
    """Recorded trades grouped by pool as (t, wallet, is_buy, sol, px) tuples sorted by time, plus per-wallet
    activity counts (all sizes) for bot exclusion."""

    def __init__(self):
        self.by_pool: dict[str, list[tuple]] = collections.defaultdict(list)
        self.mint: dict[str, str] = {}
        self.sym: dict[str, str] = {}
        self.n_trades: collections.Counter = collections.Counter()
        self.coins: dict[str, set] = collections.defaultdict(set)
        self.first_t = float("inf")
        self.last_t = 0.0
        self.usd_per_sol: list[float] = []

    def add(self, r: dict, min_sol: float, coin_cap: int) -> None:
        w = sys.intern(r["wallet"])
        self.n_trades[w] += 1
        cs = self.coins[w]
        if len(cs) <= coin_cap:
            cs.add(r["pool"])
        t = r["t"]
        self.first_t, self.last_t = min(self.first_t, t), max(self.last_t, t)
        if r["sol"] >= 1 and r.get("usd") and len(self.usd_per_sol) < 5000:
            self.usd_per_sol.append(r["usd"] / r["sol"])
        if r["sol"] < min_sol or r["px"] <= 0:
            return
        key = f"{r.get('sig') or r['id']}|{r['side']}|{r['tokens']:.2f}"      # same trade from GeckoTerminal or the stream
        self.by_pool[r["pool"]].append((t, w, r["side"] == "buy", r["sol"], r["px"], key))
        self.mint.setdefault(r["pool"], r["mint"])
        if r.get("sym"):
            self.sym.setdefault(r["pool"], r["sym"])

    def finish(self) -> "Data":
        for pool, rs in self.by_pool.items():
            rs.sort(key=lambda x: (x[0], x[5]))
            out, last = [], None
            for x in rs:
                if x[5] != last:
                    out.append(x[:5])
                last = x[5]
            self.by_pool[pool] = out
        return self

    @property
    def sol_usd(self) -> float:
        return statistics.median(self.usd_per_sol) if self.usd_per_sol else 150.0

    def days(self) -> float:
        return max(0.0, self.last_t - self.first_t) / DAY if self.last_t else 0.0


def trade_files(data_dir: Path = DATA) -> list[Path]:
    return sorted(Path(data_dir).glob("trades-*.jsonl*"))


def load_data(files: list[Path], start: float | None = None, end: float | None = None, min_sol: float = 0.0,
              coin_cap: int = 400) -> Data:
    d = Data()
    for f in files:
        opener = gzip.open if f.suffix == ".gz" else open
        with opener(f, "rt") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                          # a line cut off by a crash
                if (start is not None and r["t"] < start) or (end is not None and r["t"] >= end):
                    continue
                d.add(r, min_sol, coin_cap)
    return d.finish()


def load_pools(data_dir: Path = DATA) -> dict:
    p = Path(data_dir) / "pools.json"
    return json.loads(p.read_text()) if p.exists() else {}


def liq_at(rec: dict, t: float) -> float | None:
    snaps = [s for s in rec.get("liq") or [] if s[0] <= t]
    return snaps[-1][1] if snaps else None


def created(rec: dict) -> float | None:
    return (rec.get("meta") or {}).get("created")


# ---------------------------------------------------------------------- period A: moves and wallets
def find_moves(d: Data, sel: dict) -> list[dict]:
    """Price reaches runner_x times a low within move_window_h, with min_move_buyers distinct buyers between."""
    win = sel["move_window_h"] * 3600
    moves = []
    for pool, rs in d.by_pool.items():
        lows: collections.deque = collections.deque()      # (t, px, i), increasing px: rolling minimum
        cool = -1.0
        for i, (t, _w, _b, _sol, px) in enumerate(rs):
            while lows and lows[-1][1] >= px:
                lows.pop()
            lows.append((t, px, i))
            while lows[0][0] < t - win:
                lows.popleft()
            if t < cool:
                continue
            lo_t, lo, lo_i = lows[0]
            if px < sel["runner_x"] * lo:
                continue
            seg = rs[lo_i:i + 1]
            if len({x[1] for x in seg if x[2]}) < sel["min_move_buyers"]:
                continue
            cross = next(x[0] for x in seg if x[4] >= sel["early_max_x"] * lo)
            moves.append({"pool": pool, "mint": d.mint.get(pool), "sym": d.sym.get(pool), "t_low": lo_t, "low": lo,
                          "t_cross": cross, "t_hit": t, "x": px / lo})
            cool = t + win
            lows.clear()
    return sorted(moves, key=lambda m: m["t_low"])


def select_wallets(d: Data, sel: dict) -> dict:
    moves = find_moves(d, sel)
    hits: dict[str, set] = collections.defaultdict(set)
    early_sol: collections.Counter = collections.Counter()
    for m in moves:
        rs = d.by_pool[m["pool"]]
        i = bisect.bisect_left(rs, (m["t_low"] - sel["early_pre_s"],))
        for t, w, buy, sol, px in rs[i:]:
            if t > m["t_cross"]:
                break
            if buy and sol >= sel["min_buy_sol"] and px <= sel["early_max_x"] * m["low"]:
                hits[w].add(m["pool"])
                early_sol[w] += sol
    good: collections.Counter = collections.Counter()
    for pool, rs in d.by_pool.items():
        cost: collections.Counter = collections.Counter()
        qty: collections.Counter = collections.Counter()
        done: set = set()
        for t, w, buy, sol, px in rs:
            if buy:
                cost[w] += sol
                qty[w] += sol / px
            elif w in qty and w not in done and qty[w] and px >= sel["good_exit_x"] * cost[w] / qty[w]:
                good[w] += 1
                done.add(w)
    bots = {w for w, n in d.n_trades.items() if n > sel["max_trades"] or len(d.coins[w]) > sel["max_coins"]}
    rows = []
    for w, coins in hits.items():
        if len(coins) < sel["min_hits"] or w in bots or w in d.by_pool:
            continue
        if sel["require_good_exit"] and not good[w]:
            continue
        rows.append({"wallet": w, "hits": len(coins), "good_exits": good[w], "early_sol": round(early_sol[w], 3),
                     "coins": sorted(d.sym.get(p) or p[:6] for p in coins)})
    rows.sort(key=lambda r: (-r["hits"], -r["good_exits"], -r["early_sol"], r["wallet"]))
    # control group for the random-wallet baseline: active, not bots, bought with size at least twice
    sized: collections.Counter = collections.Counter()
    for rs in d.by_pool.values():
        for _t, w, buy, sol, _px in rs:
            if buy and sol >= sel["min_buy_sol"]:
                sized[w] += 1
    control = sorted(w for w, n in sized.items() if n >= 2 and w not in bots)
    return {"moves": len(moves), "move_list": moves, "qualified": len(rows), "wallets": rows[: sel["max_wallets"]],
            "near_misses": sum(1 for w, c in hits.items() if len(c) == sel["min_hits"] - 1 and w not in bots),
            "bots_excluded": len(bots), "control": control, "days": d.days(), "first_t": d.first_t, "last_t": d.last_t}


# ---------------------------------------------------------------------- period B: signals, gates, trades
def candidate_buys(d: Data, pools: dict, sig: dict, start: float) -> list[tuple]:
    """(t, pool, wallet) for every sized buy in period B on a coin old enough to signal."""
    out = []
    for pool, rs in d.by_pool.items():
        c = created(pools.get(pool) or {})
        if not c:
            continue
        for t, w, buy, sol, _px in rs:
            if buy and t >= start and sol >= sig["min_buy_sol"] and t - c >= sig["min_coin_age_days"] * DAY:
                out.append((t, pool, w))
    out.sort()
    return out


def find_signals(buys: list[tuple], wallets: set, sig: dict) -> list[dict]:
    win, cool_s = sig["window_h"] * 3600, sig["cooldown_days"] * DAY
    recent: dict[str, collections.deque] = collections.defaultdict(collections.deque)
    cool: dict[str, float] = {}
    out = []
    for t, pool, w in buys:
        if w not in wallets:
            continue
        q = recent[pool]
        q.append((t, w))
        while q[0][0] < t - win:
            q.popleft()
        ws = {x[1] for x in q}
        if len(ws) >= sig["min_wallets"] and t >= cool.get(pool, 0):
            out.append({"pool": pool, "t": t, "wallets": sorted(ws)})
            cool[pool] = t + cool_s
    return out


def survived(candles: list | None, t: float, pct: float) -> bool | None:
    """Fell >= pct from its high on some complete day before t: a day's low under an earlier day's high, or a
    day's close under the high so far (a candle doesn't say whether its own low came before its high)."""
    if not candles:
        return None
    hi, k = 0.0, 1 - pct / 100
    for ts, _o, h, low, c, *_ in candles:
        if ts + DAY > t:
            break
        if hi and low <= hi * k:
            return True
        hi = max(hi, h)
        if c <= hi * k:
            return True
    return False


def gates(d: Data, pools: dict, candles: dict, g: dict, pool: str, t: float) -> dict:
    rec = pools.get(pool) or {}
    liq = liq_at(rec, t)
    rs = d.by_pool.get(pool, [])
    i, j = bisect.bisect_left(rs, (t - DAY,)), bisect.bisect_left(rs, (t,))
    buyers = {x[1] for x in rs[i:j] if x[2]}
    sellers = {x[1] for x in rs[i:j] if not x[2]}
    out = {"survived": survived(candles.get(pool), t, g["survived_dump_pct"]),
           "liquidity": None if liq is None else liq >= g["min_liquidity_usd"],
           "holders": (len(buyers) > len(sellers)) if g["holders_growing"] else True}
    out["all"] = all(v is True for v in out.values())
    return out


def simulate(rs: list[tuple], t_sig: float, tr: dict, reserve_sol: float | None, data_end: float) -> dict | None:
    """One trade on a pool's price path: enter at the first trade >= entry_delay_s after the signal; half off at
    take_profit_x, then trail; stop at stop_x; time stop. Net of fees, price impact and transaction costs."""
    i = bisect.bisect_left(rs, (t_sig + tr["entry_delay_s"],))
    if i >= len(rs):
        return None
    t0, px0 = rs[i][0], rs[i][4]
    end = t0 + tr["max_hold_days"] * DAY
    got, left, peak, tp, reason, last_px, ntx = 0.0, 1.0, px0, False, "time", px0, 2
    for t, _w, _b, _sol, px in rs[i + 1:]:
        if t > end:
            break
        last_px = px
        if not tp and px >= tr["take_profit_x"] * px0:
            got += 0.5 * tr["take_profit_x"]
            left, tp, peak, ntx = 0.5, True, px, 3
        if px <= tr["stop_x"] * px0:
            got += left * px / px0
            left, reason = 0.0, "stop"
            break
        if tp:
            peak = max(peak, px)
            if px <= peak * (1 - tr["trail_pct"] / 100):
                got += left * px / px0
                left, reason = 0.0, "trail"
                break
    if left:
        got += left * last_px / px0
        if data_end < end:
            reason = "open"
    size = tr["size_sol"]
    imp_in = size / reserve_sol if reserve_sol else 0.0
    imp_out = size * got / reserve_sol if reserve_sol else 0.0
    fee = tr["fee_pct"] / 100
    net = got * (1 - fee) * (1 - imp_out) / ((1 + fee) * (1 + imp_in)) - 1 - ntx * tr["tx_cost_sol"] / size
    return {"t_entry": t0, "px": px0, "gross_x": round(got, 4), "net": round(net, 4), "exit": reason}


def run_arm(d: Data, pools: dict, candles: dict, pol: dict, signals: list[dict], data_end: float, sol_usd: float) -> list[dict]:
    out = []
    for s in signals:
        g = gates(d, pools, candles, pol["gates"], s["pool"], s["t"])
        if not g["all"]:
            continue
        liq = liq_at(pools.get(s["pool"]) or {}, s["t"])
        res = simulate(d.by_pool[s["pool"]], s["t"], pol["trade"], liq / 2 / sol_usd if liq else None, data_end)
        if res:
            out.append({**s, **res, "sym": d.sym.get(s["pool"])})
    return out


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def day_bootstrap(trades: list[dict], sims: int = 5000, seed: int = 7) -> float | None:
    """P(mean net return > 0), resampling whole signal days."""
    by: dict[str, list[float]] = collections.defaultdict(list)
    for x in trades:
        by[_iso(x["t"])[:10]].append(x["net"])
    days = list(by.values())
    if not days:
        return None
    rng = random.Random(seed)
    pos = 0
    for _ in range(sims):
        pick = [r for _ in days for r in rng.choice(days)]
        pos += sum(pick) > 0
    return pos / sims


def random_coin_means(d: Data, pools: dict, candles: dict, pol: dict, trades: list[dict], data_end: float,
                      sol_usd: float, sims: int = 300, seed: int = 11) -> list[float]:
    """For each real trade, a random other coin that passed the same gates at the same moment."""
    sig = pol["signal"]
    cands = []
    for x in trades:
        t, opts = x["t"], []
        for pool, rs in d.by_pool.items():
            c = created(pools.get(pool) or {})
            if pool == x["pool"] or not c or t - c < sig["min_coin_age_days"] * DAY:
                continue
            i = bisect.bisect_left(rs, (t - 3600,))
            if i >= len(rs) or rs[i][0] > t + 3600:
                continue
            if gates(d, pools, candles, pol["gates"], pool, t)["all"]:
                opts.append(pool)
        cands.append((t, opts))
    rng = random.Random(seed)
    means, cache = [], {}
    for _ in range(sims):
        nets = []
        for t, opts in cands:
            if not opts:
                continue
            pool = rng.choice(opts)
            if (pool, t) not in cache:
                liq = liq_at(pools.get(pool) or {}, t)
                cache[(pool, t)] = simulate(d.by_pool[pool], t, pol["trade"], liq / 2 / sol_usd if liq else None, data_end)
            if cache[(pool, t)]:
                nets.append(cache[(pool, t)]["net"])
        if nets:
            means.append(sum(nets) / len(nets))
    return means


def random_wallet_means(d: Data, pools: dict, candles: dict, pol: dict, buys: list[tuple], control: list[str],
                        n: int, data_end: float, sol_usd: float, sims: int = 200, seed: int = 13) -> list[float]:
    rng = random.Random(seed)
    means = []
    for _ in range(sims):
        ws = set(rng.sample(control, min(n, len(control))))
        tr = run_arm(d, pools, candles, pol, find_signals(buys, ws, pol["signal"]), data_end, sol_usd)
        if tr:
            means.append(sum(x["net"] for x in tr) / len(tr))
    return means


def _pct_beaten(x: float | None, dist: list[float]) -> float | None:
    if x is None or not dist:
        return None
    return 100.0 * sum(1 for v in dist if x > v) / len(dist)


def evaluate(pol: dict, lock: dict, d: Data, pools: dict, candles: dict, control: list[str], quick: bool = False) -> dict:
    start = _ts(lock["frozen_at"])
    listed = {w["wallet"] for w in lock["wallets"]}
    buys = candidate_buys(d, pools, pol["signal"], start)
    signals = find_signals(buys, listed, pol["signal"])
    sol_usd = d.sol_usd
    data_end = d.last_t
    trades = run_arm(d, pools, candles, pol, signals, data_end, sol_usd)
    ungated = []
    for s in signals:
        liq = liq_at(pools.get(s["pool"]) or {}, s["t"])
        r = simulate(d.by_pool[s["pool"]], s["t"], pol["trade"], liq / 2 / sol_usd if liq else None, data_end)
        if r:
            ungated.append(r["net"])
    nets = [x["net"] for x in trades]
    mean = _mean(nets)
    coin = random_coin_means(d, pools, candles, pol, trades, data_end, sol_usd, sims=50 if quick else 300)
    wal = random_wallet_means(d, pools, candles, pol, buys, control, len(listed), data_end, sol_usd,
                              sims=20 if quick else 200)
    days_b = max(0.0, data_end - start) / DAY
    p = pol["pass"]
    rep = {"policy": pol["name"], "rules_signature": lock["rules_signature"], "frozen_at": lock["frozen_at"],
           "period_b_days": round(days_b, 2), "listed_wallets": len(listed), "signals": len(signals),
           "gate_fail": len(signals) - len(trades), "trades": len(trades), "open": sum(x["exit"] == "open" for x in trades),
           "mean_net": mean, "median_net": statistics.median(nets) if nets else None,
           "win_rate": (sum(v > 0 for v in nets) / len(nets)) if nets else None,
           "pnl_sol": round(sum(nets) * pol["trade"]["size_sol"], 4), "ungated_mean_net": _mean(ungated),
           "p_mean_positive": day_bootstrap(trades), "random_coin_mean": _mean(coin),
           "beats_random_coin_pct": _pct_beaten(mean, coin), "random_wallets_mean": _mean(wal),
           "beats_random_wallets_pct": _pct_beaten(mean, wal), "trade_list": trades}
    if days_b < p["min_days"] or len(trades) < p["min_signals"]:
        rep["verdict"] = f"not enough data yet: {days_b:.1f}/{p['min_days']} days, {len(trades)}/{p['min_signals']} trades"
    else:
        checks = {"p_mean_positive": (rep["p_mean_positive"] or 0) >= p["p_mean_positive"],
                  "beat_random_coin": (rep["beats_random_coin_pct"] or 0) >= p["beat_random_coin_pct"],
                  "beat_random_wallets": (rep["beats_random_wallets_pct"] or 0) >= p["beat_random_wallets_pct"]}
        rep["checks"] = checks
        rep["verdict"] = "PASS" if all(checks.values()) else "FAIL: " + ", ".join(k for k, v in checks.items() if not v)
    return rep


# ---------------------------------------------------------------------- commands
def _candles(data_dir: Path) -> dict:
    out = {}
    for p in (Path(data_dir) / "candles").glob("*.json"):
        try:
            out[p.stem] = json.loads(p.read_text())["daily"]
        except (ValueError, KeyError):
            continue
    return out


def control_path(pol: dict) -> Path:
    return RESEARCH / f"{pol['name']}-control.json"


def cmd_select(pol: dict, data_dir: Path = DATA) -> dict:
    lock = checked_lock(pol)
    end = _ts(lock["frozen_at"]) if lock.get("frozen_at") else None
    d = load_data(trade_files(data_dir), end=end, min_sol=pol["selection"]["min_price_sol"],
                  coin_cap=pol["selection"]["max_coins"])
    return select_wallets(d, pol["selection"])


def cmd_freeze(pol: dict, data_dir: Path = DATA, now: float | None = None) -> dict:
    lock = checked_lock(pol)
    if lock.get("frozen_at"):
        raise StudyError(f"{pol['name']} was frozen at {lock['frozen_at']}")
    sel = pol["selection"]
    res = cmd_select(pol, data_dir)
    if res["days"] < sel["period_days"]:
        raise StudyError(f"period A has {res['days']:.1f} of {sel['period_days']} days of data")
    if res["qualified"] < sel["min_wallets"] and res["days"] < sel["period_days"] + 7:
        raise StudyError(f"only {res['qualified']} wallets qualify (< {sel['min_wallets']}): the policy extends period A "
                         f"to {sel['period_days'] + 7} days")
    now = now or time.time()
    lock.update(frozen_at=_iso(now), period_a=[_iso(res["first_t"]), _iso(res["last_t"])], moves=res["moves"],
                qualified=res["qualified"], wallets=res["wallets"],
                control_hash=hashlib.sha256("\n".join(res["control"]).encode()).hexdigest()[:12])
    RESEARCH.mkdir(parents=True, exist_ok=True)
    control_path(pol).write_text(json.dumps(res["control"]))
    _write_lock(pol, lock)
    return lock


def cmd_eval(pol: dict, data_dir: Path = DATA, quick: bool = False) -> dict:
    lock = checked_lock(pol)
    if not lock.get("frozen_at"):
        raise StudyError(f"{pol['name']} isn't frozen yet: period A is still collecting (see `select`)")
    control = json.loads(control_path(pol).read_text())
    if hashlib.sha256("\n".join(control).encode()).hexdigest()[:12] != lock["control_hash"]:
        raise StudyError("the control-wallet file doesn't match the one frozen with the list")
    start = _ts(lock["frozen_at"])
    d = load_data(trade_files(data_dir), start=start - DAY, min_sol=pol["selection"]["min_price_sol"])
    rep = evaluate(pol, lock, d, load_pools(data_dir), _candles(data_dir), control, quick=quick)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    (RESEARCH / f"{pol['name']}-eval.json").write_text(json.dumps(rep, indent=1, default=str))
    return rep


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def print_select(res: dict, pol: dict) -> None:
    sel = pol["selection"]
    print(f"Period A so far: {res['days']:.1f} of {sel['period_days']} days, {res['moves']} moves of {sel['runner_x']}x+, "
          f"{res['qualified']} wallets qualify (need {sel['min_wallets']}), {res['near_misses']} one hit short, "
          f"{res['bots_excluded']} bot wallets excluded, {len(res['control'])} control wallets")
    for w in res["wallets"][:30]:
        print(f"  {w['wallet']}  hits {w['hits']}  good exits {w['good_exits']}  early {w['early_sol']:.2f} SOL  "
              f"{', '.join(w['coins'][:6])}")


def print_eval(rep: dict) -> None:
    print(f"{rep['policy']} (rules {rep['rules_signature']}, list frozen {rep['frozen_at']}): period B {rep['period_b_days']} days")
    print(f"  {rep['listed_wallets']} wallets -> {rep['signals']} signals, {rep['gate_fail']} failed the gates, "
          f"{rep['trades']} trades ({rep['open']} still open)")
    print(f"  mean net {_pct(rep['mean_net'])}, median {_pct(rep['median_net'])}, win rate "
          f"{'-' if rep['win_rate'] is None else f'{rep['win_rate']:.0%}'}, P&L {rep['pnl_sol']:+.3f} SOL at the policy size")
    print(f"  without the gates: {_pct(rep['ungated_mean_net'])}")
    print(f"  P(mean > 0) {rep['p_mean_positive'] if rep['p_mean_positive'] is not None else '-'}; random coin mean "
          f"{_pct(rep['random_coin_mean'])} (beaten {rep['beats_random_coin_pct'] if rep['beats_random_coin_pct'] is not None else '-'}%); "
          f"random wallets mean {_pct(rep['random_wallets_mean'])} (beaten "
          f"{rep['beats_random_wallets_pct'] if rep['beats_random_wallets_pct'] is not None else '-'}%)")
    print(f"  VERDICT: {rep['verdict']}")
