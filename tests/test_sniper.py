import asyncio

import pytest

from meme_trader import config
from meme_trader.sniper.curve import CURVE_TOKENS, Curve
from meme_trader.sniper.engine import Engine, reason_key
from meme_trader.sniper.events import Launch, Trade, dumps, loads
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import TRADE_EVENT, PumpPortalFeed, SolanaTradeFeed, SyntheticFeed
from meme_trader.sniper.signals import extract_mints
from meme_trader.sniper.strategy import SniperPosition, evaluate_entry, evaluate_exit
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
E, X = P.sniper.entry, P.sniper.exit
MINT = "7" * 40 + "pump"
DEV = "D" * 44


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    monkeypatch.setattr("meme_trader.journal.DATA", tmp_path)
    monkeypatch.setattr("meme_trader.sniper.engine.DATA", tmp_path)


def test_curve_math_roundtrip_and_graduation():
    c = Curve()
    assert c.progress == 0 and c.market_cap_sol == pytest.approx(27.96, rel=1e-3)
    tok = c.quote_buy(1.0, 0)
    c.apply(1.0, -tok)
    assert c.quote_sell(tok, 0) == pytest.approx(1.0)          # no-fee round trip is lossless
    c2 = Curve()
    c2.apply(85.0, -CURVE_TOKENS)                                 # ~85 SOL sells the curve out
    assert c2.progress == pytest.approx(1.0) and c2.real_sol == pytest.approx(85, abs=0.1)


def make_state(dev_pct=2.0, bundle_buys=0, organic=30, dev_sells=False) -> tuple[TokenState, float]:
    t0 = 1000.0
    c = Curve()
    dev_tok = 1e9 * dev_pct / 100
    sol = c.v_sol * c.v_tokens / (c.v_tokens - dev_tok) - c.v_sol
    c.apply(sol, -dev_tok)
    s = TokenState(MINT, Launch(MINT, t0, DEV, symbol="TEST", dev_buy_tokens=dev_tok, v_sol=c.v_sol,
                                v_tokens=c.v_tokens, twitter="x"), t0)
    s.on_launch(s.launch)

    def buy(ts, who, amt):
        tok = c.quote_buy(amt, 0)
        c.apply(amt, -tok)
        s.on_trade(Trade(MINT, ts, who, "buy", amt, tok, c.v_sol, c.v_tokens), E.bundle_window_s)

    for i in range(bundle_buys):
        buy(t0 + 0.5, f"bundler{i}", 3.0)
    for i in range(organic):
        buy(t0 + 3 + i * 0.6, f"w{i}", 0.25)
    if dev_sells:
        tok = s.holders[DEV]
        gross = c.quote_sell(tok, 0)
        c.apply(-gross, tok)
        s.on_trade(Trade(MINT, t0 + 22, DEV, "sell", gross, tok, c.v_sol, c.v_tokens), E.bundle_window_s)
    return s, t0 + 3 + organic * 0.6


CTX = {"creator_launches": 1, "symbol_dupes": 0, "social_weight": 0}


def test_entry_on_organic_launch():
    s, now = make_state(organic=40)            # 10 SOL volume -> 0.125 SOL fees paid
    d = evaluate_entry(s, now, E, CTX)
    assert d.action == "enter", d.notes


@pytest.mark.parametrize("kw,ctx,needle", [
    ({"dev_sells": True}, {}, "dev sold"),
    ({"dev_pct": 15}, {}, "dev bought"),
    ({"bundle_buys": 8}, {}, "bundle"),
    ({}, {"creator_launches": 5}, "serial deployer"),
    ({}, {"symbol_dupes": 9}, "copycat"),
])
def test_entry_hard_rejects(kw, ctx, needle):
    s, now = make_state(**kw)
    d = evaluate_entry(s, now, E, {**CTX, **ctx})
    assert d.action == "reject" and needle in d.notes[0]


def test_entry_waits_when_too_early():
    s, _ = make_state(organic=3)
    assert evaluate_entry(s, s.created_ts + 5, E, CTX).action == "wait"


def pos_at(s: TokenState, now: float) -> SniperPosition:
    p = s.curve.price
    return SniperPosition(MINT, "TEST", now, p, 1e6, 1e6, 0.05, 0.05, 80, peak_price=p, exits=[])


def push_price(s: TokenState, now: float, mult: float, side="buy", sol=0.1):
    """Move the curve so price = mult x current price."""
    c = s.curve
    k = c.v_sol * c.v_tokens
    target = c.price * mult
    v_sol = (k * target) ** 0.5
    s.on_trade(Trade(MINT, now, "mover", side, sol, 0, v_sol, k / v_sol), E.bundle_window_s)


def test_exit_initials_then_trailing_stop():
    s, now = make_state()
    pos = pos_at(s, now)
    push_price(s, now + 5, 2.2)
    frac, why = evaluate_exit(pos, s, now + 5, X, 1.75)
    assert why.startswith("initials") and frac == pytest.approx((1.0175) / 2.2, rel=1e-3)
    pos.initials_taken = True
    push_price(s, now + 10, 2.0)                 # 4.4x peak
    assert evaluate_exit(pos, s, now + 10, X, 1.75) is None
    push_price(s, now + 15, 0.75)                # -25% from a +340% peak -> 25% trail tier
    frac, why = evaluate_exit(pos, s, now + 15, X, 1.75)
    assert frac == 1.0 and "trailing" in why


def test_exit_stop_loss_and_dev_sell():
    s, now = make_state()
    pos = pos_at(s, now)
    push_price(s, now + 5, 0.6, side="sell")
    assert "stop loss" in evaluate_exit(pos, s, now + 5, X, 1.75)[1]
    s2, now2 = make_state()
    pos2 = pos_at(s2, now2)
    s2.dev_sold = 1
    assert evaluate_exit(pos2, s2, now2, X, 1.75)[1] == "dev sold"


def test_extract_mints():
    text = f"🚀 new gem CA: {MINT} chart https://dexscreener.com/solana/{MINT} ape now"
    assert extract_mints(text) == [MINT]
    assert extract_mints("no address here 0xabc") == []


def test_pumpportal_parse_and_event_roundtrip():
    e = PumpPortalFeed.parse({"txType": "create", "mint": MINT, "traderPublicKey": DEV, "initialBuy": 5e7,
                              "solAmount": 1.5, "vSolInBondingCurve": 31.5, "vTokensInBondingCurve": 1.02e9,
                              "name": "Test", "symbol": "TEST", "uri": "u"}, 1.0)
    assert isinstance(e, Launch) and e.dev_buy_tokens == 5e7
    t = PumpPortalFeed.parse({"txType": "sell", "mint": MINT, "traderPublicKey": "w", "solAmount": .1,
                              "tokenAmount": 1e6, "vSolInBondingCurve": 31, "vTokensInBondingCurve": 1.03e9}, 2.0)
    assert isinstance(t, Trade) and t.side == "sell"
    assert loads(dumps(t)) == t
    assert PumpPortalFeed.parse({"message": "subscribed"}, 0) is None


# one real pump.fun TradeEvent log line (mainnet tx 5YNTY9nP..., captured 2026-10-03)
REAL_TRADE_LOG = (
    "Program data: vdt/007mYe4gLmFMZa4UdjssR7Bu/uz3qrxDY3NiSvHZox0uJNL9j6cvwAAAAAAAUOSK0lEAAAABootf0mq0eaapzGy/awsj"
    "62GIWjceASCsqRO+7z0TinhukcBqAAAAAMCwbWgKAAAAUgQylRvNAwCvPEUFAAAAAFJsH0mKzgIA6JMUH7GOnxV02BDheOGeMGBOMXWqLkoy"
    "38hgByfRBwkAAAAAAAAAAAAAAAAAAAAAkuUuwp329ugiNSKrQGN+N08f9TL/fPIO+zm+kQTAZRwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAGJ1eQEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAKcvwAAAAAAAwLBtaAoAAACvPEUFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==")


def _trade_log(mint: bytes, sol: int, tokens: int, buy: bool, user: bytes, v_sol: int, v_tokens: int) -> str:
    import base64
    import struct
    raw = (TRADE_EVENT + mint + struct.pack("<QQ?", sol, tokens, buy) + user
           + struct.pack("<qQQQQ", 0, v_sol, v_tokens, 0, 0))
    return "Program data: " + base64.b64encode(raw + bytes(250)).decode()


def test_solana_logs_parse_real_trade():
    t, = SolanaTradeFeed.parse_logs({"signature": "sig", "err": None,
                                     "logs": ["Program log: Instruction: Buy", REAL_TRADE_LOG]}, 5.0)
    assert t.mint == "3Ad4mzd7W1qSZ9pdevHudtyGKuLMJucwSciz1LbVpump"
    assert t.trader == "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
    assert (t.side, t.sol, t.tokens) == ("buy", 0.012595111, 351424.668752)
    assert (t.v_sol, t.v_tokens, t.pool, t.new_balance, t.ts) == (44.701692096, 1069943281.02613, "pump", -1.0, 5.0)


def test_solana_logs_parse_skips_noise_and_failed_tx():
    from solders.pubkey import Pubkey
    mint, user = bytes(Pubkey.from_string(MINT[:32] + "1" * 12)), bytes(range(32))
    sell = _trade_log(mint, 250_000_000, 7_000_000_000_000, False, user, 31_000_000_000, 1_030_000_000_000_000)
    logs = ["Program data: !!not base64!", "Program data: " + "QUJD" * 30, sell]   # junk, other program's event
    t, = SolanaTradeFeed.parse_logs({"err": None, "logs": logs}, 1.0)
    assert (t.side, t.sol, t.tokens, t.v_sol, t.v_tokens) == ("sell", 0.25, 7_000_000.0, 31.0, 1_030_000_000.0)
    assert t.trader == str(Pubkey.from_bytes(user))
    assert SolanaTradeFeed.parse_logs({"err": {"InstructionError": [2, "Custom"]}, "logs": logs}, 1.0) == []


def test_solana_feed_watch_sets_and_degraded():
    f = SolanaTradeFeed("wss://example.org/?api-key=secret")
    asyncio.run(f.watch([MINT]))
    asyncio.run(f.watch_accounts(["w"]))
    asyncio.run(f.unwatch([MINT]))
    assert f.watched == set() and f.accounts == {"w"}
    assert f.degraded                       # no trade notification yet: entries stay paused
    f.trades_up = True
    assert not f.degraded and f.host == "example.org"


def test_solana_feed_replays_early_trades_without_the_dev_buy():
    f = SolanaTradeFeed()
    f._q = asyncio.Queue()
    f.dev_buys[MINT] = (DEV, 5e7)
    dev = Trade(MINT, 1.0, DEV, "buy", 1.5, 5e7, 31.5, 1.02e9)
    sniper = Trade(MINT, 1.2, "s", "buy", 0.5, 1e7, 32.0, 1.01e9)
    other = Trade("x" * 44, 1.3, "s", "buy", 0.1, 1e6, 30.1, 1.07e9)
    for t in (dev, sniper, other):
        f._hold(t)
    asyncio.run(f.watch([MINT]))
    got = f._q.get_nowait()
    assert f._q.empty() and got.trader == "s" and got.ts > sniper.ts      # dev buy dropped, restamped to now
    assert MINT not in f.early and "x" * 44 in f.early
    f._hold(Trade("y" * 44, 1.3 + f.EARLY_S + 1, "s", "buy", 0.1, 1e6, 30.1, 1.07e9))
    assert "x" * 44 not in f.early                                         # expired

def test_reason_key():
    assert reason_key("dev bought 7.1% > 6%") == "dev bought"
    assert reason_key("serial deployer (3 launches/24h)") == "serial deployer"


def test_engine_synthetic_backtest_accounting():
    feed = SyntheticFeed(seed=5, speed=0, launches=300, start_ts=1_780_000_000)
    eng = Engine(P, feed, PaperExecutor(P.sniper.execution), mode="backtest", log_to_journal=False)
    asyncio.run(eng.run())

    async def flush():
        for m in list(eng.positions):
            await eng.sell_now(m)
    asyncio.run(flush())
    s = eng.summary()
    assert s["launches"] == 300 and s["entries"] > 0
    assert not eng.positions
    # cash conservation: final SOL = start + realized P&L
    assert eng.book.sol == pytest.approx(eng.book.start_sol + s["realized_pnl_sol"], abs=1e-9)
    # the gates should keep us out of nearly all bundled rugs
    rugs = [c for c in eng.book.closed if feed.archetype[c["mint"]] == "bundle_rug"]
    assert len(rugs) <= 0.05 * sum(1 for a in feed.archetype.values() if a == "bundle_rug")
    snap = eng.snapshot()
    assert {"positions", "watching", "summary", "rejects"} <= set(snap)
