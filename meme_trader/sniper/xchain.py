"""Paper trading on other chains: young DEX coins on BNB Chain, Base, Ethereum and Solana (outside pump.fun).

Same paper balance, daily loss limit, kill switch, Pause button and trade log as the pump.fun bots, but a different
game: prices are polled (DexScreener, every `poll_s`), so this trades momentum over minutes to hours, never launches.
Candidates come from GeckoTerminal's trending and new pools (ui/chains.py, shared with the Charts view).

Every paper fill pays what a real one would: the pool's swap fee, price impact from its liquidity, the token's own
buy/sell tax, the network's gas, plus `extra_slip_pct` for the seconds between the quote and a real fill. A coin is
traded only when it can be sold again: on EVM chains honeypot.is must have simulated a buy and a sell (low risk,
taxes <= max_tax_pct), or, for coins it doesn't know, GoPlus must clear every check (not a honeypot, open source,
not upgradeable, no blacklist, no pausing, no owner balance changes); on Solana the mint's freeze and mint
authorities must be renounced. Measured 2026-10-05: 3 of the 8 trending BNB Chain pools and 1 of 8 on Base were
honeypots, and honeypot.is didn't know most brand-new pools or some large BNB Chain ones.

Paper only: there's no EVM wallet or executor, so in live mode this never opens anything.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "chains": ["bsc", "base", "solana"],   # "eth" too if you like: its gas (~$1-3 a swap) eats small trades
    "poll_s": 10, "scan_s": 90, "max_open": 4,
    "size_usd": 0,                         # 0 = the risk dial's sizing.base_usd
    "min_liq_usd": 20_000, "min_vol_h1_usd": 20_000, "min_age_min": 15, "max_age_h": 168,
    "min_mcap_usd": 50_000, "max_mcap_usd": 20_000_000,
    "min_buy_ratio": 1.0,                  # buys / sells in the last hour
    "min_chg_h1": 5, "max_chg_h1": 150,    # moving up, not already gone vertical
    "max_tax_pct": 5,
    "stop_loss_pct": 15, "take_profit_pct": 40, "take_profit_fraction": 0.5,
    "trail_after_pct": 20, "trail_pct": 20, "max_hold_h": 6, "cooldown_h": 6,
    "extra_slip_pct": 0.5,
    "gas_usd": {"bsc": 0.05, "base": 0.02, "eth": 1.5, "solana": 0.01},
}
HONEYPOT_CHAIN = {"bsc": 56, "base": 8453, "eth": 1}
MAJORS = {"WETH", "ETH", "WBNB", "BNB", "SOL", "WSOL", "USDC", "USDT", "DAI", "BUSD", "FDUSD", "USD1", "USDE",
          "WBTC", "BTCB", "CBBTC", "BTC", "STETH", "WSTETH", "CBETH", "JITOSOL", "MSOL"}
DEX_FEE = {"pancakeswap_v2": 0.25, "pancakeswap-v2-bsc": 0.25, "uniswap_v2": 0.3, "raydium": 0.25, "pumpswap": 0.3}
DS = "https://api.dexscreener.com"


def fee_pct(row: dict) -> float:
    """The pool's swap fee: v3/v4 pools carry their fee tier in GeckoTerminal's name ("X / WETH 1%")."""
    m = re.search(r"([\d.]+)%\s*$", row.get("name") or "")
    if m:
        return float(m.group(1))
    return DEX_FEE.get(row.get("dex_id") or "", 0.3)


def impact_pct(usd: float, liq_usd: float) -> float:
    """Price impact of a swap this size against a constant-product pool holding liq_usd (half on each side)."""
    return usd / max(liq_usd / 2, 1.0) * 100


def screen(row: dict, cfg: dict) -> str:
    """'' if the pool is a candidate, else why not."""
    if (row.get("symbol") or "").upper() in MAJORS:
        return "major or stablecoin"
    if row.get("dex_id") == "pump-fun":
        return "still on the pump.fun curve (the main bots' game)"
    age = row.get("age_s")
    if age is None or age < cfg["min_age_min"] * 60:
        return "too new"
    if age > cfg["max_age_h"] * 3600:
        return f"older than {cfg['max_age_h']} h"
    liq = row.get("liq_usd") or 0
    if liq < cfg["min_liq_usd"]:
        return f"liquidity ${liq:,.0f}"
    mc = row.get("mcap_usd") or row.get("fdv_usd") or 0
    if not cfg["min_mcap_usd"] <= mc <= cfg["max_mcap_usd"]:
        return f"market cap ${mc:,.0f}"
    if (row.get("vol_h1") or 0) < cfg["min_vol_h1_usd"]:
        return f"1h volume ${row.get('vol_h1') or 0:,.0f}"
    b, s = row.get("buys_h1") or 0, row.get("sells_h1") or 0
    if b < cfg["min_buy_ratio"] * max(s, 1):
        return f"buys/sells {b}/{s}"
    h1, m5 = row.get("chg_h1"), row.get("chg_m5")
    if h1 is None or not cfg["min_chg_h1"] <= h1 <= cfg["max_chg_h1"]:
        return f"1h move {h1 if h1 is not None else '?'}%"
    if m5 is None or m5 < 0:
        return "falling in the last 5 min"
    return ""


def judge_honeypot(d: dict | None, max_tax: float) -> tuple[bool, str, dict]:
    """honeypot.is's answer -> (tradable, why not, {buy_tax, sell_tax}). No answer is a no."""
    if not d:
        return False, "honeypot.is hasn't checked it yet", {}
    hp, sim, sm = d.get("honeypotResult") or {}, d.get("simulationResult") or {}, d.get("summary") or {}
    if hp.get("isHoneypot") is not False:
        return False, "honeypot (can't sell)" if hp.get("isHoneypot") else "honeypot.is couldn't simulate a sale", {}
    if (sm.get("risk") or "") != "low":
        return False, f"honeypot.is risk: {sm.get('risk') or '?'}", {}
    bt, st = float(sim.get("buyTax") or 0), float(sim.get("sellTax") or 0)
    if max(bt, st) > max_tax:
        return False, f"tax {bt:g}% buy / {st:g}% sell", {}
    return True, "", {"buy_tax": bt, "sell_tax": st}


GOPLUS_MUST_BE_0 = ("is_honeypot", "cannot_sell_all", "is_blacklisted", "transfer_pausable", "owner_change_balance",
                    "hidden_owner", "slippage_modifiable", "is_proxy", "selfdestruct", "external_call")


def judge_goplus(d: dict | None, token: str, max_tax: float) -> tuple[bool, str, dict]:
    """GoPlus token_security -> (tradable, why not, taxes). Every check must be answered and clean."""
    res = ((d or {}).get("result") or {}).get(token.lower())
    if not res:
        return False, "no safety check has seen it yet", {}
    for k in GOPLUS_MUST_BE_0:
        v = res.get(k)
        if v not in ("0", None) or (v is None and k in ("is_honeypot", "is_proxy")):
            return False, f"GoPlus: {k.replace('_', ' ')}" + ("" if v == "1" else " unknown"), {}
    if res.get("is_open_source") != "1":
        return False, "GoPlus: contract not open source", {}
    try:
        bt, st = float(res.get("buy_tax") or 0) * 100, float(res.get("sell_tax") or 0) * 100
    except ValueError:
        return False, "GoPlus: unreadable tax", {}
    if max(bt, st) > max_tax:
        return False, f"tax {bt:g}% buy / {st:g}% sell", {}
    return True, "", {"buy_tax": bt, "sell_tax": st}


def judge_solana_mint(d: dict | None, max_tax: float) -> tuple[bool, str, dict]:
    """getAccountInfo (jsonParsed) of a Solana mint -> (tradable, why not, {buy_tax, sell_tax})."""
    try:
        info = d["result"]["value"]["data"]["parsed"]["info"]
    except (KeyError, TypeError):
        return False, "couldn't read the token's mint", {}
    if info.get("freezeAuthority"):
        return False, "the creator can freeze holders' tokens", {}
    if info.get("mintAuthority"):
        return False, "the creator can still mint more", {}
    fee = 0.0
    for x in info.get("extensions") or []:
        if x.get("extension") == "transferFeeConfig":
            fee = float(((x.get("state") or {}).get("newerTransferFee") or {}).get("transferFeeBasisPoints") or 0) / 100
    if fee > max_tax:
        return False, f"transfer fee {fee:g}%", {}
    return True, "", {"buy_tax": fee, "sell_tax": fee}


class XChain:
    def __init__(self, engine, path: Path | None):
        self.e = engine
        self.path = Path(path) if path else None
        self.positions: dict[str, dict] = {}
        self.cooldown: dict[str, float] = {}
        self.checks: dict[str, tuple] = {}          # chain:token -> (ts, ok, why, info)
        self.scan: list[dict] = []
        self.last_scan = 0.0
        self.notes: deque = deque(maxlen=12)
        self.http = None
        self._chains = None
        self._recorded = None                          # the lists last kept (their fetch time)
        self.load()

    @property
    def cfg(self) -> dict:
        return {**DEFAULTS, **(self.e.p.get("xchain") or {})}

    @property
    def active(self) -> bool:
        """New entries allowed by config and mode (open positions are always managed)."""
        return bool(self.cfg["enabled"]) and self.e.mode == "paper"

    # ------------------------------------------------------------------ state
    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.positions, self.cooldown = d.get("positions") or {}, d.get("cooldown") or {}

    def save(self) -> None:
        if not self.path:
            return
        now = time.time()
        self.cooldown = {k: t for k, t in self.cooldown.items() if now - t < 86400}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"positions": self.positions, "cooldown": self.cooldown}))
        tmp.replace(self.path)

    def note(self, text: str) -> None:
        self.notes.appendleft((time.time(), text[:200]))

    # ------------------------------------------------------------------ data
    async def _session(self):
        import aiohttp
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"Accept": "application/json"})
        return self.http

    async def _get(self, url: str) -> dict | list | None:
        s = await self._session()
        async with s.get(url) as r:
            if r.status == 404:
                return None
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} from {url.split('/')[2]}")
            return await r.json(content_type=None)

    @staticmethod
    def _k(chain: str, addr: str) -> str:
        return f"{chain}:{addr if chain == 'solana' else addr.lower()}"

    async def quotes(self, chain: str, pools: list[str]) -> dict[str, dict]:
        """Live price, liquidity and 5-minute flow per pool, from DexScreener (30 pools a call)."""
        from ..ui.chains import NETWORKS
        out = {}
        for i in range(0, len(pools), 30):
            d = await self._get(f"{DS}/latest/dex/pairs/{NETWORKS[chain][1]}/{','.join(pools[i:i + 30])}") or {}
            for p in d.get("pairs") or []:
                try:
                    price = float(p.get("priceUsd") or 0)
                except ValueError:
                    continue
                if price <= 0:
                    continue
                m5 = (p.get("txns") or {}).get("m5") or {}
                out[self._k(chain, p.get("pairAddress") or "")] = {
                    "price": price, "liq": float((p.get("liquidity") or {}).get("usd") or 0),
                    "mcap": p.get("marketCap") or p.get("fdv"), "buys_m5": m5.get("buys") or 0, "sells_m5": m5.get("sells") or 0}
        return out

    async def safety(self, chain: str, token: str) -> tuple[bool, str, dict]:
        key, now = self._k(chain, token), time.time()
        c = self.checks.get(key)
        if c and now - c[0] < (6 * 3600 if c[1] else 1800):      # a "not yet" is asked again after 30 min
            return c[1], c[2], c[3]
        max_tax = float(self.cfg["max_tax_pct"])
        if chain == "solana":
            s = await self._session()
            rpc = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
            async with s.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                         "params": [token, {"encoding": "jsonParsed"}]}) as r:
                res = judge_solana_mint(await r.json(content_type=None) if r.status == 200 else None, max_tax)
        else:
            d = await self._get(f"https://api.honeypot.is/v2/IsHoneypot?address={token}&chainID={HONEYPOT_CHAIN[chain]}")
            res = judge_honeypot(d, max_tax)
            if not d or (d.get("honeypotResult") or {}).get("isHoneypot") is None:   # honeypot.is has no verdict
                g = await self._get(f"https://api.gopluslabs.io/api/v1/token_security/{HONEYPOT_CHAIN[chain]}?contract_addresses={token}")
                res = judge_goplus(g, token, max_tax)
        self.checks[key] = (now, *res)
        if len(self.checks) > 2000:
            for k in sorted(self.checks, key=lambda k: self.checks[k][0])[:500]:
                del self.checks[k]
        return res

    # ------------------------------------------------------------------ the loop
    async def run(self) -> None:
        while True:
            try:
                await self.step(time.time())
            except asyncio.CancelledError:
                raise
            except Exception as err:                    # a bad answer from an API must never stop the loop
                self.note(f"{type(err).__name__}: {err}")
            await asyncio.sleep(float(self.cfg["poll_s"]))

    async def close(self) -> None:
        self.save()
        if self.http is not None and not self.http.closed:
            await self.http.close()

    async def step(self, now: float) -> None:
        if self.positions:
            await self.refresh(now)
            self.check_exits(now)
        if self.active and now - self.last_scan >= float(self.cfg["scan_s"]):
            self.last_scan = now
            await self.scan_and_enter(now)

    async def refresh(self, now: float) -> None:
        by_chain: dict[str, list[str]] = {}
        for p in self.positions.values():
            by_chain.setdefault(p["chain"], []).append(p["pool"])
        for chain, pools in by_chain.items():
            q = await self.quotes(chain, pools)
            for p in self.positions.values():
                x = q.get(self._k(p["chain"], p["pool"])) if p["chain"] == chain else None
                if x:
                    p.update(last=x["price"], last_ts=now, liq=x["liq"] or p["liq"], mcap=x["mcap"])
                    p["peak"], p["trough"] = max(p["peak"], x["price"]), min(p["trough"], x["price"])

    def block(self, cost_sol: float) -> str:
        e, cfg = self.e, self.cfg
        if e.book.halted:
            return "halted: " + e.book.halted
        if e.paused:
            return "paused"
        if -e.book.day_pnl >= e.p.capital.daily_loss_limit_sol:
            return "daily loss limit"
        if len(self.positions) >= int(cfg["max_open"]):
            return "max positions"
        if e.book.available - cost_sol < e.p.capital.min_sol_reserve:
            return "low SOL"
        return ""

    def size_usd(self) -> float:
        return float(self.cfg["size_usd"] or self.e.p.sizing.base_usd)

    async def _chains_data(self) -> dict:
        c = getattr(self.e, "chains", None)
        if c is None:
            if self._chains is None:
                from ..ui.chains import Chains
                self._chains = Chains()
            c = self._chains
        c.trade_nets = set(self.cfg["chains"])
        return await c.get()

    async def scan_and_enter(self, now: float) -> None:
        cfg = self.cfg
        d = await self._chains_data()
        rows, seen = [], set()
        for kind in ("hot", "trending", "new"):            # hot = most active in the last hour: where young coins show up
            for chain in cfg["chains"]:
                for r in (d.get(kind) or {}).get(chain) or []:
                    k = self._k(chain, r["pool"])
                    if k not in seen:
                        seen.add(k)
                        rows.append(r)
        held = {self._k(p["chain"], p["token"]) for p in self.positions.values()}
        out = []
        for r in rows:
            why = screen(r, cfg)
            tk = self._k(r["chain"], r["token"])
            if not why and (tk in held or self._k(r["chain"], r["pool"]) in self.positions):
                why = "held"
            elif not why and now - self.cooldown.get(tk, 0) < float(cfg["cooldown_h"]) * 3600:
                why = "traded lately"
            out.append({**r, "why": why})
        out.sort(key=lambda r: (r["why"] != "", -(r.get("vol_h1") or 0)))
        self.scan = [{k: r.get(k) for k in ("chain", "chain_name", "symbol", "name", "pool", "token", "mcap_usd", "liq_usd",
                                             "vol_h1", "chg_h1", "chg_m5", "buys_h1", "sells_h1", "age_s", "dex", "why")}
                     for r in out[:15]]
        if d.get("fetched", 0) != self._recorded:          # new lists only: the same lists aren't kept twice
            self._recorded = d.get("fetched", 0)
            self._record(now, [r for r in out if (r.get("age_s") or 0) < 14 * 86400 and r["why"] != "major or stablecoin"])
        size = self.size_usd()
        sol_usd = self.e.sol_price.usd or 1.0
        for r in [r for r in out if not r["why"]][:6]:
            why = self.block((size + cfg["gas_usd"].get(r["chain"], 0.05)) / sol_usd)
            if why:
                self._why(r, why)
                break
            q = (await self.quotes(r["chain"], [r["pool"]])).get(self._k(r["chain"], r["pool"]))
            if not q:
                self._why(r, "no live price on DexScreener")
                continue
            if q["sells_m5"] > q["buys_m5"]:
                self._why(r, f"sellers now ({q['buys_m5']}/{q['sells_m5']} in 5 min)")
                continue
            ok, why, info = await self.safety(r["chain"], r["token"])
            if not ok:
                self._why(r, why)
                continue
            self.open(r, q, info, size, now)

    def _why(self, r: dict, why: str) -> None:
        for x in self.scan:
            if x["pool"] == r["pool"]:
                x["why"] = why

    def _record(self, now: float, rows: list[dict]) -> None:
        """Every scan, kept for research: what the market offered and why each was passed over."""
        if not self.path:
            return
        d = self.path.parent / "xchain"
        d.mkdir(exist_ok=True)
        with (d / f"scan-{time.strftime('%Y-%m-%d', time.gmtime(now))}.jsonl").open("a") as f:
            f.write(json.dumps({"ts": round(now), "rows": [{k: r.get(k) for k in (
                "chain", "pool", "token", "symbol", "mcap_usd", "liq_usd", "vol_h1", "chg_m5", "chg_h1", "buys_h1",
                "sells_h1", "age_s", "price_usd", "why")} for r in rows]}) + "\n")

    # ------------------------------------------------------------------ fills (paper)
    def open(self, r: dict, q: dict, info: dict, size_usd: float, now: float) -> dict:
        cfg, e = self.cfg, self.e
        sol_usd = e.sol_price.usd or 1.0
        liq = q["liq"] or r.get("liq_usd") or 0
        fee = fee_pct(r)
        imp = impact_pct(size_usd, liq) + float(cfg["extra_slip_pct"])
        gas = float(cfg["gas_usd"].get(r["chain"], 0.05))
        tokens = size_usd * (1 - (fee + info.get("buy_tax", 0)) / 100) / (q["price"] * (1 + imp / 100))
        cost_usd = size_usd + gas
        cost_sol = cost_usd / sol_usd
        e.book.sol -= cost_sol
        key = self._k(r["chain"], r["pool"])
        pos = {"key": key, "chain": r["chain"], "chain_name": r.get("chain_name") or r["chain"], "pool": r["pool"],
               "token": r["token"], "symbol": r.get("symbol") or "?", "name": r.get("name") or "", "url": r.get("dex") or "",
               "opened": now, "size_usd": size_usd, "cost_usd": cost_usd, "cost_sol": cost_sol, "cost_sol_left": cost_sol,
               "tokens": tokens, "tokens0": tokens, "entry": q["price"], "fee_pct": fee, "buy_tax": info.get("buy_tax", 0),
               "sell_tax": info.get("sell_tax", 0), "liq": liq, "liq0": liq, "mcap": q.get("mcap"), "mcap0": q.get("mcap"),
               "last": q["price"], "last_ts": now, "peak": q["price"], "trough": q["price"],
               "proceeds_sol": 0.0, "proceeds_usd": 0.0, "tp_taken": False, "exits": [], "sol_usd0": sol_usd,
               "why": f"1h {r.get('chg_h1'):+.0f}%, {r.get('buys_h1')}/{r.get('sells_h1')} buys/sells, ${(r.get('vol_h1') or 0) / 1000:,.0f}k vol"}
        self.positions[key] = pos
        self.save()
        mc = q.get("mcap")
        e.say("buy", f"🌐 {pos['symbol']} on {pos['chain_name']}: ${size_usd:,.0f} (paper){f' at ${mc / 1e6:,.2f}M market cap' if mc else ''} | {pos['why']}")
        return pos

    def sell(self, key: str, frac: float, reason: str, now: float, price: float | None = None) -> None:
        pos = self.positions.get(key)
        if pos is None:
            return
        cfg, e = self.cfg, self.e
        sol_usd = e.sol_price.usd or 1.0
        frac = min(max(frac, 0.0), 1.0)
        px = pos["last"] if price is None else price
        sold = pos["tokens"] * frac
        gross = sold * px
        imp = impact_pct(gross, pos["liq"]) + float(cfg["extra_slip_pct"])
        usd = gross * max(0.0, 1 - imp / 100) * (1 - (pos["fee_pct"] + pos["sell_tax"]) / 100) - float(cfg["gas_usd"].get(pos["chain"], 0.05))
        sol = usd / sol_usd
        cost_part = pos["cost_sol_left"] * frac
        pos["tokens"] -= sold
        pos["cost_sol_left"] -= cost_part
        pos["proceeds_sol"] += sol
        pos["proceeds_usd"] += usd
        e.book.sol += sol
        e.book.day_pnl += sol - cost_part
        pos["exits"].append([now, reason, round(frac, 3), round(sol, 6)])
        e.say("sell", f"🌐 {pos['symbol']} ({pos['chain_name']}) {frac:.0%} for ${usd:,.2f} | {reason}")
        if frac >= 0.999 or pos["tokens"] * px < 1.0:
            self._close(pos, now)
        else:
            self.save()

    def _close(self, pos: dict, now: float) -> None:
        e = self.e
        self.positions.pop(pos["key"], None)
        if pos["cost_sol_left"] > 0:                    # an unsold remainder is written off, today
            e.book.day_pnl -= pos["cost_sol_left"]
            pos["cost_sol_left"] = 0.0
        self.cooldown[self._k(pos["chain"], pos["token"])] = now
        pnl = pos["proceeds_sol"] - pos["cost_sol"]
        usd = e.sol_price.usd
        row = {"mint": pos["token"], "symbol": pos["symbol"], "opened": pos["opened"], "closed": now,
               "cost": pos["cost_sol"], "proceeds": pos["proceeds_sol"], "pnl": pnl,
               "pnl_pct": pnl / max(pos["cost_sol"], 1e-12) * 100,
               "peak_gain_pct": (pos["peak"] / pos["entry"] - 1) * 100, "mae_pct": (pos["trough"] / pos["entry"] - 1) * 100,
               "score": 0.0, "initials": pos["tp_taken"], "exit": pos["exits"][-1][1] if pos["exits"] else "",
               "source": "chains", "desk": "", "p": None,
               "chain": pos["chain"], "pool": pos["pool"], "url": pos["url"],
               "cost_usd": round(pos["cost_usd"], 2), "proceeds_usd": round(pos["proceeds_usd"], 2),
               "pnl_usd": round(pos["proceeds_usd"] - pos["cost_usd"], 2),
               "entry_mcap_usd": round(pos["mcap0"]) if pos.get("mcap0") else None,
               "exit_mcap_usd": round(pos["mcap"]) if pos.get("mcap") else None,
               "entry_mcap_sol": round(pos["mcap0"] / pos["sol_usd0"], 2) if pos.get("mcap0") else None,
               "exit_mcap_sol": round(pos["mcap"] / usd, 2) if pos.get("mcap") and usd else None,
               "fees": {"swap_pct": pos["fee_pct"], "buy_tax": pos["buy_tax"], "sell_tax": pos["sell_tax"]}}
        e.record_close(row, pos["key"])
        self.save()

    def check_exits(self, now: float) -> None:
        cfg = self.cfg
        for key, p in list(self.positions.items()):
            if self.e.book.halted:
                self.sell(key, 1.0, "kill switch", now)
                continue
            px, entry = p["last"], p["entry"]
            stale = now - p["last_ts"]
            if now - p["opened"] >= float(cfg["max_hold_h"]) * 3600:
                if stale > 1800:                         # no price for 30 min at the end: assume the worst
                    self.sell(key, 1.0, "no price for 30 min (pool gone?)", now, price=0.0)
                else:
                    self.sell(key, 1.0, f"held {cfg['max_hold_h']} h", now)
            elif stale > 120:
                continue                                  # don't act on an old price
            elif p["liq0"] and p["liq"] < 0.4 * p["liq0"]:
                self.sell(key, 1.0, f"liquidity pulled (${p['liq']:,.0f} from ${p['liq0']:,.0f})", now)
            elif px <= entry * (1 - float(cfg["stop_loss_pct"]) / 100):
                self.sell(key, 1.0, f"stop loss -{cfg['stop_loss_pct']}%", now)
            elif not p["tp_taken"] and px >= entry * (1 + float(cfg["take_profit_pct"]) / 100):
                p["tp_taken"] = True
                self.sell(key, float(cfg["take_profit_fraction"]), f"take profit +{cfg['take_profit_pct']}%", now)
            elif p["peak"] >= entry * (1 + float(cfg["trail_after_pct"]) / 100) and px <= p["peak"] * (1 - float(cfg["trail_pct"]) / 100):
                self.sell(key, 1.0, f"trailing stop {cfg['trail_pct']}% under the peak", now)

    def sell_now(self, key: str) -> str:
        if key not in self.positions:
            return "no such position"
        self.sell(key, 1.0, "you sold", time.time())
        return ""

    # ------------------------------------------------------------------ views
    def value_sol(self) -> float:
        sol_usd = self.e.sol_price.usd or 1.0
        return sum(p["tokens"] * p["last"] for p in self.positions.values()) / sol_usd

    def view(self) -> dict:
        now, sol_usd = time.time(), self.e.sol_price.usd or 1.0
        cfg = self.cfg
        pos = []
        for p in self.positions.values():
            val = p["tokens"] * p["last"]
            pnl_usd = p["proceeds_usd"] + val - p["cost_usd"]
            pos.append({k: p[k] for k in ("key", "chain", "chain_name", "symbol", "name", "url", "opened", "size_usd", "mcap0",
                                          "mcap", "why", "tp_taken")}
                       | {"value_usd": round(val, 2), "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_usd / p["cost_usd"] * 100, 1),
                          "move_pct": round((p["last"] / p["entry"] - 1) * 100, 1), "stale_s": round(now - p["last_ts"])})
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        today = [c for c in self.e.book.closed if c.get("source") == "chains" and time.strftime("%Y-%m-%d", time.gmtime(c["closed"])) == day]
        size = self.size_usd()
        mode = self.e.mode
        note = "" if mode == "paper" else ("demo: other chains trade only in the real paper bot" if "synthetic" in mode
                                           else "live mode: other chains are paper only, so they're off")
        return {"active": self.active, "enabled": bool(cfg["enabled"]), "paper_only": mode != "paper", "mode_note": note,
                "chains": cfg["chains"], "size_usd": size, "max_open": cfg["max_open"],
                "blocked": self.block(size / sol_usd) if self.active else "", "positions": pos, "scan": self.scan,
                "last_scan": self.last_scan, "notes": [{"ts": t, "text": x} for t, x in list(self.notes)[:5]],
                "today": {"trades": len(today), "pnl_sol": round(sum(c["pnl"] for c in today), 4),
                          "pnl_usd": round(sum(c.get("pnl_usd") or 0 for c in today), 2)},
                "rules": {k: cfg[k] for k in ("min_liq_usd", "min_vol_h1_usd", "min_age_min", "max_age_h", "min_mcap_usd", "max_mcap_usd",
                                              "min_buy_ratio", "min_chg_h1", "max_chg_h1", "max_tax_pct", "stop_loss_pct",
                                              "take_profit_pct", "trail_pct", "max_hold_h")}}

    def brief(self) -> dict:
        """What the AI desk reads about the other-chain bot."""
        v = self.view()
        return {"active": v["active"], "chains": v["chains"], "today": v["today"],
                "open": [{"coin": f"{p['symbol']} ({p['chain_name']})", "pnl_pct": p["pnl_pct"], "held_min": round((time.time() - p["opened"]) / 60)}
                         for p in v["positions"]],
                "top_candidates": [f"{r['symbol']} ({r['chain_name']}): {r['why'] or 'passes'}" for r in v["scan"][:5]]}
