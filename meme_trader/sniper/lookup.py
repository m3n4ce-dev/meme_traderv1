"""Metrics for ANY Solana token by contract address (CA), tracked by the bot or not.

Sources, all keyless and fetched concurrently (each one optional - a slow or failing source just
leaves its fields empty):
  * on-chain (Solana RPC): the pump.fun bonding curve (price, market cap, curve %, graduated?, Mayhem
    mode), supply, mint/freeze authority; the largest holders only with your own SOLANA_RPC_URL
    (public endpoints refuse that call);
  * DexScreener: price, liquidity, volume, buys/sells, price change, pair age, socials, image;
  * RugCheck: risk flags, top holders with insider flags, holder count;
  * GeckoTerminal: candles for listed pools;
  * the engine itself, when it tracks the token (score, gates, insider cluster, our position).

    python -m meme_trader.sniper.lookup <mint>      # print the lookup as JSON
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import struct
import time

import httpx

from .curve import CURVE_TOKENS, INITIAL_V_TOKENS, TOTAL_SUPPLY

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL = "So11111111111111111111111111111111111111112"
B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
CACHE_S = 15
_cache: dict[str, tuple[float, dict]] = {}


def is_mint(text: str) -> bool:
    t = str(text or "").strip()
    if not (32 <= len(t) <= 44 and set(t) <= B58):
        return False
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(t)
        return True
    except Exception:
        return False


def rpc_urls() -> list[str]:
    urls = [os.environ.get("SOLANA_RPC_URL", "")]
    ws = os.environ.get("SOLANA_WS_URL", "")
    if ws.startswith(("wss://", "ws://")):
        urls.append("http" + ws[2:])
    urls += ["https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"]
    return list(dict.fromkeys(u for u in urls if u))


def curve_address(mint: str) -> str:
    from solders.pubkey import Pubkey

    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                         Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def decode_curve(data: bytes) -> dict | None:
    """pump.fun BondingCurve account: 8-byte discriminator, 5 x u64 reserves, complete flag, creator,
    then (newer curves) a Mayhem-mode flag - those tokens mint 2B, half of it to pump.fun's trading agent."""
    if len(data) < 49:
        return None
    v_tok, v_sol, r_tok, r_sol, supply = struct.unpack_from("<5Q", data, 8)
    if not v_tok:
        return None
    out = {"v_tokens": v_tok / 1e6, "v_sol": v_sol / 1e9, "real_tokens": r_tok / 1e6, "real_sol": r_sol / 1e9,
           "supply": supply / 1e6, "complete": bool(data[48])}
    if len(data) >= 81:
        from solders.pubkey import Pubkey

        out["creator"] = str(Pubkey.from_bytes(data[49:81]))
    if len(data) >= 82:
        out["mayhem"] = bool(data[81])
    out["price_sol"] = out["v_sol"] / out["v_tokens"]
    out["mcap_sol"] = out["price_sol"] * (out["supply"] or TOTAL_SUPPLY)
    out["progress_pct"] = 100.0 if out["complete"] else \
        round(min(max((INITIAL_V_TOKENS - out["v_tokens"]) / CURVE_TOKENS, 0.0), 1.0) * 100, 2)
    return out


class _Rpc:
    def __init__(self, http: httpx.AsyncClient):
        self.http, self.urls = http, rpc_urls()

    async def call(self, method: str, params: list):
        last = None
        for url in self.urls:
            try:
                r = await self.http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                d = r.json()
                if "error" in d:
                    last = RuntimeError(str(d["error"])[:200])
                    continue
                return d.get("result")
            except (httpx.HTTPError, ValueError) as e:
                last = e
        raise last or RuntimeError("no RPC")


async def _onchain(rpc: _Rpc, mint: str) -> dict:
    curve_pda = curve_address(mint)
    own = os.environ.get("SOLANA_RPC_URL", "")

    async def largest_accounts():
        if not own:                          # PublicNode wants a personal token for it; the public RPC rate-limits it
            return None
        r = await rpc.http.post(own, json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                                           "params": [mint, {"commitment": "confirmed"}]})
        return r.json().get("result")
    acc, mint_acc, largest = await asyncio.gather(
        rpc.call("getAccountInfo", [curve_pda, {"encoding": "base64", "commitment": "confirmed"}]),
        rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]),
        largest_accounts(), return_exceptions=True)
    out: dict = {"curve_address": curve_pda}
    if isinstance(acc, dict) and acc.get("value"):
        out["curve"] = decode_curve(base64.b64decode(acc["value"]["data"][0]))
    if isinstance(mint_acc, dict) and mint_acc.get("value"):
        v = mint_acc["value"]
        info = ((v.get("data") or {}).get("parsed") or {}).get("info") or {}
        if not info:
            raise ValueError("not a token mint")
        out["mint"] = {"supply": int(info.get("supply", 0)) / 10 ** int(info.get("decimals", 0)),
                       "decimals": info.get("decimals"), "mint_authority": info.get("mintAuthority"),
                       "freeze_authority": info.get("freezeAuthority"),
                       "program": (v.get("data") or {}).get("program", "")}
    elif isinstance(mint_acc, Exception):
        out["error"] = f"RPC: {type(mint_acc).__name__}"
    else:
        raise ValueError("no such account on Solana")
    if isinstance(largest, dict):
        accts = (largest.get("value") or [])[:20]
        owners = {}
        if accts:
            try:
                r = await rpc.http.post(own, json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                                                   "params": [[a["address"] for a in accts],
                                                              {"encoding": "jsonParsed", "commitment": "confirmed"}]})
                for a, v in zip(accts, (r.json().get("result") or {}).get("value") or []):
                    parsed = ((v or {}).get("data") or {}).get("parsed") or {}
                    owners[a["address"]] = (parsed.get("info") or {}).get("owner")
            except Exception:
                pass
        out["largest"] = [{"account": a["address"], "owner": owners.get(a["address"]),
                           "amount": float(a.get("uiAmount") or 0)} for a in accts]
    return out


async def _dexscreener(http: httpx.AsyncClient, mint: str) -> dict | None:
    r = await http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
    pairs = [p for p in (r.json() or []) if (p.get("baseToken") or {}).get("address") == mint]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0,
                                     (p.get("volume") or {}).get("h24") or 0))
    best["_pairs"] = len(pairs)
    return best


async def _rugcheck(http: httpx.AsyncClient, mint: str) -> dict | None:
    r = await http.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    return r.json() if r.status_code == 200 else None


def default_tf(age_s: float | None) -> str:
    """The card's first chart: 1-minute candles under 2 hours old, 5-minute under a day, else 15-minute."""
    return "1m" if age_s is not None and age_s < 2 * 3600 else "5m" if age_s is not None and age_s < 24 * 3600 else "15m"


TICKER_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9_]{0,14})\b")


async def search_ticker(sym: str) -> list[dict]:
    """Solana coins trading as $SYM (DexScreener search, exact symbol), the most liquid first. Copies share tickers,
    so the caller shows the first and lists the rest."""
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "meme_trader/0.1"}) as http:
        r = await http.get("https://api.dexscreener.com/latest/dex/search", params={"q": sym})
    if r.status_code != 200:
        return []
    by: dict[str, dict] = {}
    for p in (r.json() or {}).get("pairs") or []:
        b = p.get("baseToken") or {}
        if p.get("chainId") != "solana" or str(b.get("symbol") or "").lower() != sym.lower() or not b.get("address"):
            continue
        x = by.setdefault(b["address"], {"mint": b["address"], "symbol": b.get("symbol"), "name": str(b.get("name") or "")[:60],
                                         "liq_usd": 0.0, "vol_h24": 0.0, "mcap_usd": 0.0, "pairs": 0})
        x["liq_usd"] = max(x["liq_usd"], float((p.get("liquidity") or {}).get("usd") or 0))
        x["vol_h24"] += float((p.get("volume") or {}).get("h24") or 0)
        x["mcap_usd"] = max(x["mcap_usd"], float(p.get("marketCap") or p.get("fdv") or 0))
        x["pairs"] += 1
    return sorted(by.values(), key=lambda x: (x["liq_usd"], x["vol_h24"]), reverse=True)[:6]


async def _candles(http: httpx.AsyncClient, pool: str, age_s: float | None) -> list:
    tf, agg, limit = {"1m": ("minute", 1, 120), "5m": ("minute", 5, 144), "15m": ("minute", 15, 96)}[default_tf(age_s)]
    r = await http.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{tf}",
                       params={"aggregate": agg, "limit": limit, "currency": "usd"},
                       headers={"Accept": "application/json"})
    if r.status_code != 200:
        return []
    rows = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return [[int(t), o, h, lo, c, v] for t, o, h, lo, c, v in sorted(rows)]


def _f(x, nd=2):
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _holders(onchain: dict, rug: dict | None, tracked: dict | None, curve_pda: str, creator: str | None,
             supply: float, known: dict) -> dict:
    """Largest holders, labelled (bonding curve, pool/program, creator, insider). RugCheck's list first, then
    the user's own RPC, then the wallets the bot saw buying on its live tape."""
    from solders.pubkey import Pubkey

    src, raw = "", []
    if rug and rug.get("topHolders"):
        src = "RugCheck"
        raw = [{"account": h.get("address"), "owner": h.get("owner"), "pct": float(h.get("pct") or 0),
                "insider": bool(h.get("insider"))} for h in rug["topHolders"]]
    elif onchain.get("largest"):
        src = "chain"
        raw = [{"account": h["account"], "owner": h.get("owner"), "pct": h["amount"] / supply * 100 if supply else 0.0}
               for h in onchain["largest"]]
    elif tracked and tracked.get("holders"):
        src = "bot tape (buyers it saw)"
        raw = [{"account": h.get("wallet"), "owner": h.get("wallet"), "pct": float(h.get("pct") or 0),
                "creator": bool(h.get("dev"))} for h in tracked["holders"]]
    rows = []
    for h in raw:
        owner = h.get("owner") or ""
        pct = h["pct"]
        label = "insider" if h.get("insider") else ""
        if owner == curve_pda:
            label = "bonding curve"
        elif owner and owner in known:
            label = known[owner]
        elif h["account"] in known:
            label = known[h["account"]]
        elif owner:
            try:
                if not Pubkey.from_string(owner).is_on_curve():
                    label = "program / pool"
            except Exception:
                pass
        if (creator and owner == creator) or h.get("creator"):
            label = "creator"
        rows.append({"owner": owner or h["account"], "pct": round(pct, 2), "label": label})
    if not rows:
        return {"top": [], "top10_pct": None, "source": ""}
    wallets = [r for r in rows if r["label"] in ("", "creator", "insider")]
    return {"top": rows[:12], "top10_pct": round(sum(r["pct"] for r in wallets[:10]), 1), "source": src}


def _flags(d: dict) -> list[dict]:
    f = []
    add = lambda level, text: f.append({"level": level, "text": text})   # noqa: E731
    if d.get("mint_authority"):
        add("bad", "Mint authority still set: more tokens can be created")
    if d.get("freeze_authority"):
        add("bad", "Freeze authority set: holders' tokens can be frozen")
    if (d.get("curve") or {}).get("mayhem"):
        add("warn", "Mayhem mode: 2B supply, half minted to pump.fun's AI agent, which trades it")
    top10 = (d.get("holders") or {}).get("top10_pct")
    if top10 is not None:
        if top10 >= 50:
            add("bad", f"Top 10 wallets hold {top10:.0f}% of supply")
        elif top10 >= 30:
            add("warn", f"Top 10 wallets hold {top10:.0f}% of supply")
        else:
            add("good", f"Top 10 wallets hold {top10:.0f}% (spread out)")
    rc = d.get("risk") or {}
    if rc.get("rugged"):
        add("bad", "RugCheck marks it as rugged")
    for r in rc.get("risks") or []:
        value = f" ({r['value']})" if r.get("value") else ""
        add("bad" if r.get("level") == "danger" else "warn", f"RugCheck: {r.get('name')}{value}")
    tr = d.get("trading") or {}
    b, s = tr.get("buys_h1"), tr.get("sells_h1")
    if b is not None and s is not None and b + s >= 20:
        if s > b * 1.5:
            add("warn", f"Selling pressure: {s} sells vs {b} buys in the last hour")
        elif b > s * 1.5:
            add("good", f"Buying pressure: {b} buys vs {s} sells in the last hour")
    liq = d.get("liquidity_usd")
    if d.get("stage") == "listed" and liq is not None and liq < 10_000:
        add("warn", f"Thin liquidity (${liq:,.0f}): big sells move the price a lot")
    c = d.get("curve") or {}
    if c and not c.get("complete") and (c.get("progress_pct") or 0) >= 85:
        add("warn", f"{c['progress_pct']:.0f}% up the bonding curve: close to graduation")
    age = d.get("age_s")
    if age is not None and age < 600:
        add("warn", "Less than 10 minutes old")
    t = d.get("tracked") or {}
    if t.get("dev_sold"):
        add("bad", "The creator has sold")
    for g in t.get("failed_gates") or []:
        add("warn", f"Bot gate failed: {g}")
    return f


def _tracked(engine, mint: str) -> dict | None:
    if engine is None:
        return None
    d = engine.token_detail(mint)
    if d is None:
        return None
    s = engine.tokens.get(mint)
    return {"status": d.get("status"), "score": d.get("score"), "p": d.get("p"), "age_s": d.get("age_s"),
            "curve_pct": d.get("curve_pct"), "mcap_usd": d.get("mcap_usd"),
            "buyers": len(s.buyers) if s else None, "dev_sold": bool(s and s.dev_sold),
            "failed_gates": [c["gate"] for c in d.get("checklist") or [] if not c.get("ok")],
            "cluster": d.get("cluster"), "notes": d.get("notes"), "position": d.get("position"),
            "chart": (d.get("chart") or [])[-150:]}


async def lookup(mint: str, engine=None, sol_usd: float | None = None, watch: bool = False) -> dict:
    """Everything we can find on a token, in one dict (JSON-ready). Raises ValueError for a bad address."""
    mint = str(mint or "").strip()
    if not is_mint(mint):
        raise ValueError("that isn't a Solana contract address")
    hit = _cache.get(mint)
    if hit and time.time() - hit[0] < CACHE_S and not watch:
        out = dict(hit[1])
        out["tracked"] = _tracked(engine, mint) or out.get("tracked")
        return out
    sol_usd = sol_usd or (engine.sol_price.usd if engine is not None else None)
    errors: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "meme_trader/0.1"}) as http:
        rpc = _Rpc(http)
        onchain, dex, rug = await asyncio.gather(_onchain(rpc, mint), _dexscreener(http, mint), _rugcheck(http, mint),
                                                 return_exceptions=True)
        if isinstance(onchain, ValueError):
            raise onchain
        for name, v in (("chain", onchain), ("dexscreener", dex), ("rugcheck", rug)):
            if isinstance(v, Exception):
                errors[name] = f"{type(v).__name__}"
        onchain = onchain if isinstance(onchain, dict) else {}
        dex = dex if isinstance(dex, dict) else None
        rug = rug if isinstance(rug, dict) else None
        age_s = (time.time() - dex["pairCreatedAt"] / 1000) if dex and dex.get("pairCreatedAt") else None
        candles = []
        if dex and dex.get("pairAddress"):
            try:
                candles = await _candles(http, dex["pairAddress"], age_s)
            except (httpx.HTTPError, ValueError) as e:
                errors["geckoterminal"] = type(e).__name__
    curve = onchain.get("curve")
    mi = onchain.get("mint") or {}
    meta = (rug or {}).get("tokenMeta") or {}
    base = (dex or {}).get("baseToken") or {}
    info = (dex or {}).get("info") or {}
    supply = mi.get("supply") or (curve or {}).get("supply") or TOTAL_SUPPLY
    stage = ("graduated" if curve and curve.get("complete") else "bonding curve" if curve
             else "listed" if dex else "unknown")
    if dex and stage == "graduated" and (dex.get("dexId") or "") != "pumpfun":
        stage = "graduated · " + str(dex.get("dexId"))
    price_usd = _f((dex or {}).get("priceUsd"), 12)
    if curve and not curve.get("complete") and sol_usd:
        price_usd = curve["price_sol"] * sol_usd
    mcap_usd = (curve["price_sol"] * supply * sol_usd) if curve and not curve.get("complete") and sol_usd else \
        _f((dex or {}).get("marketCap") or (dex or {}).get("fdv"), 0)
    tx, vol, chg = (dex or {}).get("txns") or {}, (dex or {}).get("volume") or {}, (dex or {}).get("priceChange") or {}
    socials = {s.get("type"): s.get("url") for s in info.get("socials") or [] if s.get("type") and s.get("url")}
    if info.get("websites"):
        socials["website"] = (info["websites"][0] or {}).get("url")
    known = {a: (v or {}).get("name") or "known account" for a, v in ((rug or {}).get("knownAccounts") or {}).items()}
    creator = (curve or {}).get("creator") or (rug or {}).get("creator")
    out = {
        "mint": mint, "symbol": base.get("symbol") or meta.get("symbol") or "",
        "name": base.get("name") or meta.get("name") or "",
        "image": info.get("imageUrl") or "", "stage": stage, "creator": creator,
        "price_usd": price_usd, "price_sol": (curve or {}).get("price_sol") if curve and not curve.get("complete")
        else _f((dex or {}).get("priceNative"), 14),
        "mcap_usd": mcap_usd, "liquidity_usd": _f(((dex or {}).get("liquidity") or {}).get("usd"), 0),
        "supply": supply, "mint_authority": mi.get("mint_authority"), "freeze_authority": mi.get("freeze_authority"),
        "token_program": mi.get("program"),
        "curve": None if not curve else {"progress_pct": curve["progress_pct"], "real_sol": round(curve["real_sol"], 3),
                                         "complete": curve["complete"], "price_sol": curve["price_sol"],
                                         "mcap_sol": round(curve["price_sol"] * supply, 2),
                                         "mayhem": curve.get("mayhem", False)},
        "pair": None if not dex else {"dex": dex.get("dexId"), "url": dex.get("url"), "address": dex.get("pairAddress"),
                                      "quote": (dex.get("quoteToken") or {}).get("symbol"), "pairs": dex.get("_pairs")},
        "age_s": round(age_s) if age_s is not None else None,
        "trading": None if not dex else {
            "vol_m5": _f(vol.get("m5"), 0), "vol_h1": _f(vol.get("h1"), 0), "vol_h6": _f(vol.get("h6"), 0),
            "vol_h24": _f(vol.get("h24"), 0),
            "buys_m5": (tx.get("m5") or {}).get("buys"), "sells_m5": (tx.get("m5") or {}).get("sells"),
            "buys_h1": (tx.get("h1") or {}).get("buys"), "sells_h1": (tx.get("h1") or {}).get("sells"),
            "buys_h24": (tx.get("h24") or {}).get("buys"), "sells_h24": (tx.get("h24") or {}).get("sells"),
            "change_m5": _f(chg.get("m5")), "change_h1": _f(chg.get("h1")), "change_h6": _f(chg.get("h6")),
            "change_h24": _f(chg.get("h24"))},
        "holders": None,
        "holder_count": (rug or {}).get("totalHolders") or None,
        "risk": None if not rug else {"score": rug.get("score_normalised"), "rugged": rug.get("rugged"),
                                      "insiders_detected": rug.get("graphInsidersDetected"),
                                      "lp_providers": rug.get("totalLPProviders"),
                                      "risks": [{k: r.get(k) for k in ("name", "level", "value", "description")}
                                                for r in rug.get("risks") or []]},
        "socials": socials,
        "candles": candles, "candles_tf": default_tf(age_s) if candles else None,
        "links": {"pump.fun": f"https://pump.fun/coin/{mint}", "DexScreener": f"https://dexscreener.com/solana/{mint}",
                  "Solscan": f"https://solscan.io/token/{mint}", "RugCheck": f"https://rugcheck.xyz/tokens/{mint}"},
        "errors": errors, "fetched_at": time.time(),
    }
    out["tracked"] = _tracked(engine, mint)
    out["holders"] = _holders(onchain, rug, (engine.token_detail(mint) if engine is not None else None),
                              onchain.get("curve_address", ""), creator, supply, known)
    if watch and engine is not None and out["tracked"] is None and curve and not curve.get("complete"):
        from .tracker import TokenState

        engine.tokens[mint] = TokenState(mint, None, engine.now)
        await engine._watch(mint)
        engine.say("info", f"looking up {out['symbol'] or mint[:6]}: now tracking it", mint)
        out["now_tracking"] = True
    out["flags"] = _flags(out)
    _cache[mint] = (time.time(), out)
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return out


def brief(d: dict) -> dict:
    """The lookup without the bulky parts (candles, chart) - what an AI agent needs to reason about it."""
    out = {k: v for k, v in d.items() if k not in ("candles", "links", "fetched_at", "image")}
    c = d.get("candles") or []
    if c:
        closes = [x[4] for x in c]
        out["candles_summary"] = {"n": len(c), "first_close": closes[0], "last_close": closes[-1],
                                  "high": max(x[2] for x in c), "low": min(x[3] for x in c),
                                  "from": time.strftime("%H:%M", time.gmtime(c[0][0])),
                                  "to": time.strftime("%H:%M", time.gmtime(c[-1][0]))}
    t = dict(out.get("tracked") or {})
    if t.get("chart"):
        ch = t.pop("chart")
        t["price_points_sol"] = ch[:: max(1, len(ch) // 20)]
    out["tracked"] = t or None
    return out


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(asyncio.run(lookup(sys.argv[1], sol_usd=150.0)), indent=1, default=str)[:6000])
