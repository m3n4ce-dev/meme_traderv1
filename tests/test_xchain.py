"""Paper trading on other chains (sniper/xchain.py): screening, safety checks, fills and the shared book."""
import asyncio
import time

from meme_trader.sniper.xchain import DEFAULTS, XChain, fee_pct, impact_pct, judge_honeypot, judge_solana_mint, screen
from tests.test_manual import market

GOOD = {"chain": "base", "chain_name": "Base", "name": "FROG / WETH 1%", "symbol": "FROG", "pool": "0xPOOL", "token": "0xTOKEN",
        "mcap_usd": 900_000, "fdv_usd": 900_000, "liq_usd": 80_000, "vol_h1": 60_000, "chg_m5": 2.0, "chg_h1": 30.0,
        "buys_h1": 300, "sells_h1": 200, "age_s": 5 * 3600, "dex_id": "uniswap-v3-base", "dex": "https://dexscreener.com/base/0xpool"}
# honeypot.is answers as seen 2026-10-05
HP_OK = {"honeypotResult": {"isHoneypot": False}, "simulationResult": {"buyTax": 0, "sellTax": 0}, "summary": {"risk": "low", "riskLevel": 1}}
HP_TRAP = {"honeypotResult": {"isHoneypot": True}, "simulationResult": {"buyTax": 0, "sellTax": 100}, "summary": {"risk": "honeypot", "riskLevel": 100}}
HP_UNKNOWN = {"summary": {"risk": "unknown", "flags": [{"flag": "high_fail_rate"}]}}


def test_screen_takes_young_rising_liquid_coins_only():
    cfg = dict(DEFAULTS)
    assert screen(GOOD, cfg) == ""
    assert "major" in screen({**GOOD, "symbol": "WETH"}, cfg)
    assert "pump.fun" in screen({**GOOD, "chain": "solana", "dex_id": "pump-fun"}, cfg)
    assert screen({**GOOD, "age_s": 300}, cfg) == "too new"
    assert "older" in screen({**GOOD, "age_s": 10 * 86400}, cfg)
    assert "liquidity" in screen({**GOOD, "liq_usd": 5_000}, cfg)
    assert "market cap" in screen({**GOOD, "mcap_usd": 90_000_000}, cfg)
    assert "buys/sells" in screen({**GOOD, "buys_h1": 100, "sells_h1": 200}, cfg)
    assert "1h move" in screen({**GOOD, "chg_h1": 400}, cfg)
    assert "falling" in screen({**GOOD, "chg_m5": -3}, cfg)
    assert fee_pct(GOOD) == 1.0 and fee_pct({"name": "X / WBNB", "dex_id": "pancakeswap_v2"}) == 0.25
    assert abs(impact_pct(30, 60_000) - 0.1) < 1e-9


def test_only_coins_that_can_be_sold_again_pass():
    assert judge_honeypot(HP_OK, 5) == (True, "", {"buy_tax": 0.0, "sell_tax": 0.0})
    assert judge_honeypot(HP_TRAP, 5)[1].startswith("honeypot")
    assert not judge_honeypot(HP_UNKNOWN, 5)[0] and not judge_honeypot(None, 5)[0]      # not checked = not traded
    taxed = {**HP_OK, "simulationResult": {"buyTax": 0, "sellTax": 8}}
    assert "tax" in judge_honeypot(taxed, 5)[1]

    from meme_trader.sniper.xchain import judge_goplus
    clean = {k: "0" for k in ("is_honeypot", "cannot_sell_all", "is_blacklisted", "transfer_pausable", "owner_change_balance",
                              "hidden_owner", "slippage_modifiable", "is_proxy")} | {"is_open_source": "1", "buy_tax": "0", "sell_tax": "0.03"}
    gp = lambda **kw: {"code": 1, "result": {"0xabc": {**clean, **kw}}}                 # as seen 2026-10-05 for CNPY on BNB Chain
    assert judge_goplus(gp(), "0xABC", 5) == (True, "", {"buy_tax": 0.0, "sell_tax": 3.0})
    assert "is proxy" in judge_goplus(gp(is_proxy="1"), "0xabc", 5)[1]              # upgradeable: can become a honeypot later
    assert "unknown" in judge_goplus(gp(is_honeypot=None), "0xabc", 5)[1]
    assert judge_goplus(gp(cannot_sell_all=None), "0xabc", 5)[0]                    # (a blank on the rest is fine, a "1" isn't)
    assert "cannot sell all" in judge_goplus(gp(cannot_sell_all="1"), "0xabc", 5)[1]
    assert "open source" in judge_goplus(gp(is_open_source="0"), "0xabc", 5)[1]
    assert "tax" in judge_goplus(gp(sell_tax="0.1"), "0xabc", 5)[1] and not judge_goplus({"result": {}}, "0xabc", 5)[0]

    def mint(**info):
        return {"result": {"value": {"data": {"parsed": {"info": info}}}}}
    assert judge_solana_mint(mint(freezeAuthority=None, mintAuthority=None), 5)[0]
    assert "freeze" in judge_solana_mint(mint(freezeAuthority="X", mintAuthority=None), 5)[1]
    assert "mint" in judge_solana_mint(mint(freezeAuthority=None, mintAuthority="X"), 5)[1]
    fee = mint(freezeAuthority=None, mintAuthority=None,
               extensions=[{"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 1000}}}])
    assert "transfer fee 10%" in judge_solana_mint(fee, 5)[1]


def _trader(monkeypatch, price=0.001, liq=80_000, safe=(True, "", {"buy_tax": 0.0, "sell_tax": 2.0})):
    e = market(launches=2)
    x = e.xchain
    state = {"price": price, "liq": liq}

    async def chains_data():
        return {"trending": {"base": [GOOD]}, "new": {}}

    async def quotes(chain, pools):
        return {x._k(chain, p): {"price": state["price"], "liq": state["liq"], "mcap": 900_000, "buys_m5": 9, "sells_m5": 4} for p in pools}

    async def safety(chain, token):
        return safe

    async def real_quote(chain, token_in, token_out, amount_raw):          # a stand-in router: 1% worse than the pool
        if state.get("route") is False:
            return "no route", None
        k = state["route"] if isinstance(state.get("route"), float) else 0.99      # the route's share of the pool price
        if token_out == "0xTOKEN":                                          # buy: dollars in, coins (18 decimals) out
            usd = amount_raw / 1e6
            return "ok", {"out_raw": int(usd * k / state["price"] * 1e18), "out_usd": usd * k, "gas_usd": 0.01}
        return "ok", {"out_raw": int(amount_raw / 1e18 * state["price"] * k * 1e6), "out_usd": None, "gas_usd": 0.01}
    monkeypatch.setattr(x, "_chains_data", chains_data)
    monkeypatch.setattr(x, "real_quote", real_quote)
    monkeypatch.setattr(x, "quotes", quotes)
    monkeypatch.setattr(x, "safety", safety)
    monkeypatch.setitem(e.p.sniper if "sniper" in e.p else e.p, "xchain", {**DEFAULTS, "chains": ["base"], "size_usd": 30})
    return e, x, state


def test_a_paper_round_trip_pays_real_costs_and_lands_in_the_shared_book(monkeypatch):
    e, x, state = _trader(monkeypatch)
    sol0, eq0, day0 = e.book.sol, e.equity(), e.book.day_pnl
    now = time.time()
    asyncio.run(x.step(now))
    assert len(x.positions) == 1
    p = next(iter(x.positions.values()))
    sol_usd = e.sol_price.usd
    assert abs((sol0 - e.book.sol) - (30 + 0.02) / sol_usd) < 1e-9             # $30 plus Base gas, out of the paper balance
    assert e.equity() < eq0                                                       # fees, impact and slippage: down a little at once
    assert p["tokens"] < 30 / 0.001                                               # fewer coins than a free swap would give
    state["price"] = 0.001 * 0.8                                                  # -20%: the stop
    asyncio.run(x.step(now + 20))
    assert not x.positions
    row = e.book.closed[-1]
    assert row["source"] == "chains" and row["chain"] == "base" and row["exit"].startswith("stop loss")
    from meme_trader.sniper.report import row_mode
    assert row["mode"] == "paper" and row["session"] and row_mode({"source": "chains"}) == "paper"   # counted in paper results
    assert -30 < row["pnl_pct"] < -22 and row["pnl_usd"] < -6                   # -20% move plus a 2% sell tax and costs
    assert abs((e.book.day_pnl - day0) - row["pnl"]) < 1e-9                      # counts toward the daily loss limit
    assert abs(e.book.sol - (sol0 + row["pnl"])) < 1e-9
    asyncio.run(x.step(now + 200))                                                # the same coin isn't bought straight back
    assert not x.positions and any(r["why"] == "traded lately" for r in x.scan)


def test_take_profit_then_trailing_stop(monkeypatch):
    e, x, state = _trader(monkeypatch)
    now = time.time()
    asyncio.run(x.step(now))
    state["price"] = 0.0015                                                       # +50%: half comes off
    asyncio.run(x.step(now + 10))
    p = next(iter(x.positions.values()))
    assert p["tp_taken"] and abs(p["tokens"] / p["tokens0"] - 0.5) < 1e-9
    state["price"] = 0.0020                                                       # new peak
    asyncio.run(x.step(now + 20))
    state["price"] = 0.0015                                                       # 25% under the peak: the rest goes
    asyncio.run(x.step(now + 30))
    assert not x.positions
    row = e.book.closed[-1]
    assert row["exit"].startswith("trailing stop") and row["pnl"] > 0 and row["initials"]


def test_kill_switch_pause_limits_and_live_mode(monkeypatch):
    e, x, state = _trader(monkeypatch)
    now = time.time()
    e.paused = True
    asyncio.run(x.step(now))
    assert not x.positions and x.block(0.1) == "paused"
    e.paused = False
    asyncio.run(x.step(now + 100))
    assert len(x.positions) == 1
    e.book.halted = "drawdown 40%"
    asyncio.run(x.step(now + 110))
    assert not x.positions and e.book.closed[-1]["exit"] == "kill switch"
    e.book.halted = ""
    e.mode = "live"
    assert not x.active and x.view()["paper_only"]                              # no EVM wallet: never opens anything live


def test_honeypots_are_skipped_and_positions_survive_a_restart(monkeypatch, tmp_path):
    e, x, state = _trader(monkeypatch, safe=(False, "honeypot (can't sell)", {}))
    asyncio.run(x.step(time.time()))
    assert not x.positions and x.scan[0]["why"] == "honeypot (can't sell)"
    e, x, state = _trader(monkeypatch)
    x.path = tmp_path / "xchain.json"
    asyncio.run(x.step(time.time()))
    assert len(x.positions) == 1
    y = XChain(e, tmp_path / "xchain.json")
    assert y.positions.keys() == x.positions.keys() and abs(y.value_sol() - x.value_sol()) < 1e-12
    assert (tmp_path / "xchain").is_dir()                                         # scans kept for research


def test_real_router_prices_ride_along_and_no_route_means_no_buy(monkeypatch):
    from meme_trader.sniper.xchain import parse_jupiter, parse_kyber
    ok = {"code": 0, "data": {"routeSummary": {"amountOut": "131099115858099691323392", "amountOutUsd": "29.89", "gasUsd": "0.0133"}}}
    assert parse_kyber(200, ok) == ("ok", {"out_raw": 131099115858099691323392, "out_usd": 29.89, "gas_usd": 0.0133})   # (seen 2026-10-05)
    assert parse_kyber(400, {"code": 4008, "message": "route not found"})[0] == "no route"
    assert parse_kyber(503, {})[0] == "error" and parse_kyber(429, None)[0] == "error"
    assert parse_jupiter(200, {"outAmount": "11945000"})[1]["out_raw"] == 11945000
    assert parse_jupiter(400, {"error": "Could not find any route"})[0] == "no route"

    e, x, state = _trader(monkeypatch)
    state["route"] = False
    asyncio.run(x.step(time.time()))
    assert not x.positions and "no real swap route" in x.scan[0]["why"]
    state["route"] = True
    x.last_scan = 0
    asyncio.run(x.step(time.time()))
    p = next(iter(x.positions.values()))
    assert p["real"]["buy_gap_pct"] is not None and abs(p["real"]["buy_gap_pct"]) < 2     # the router agrees with the paper fill
    assert p["dec"] == 18                                                         # decimals, from the router's dollar value
    state["price"] = 0.0011
    asyncio.run(x.refresh(time.time() + 5))
    assert p["px_src"] == "router" and abs(p["last"] / 0.0011 - 1) < 0.02       # held coins are priced by the router
    state["price"] = 0.0008
    asyncio.run(x.step(time.time() + 20))
    row = e.book.closed[-1]
    assert row["real"]["complete"] and row["real"]["pnl_usd"] < 0 and abs(row["real"]["pnl_usd"] - row["pnl_usd"]) < 2
    r = x.readiness()
    assert not r["ready"] and r["trades"] == 1 and r["checks"][0]["value"] == "1"
    names = [c["name"] for c in r["checks"]]
    assert any("luck" in n for n in names) and any("real router prices" in n for n in names)


def test_the_real_money_checklist_passes_only_on_a_real_edge(monkeypatch):
    e, x, state = _trader(monkeypatch)
    win = {"source": "chains", "pnl": 0.01, "pnl_usd": 3.0, "real": {"pnl_usd": 2.8, "complete": True, "buy_gap_pct": 0.5, "sell_gap_pct": -0.8}}
    loss = {**win, "pnl_usd": -2.0, "real": {**win["real"], "pnl_usd": -2.1}}
    e.book.closed += [dict(win) for _ in range(40)] + [dict(loss) for _ in range(20)]
    r = x.readiness()
    assert r["ready"] and r["passed"] == 6
    x._ready = None
    e.book.closed[:] = [dict(win) for _ in range(3)] + [dict(loss) for _ in range(57)]
    r = x.readiness()
    assert not r["ready"] and not r["checks"][1]["ok"] and not r["checks"][2]["ok"]


def test_one_sale_at_a_time_per_coin(monkeypatch):
    e, x, state = _trader(monkeypatch)
    asyncio.run(x.step(time.time()))
    key = next(iter(x.positions))
    gate = asyncio.Event()
    real = x.real_quote

    async def slow_quote(*a):
        await gate.wait()
        return await real(*a)
    monkeypatch.setattr(x, "real_quote", slow_quote)

    async def go():
        t1 = asyncio.create_task(x.sell(key, 0.5, "take profit +40%", time.time()))
        await asyncio.sleep(0)
        assert "already going through" in await x.sell_now(key)          # your click while the bot's sale waits
        gate.set()
        await t1
    asyncio.run(go())
    p = x.positions[key]
    assert abs(p["tokens"] / p["tokens0"] - 0.5) < 1e-9 and p["real"]["raw_left"] > 0



def test_paper_fills_are_never_better_than_the_real_route(monkeypatch):
    """Seen 2026-10-05: a thin BNB Chain route paid 17% less than the paper model on the buy (ANIMA)."""
    e, x, state = _trader(monkeypatch)
    state["route"] = 0.83                                                  # the router gives 17% less than the pool price
    now = time.time()
    asyncio.run(x.step(now))
    p = next(iter(x.positions.values()))
    assert p.get("fill") == "router" and p["real"]["buy_gap_pct"] < -10    # fewer coins: the route's, not the model's
    state["price"] = 0.0008
    asyncio.run(x.step(now + 20))
    row = e.book.closed[-1]
    assert abs(row["pnl_usd"] - row["real"]["pnl_usd"]) < 0.5              # paper books what a real wallet would get
    state["route"] = 1.2                                                   # a route better than the model ...
    x.last_scan = 0
    x.cooldown.clear()
    asyncio.run(x.step(now + 60))
    q = next(iter(x.positions.values()))
    assert q.get("fill") != "router"                                       # ... doesn't make the paper fill better
