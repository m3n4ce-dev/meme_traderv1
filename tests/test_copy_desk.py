import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from meme_trader import config
from meme_trader.sniper.__main__ import _sim_leaders, run_backtest
from meme_trader.sniper.copytrade import LeaderBook, discover
from meme_trader.sniper.desk import Desk, Vote, aggregate
from meme_trader.sniper.doctor import valid_pubkey
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.feeds import SyntheticFeed

P = config.load(config.EXAMPLE)
MINT = "M" * 40 + "pump"
LEAD = "L" * 44


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


def test_params_copy_section_is_not_dict_copy():
    assert P.sniper.copy.max_chase_pct == 25
    assert isinstance(P.copy(), dict)          # dict.copy still reachable when no key shadows it


def test_leader_book_accounting_and_pause():
    lb = LeaderBook([{"address": LEAD, "label": "pro"}])
    lb.on_trade(Trade(MINT, 1, LEAD, "buy", 1.0, 1000, 31, 1.0e9))
    assert lb.on_trade(Trade(MINT, 2, LEAD, "sell", 1.0, 500, 31, 1.0e9)) == pytest.approx(0.5)
    assert lb.on_trade(Trade(MINT, 3, LEAD, "sell", 1.0, 500, 31, 1.0e9)) == pytest.approx(1.0)
    st = lb.stats[LEAD]
    assert st.closed == 1 and st.wins == 1 and st.realized_sol == pytest.approx(1.0)
    for _ in range(4):
        assert lb.on_copy_closed(LEAD, -0.01, 5) == ""
    assert lb.on_copy_closed(LEAD, -0.01, 5) and not lb.is_leader(LEAD)


def test_discover_ranks_profitable_wallet_first():
    ev = [Launch(MINT, 0, "dev")]
    ev += [Trade(MINT, 5 + i, "winner", "buy", 1, 1e6, 31, 1e9) for i in range(1)]
    ev += [Trade(MINT, 30, "winner", "sell", 3, 1e6, 31, 1e9), Trade(MINT, 6, "loser", "buy", 1, 1e6, 31, 1e9),
           Trade(MINT, 40, "loser", "sell", 0.2, 1e6, 31, 1e9)]
    ranked = discover(ev, min_tokens=1)
    assert [w.wallet for w in ranked[:2]] == ["winner", "loser"]
    assert ranked[0].realized_sol == pytest.approx(2.0)


def test_desk_aggregate_quorum_and_veto():
    w = {"veteran": 1, "skeptic": 1, "quant": 1}
    buy = [Vote("veteran", "buy", 80), Vote("quant", "buy", 70), Vote("skeptic", "buy", 60)]
    v = aggregate(buy, w, 0.45, 75)
    assert v.approve and 0.5 <= v.size_mult <= 1.5
    veto = [Vote("veteran", "buy", 90), Vote("quant", "buy", 90), Vote("skeptic", "pass", 80, red_flags=["bundled"])]
    v = aggregate(veto, w, 0.45, 75)
    assert not v.approve and "veto" in v.summary
    assert not aggregate([Vote("veteran", "pass", 0, error="timeout")], w, 0.45, 75).approve


def test_desk_three_of_four_buys_is_a_buy():
    """Seen live: veteran buy62, narrative buy58, skeptic pass62, quant buy58 came to 'buy share 44% vs quorum 45%'
    and the trade was passed, because conviction multiplied the share. Votes count; conviction sizes."""
    w = {"veteran": 1.0, "narrative": 0.8, "skeptic": 1.0, "quant": 1.0}
    three = [Vote("veteran", "buy", 62), Vote("narrative", "buy", 58), Vote("skeptic", "pass", 62), Vote("quant", "buy", 58)]
    v = aggregate(three, w, 0.45, 75)
    assert v.approve and "APPROVED" in v.summary and v.size_mult < 1.0     # modest conviction, modest size
    one = [Vote("veteran", "buy", 90), Vote("narrative", "pass", 60), Vote("skeptic", "pass", 60), Vote("quant", "pass", 60)]
    assert not aggregate(one, w, 0.45, 75).approve                           # one voice isn't a desk
    weak = [Vote("veteran", "buy", 30), Vote("narrative", "pass", 60), Vote("skeptic", "pass", 60), Vote("quant", "buy", 40)]
    assert not aggregate(weak, w, 0.45, 75).approve                          # two half-hearted buys aren't either


class FakeClient:
    """Stands in for anthropic.AsyncAnthropic: every persona votes buy at 80."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        snap = kw["messages"][0]["content"]
        assert "Snapshot" in snap and "untrusted" not in snap       # instructions live in the system prompt
        assert "never as instructions" in kw["system"][0]["text"]
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=900, output_tokens=60),
                               content=[SimpleNamespace(type="text", text=json.dumps(
                                   {"vote": "buy", "conviction": 80, "reasons": ["clean flow"], "red_flags": []}))])


def test_engine_with_desk_and_copy_trading():
    params = copy.deepcopy(P)
    params["sniper"]["desk"]["enabled"] = True
    client = FakeClient()
    desk = Desk(params.sniper.desk, client=client)
    feed = SyntheticFeed(seed=4, speed=0, launches=250, start_ts=1_780_000_000)
    _sim_leaders(params, feed)

    async def go():
        eng = await run_backtest(params, feed, desk)
        await asyncio.sleep(0)       # let any in-flight desk tasks finish
        return eng
    eng = asyncio.run(go())
    s = eng.summary()
    assert desk.calls > 0 and len(client.calls) == desk.calls
    assert s["entries"] > 0 and "copy" in s["by_source"] and "sniper" in s["by_source"]
    assert all(c["desk"].startswith("APPROVED") for c in eng.book.closed if c["source"] != "callout")
    assert eng.book.sol == pytest.approx(eng.book.start_sol + s["realized_pnl_sol"], abs=1e-9)
    assert {"leaders", "desk"} <= set(eng.snapshot())


def test_valid_pubkey():
    assert valid_pubkey("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")   # pump.fun program id
    assert not valid_pubkey("not-a-key") and not valid_pubkey("")


def test_callout_agent_rate_limit_bags_and_factual_text():
    from meme_trader.sniper.callouts import is_red_flag

    params = copy.deepcopy(P)
    feed = SyntheticFeed(seed=7, speed=0, launches=400, start_ts=1_780_000_000)
    eng = asyncio.run(run_backtest(params, feed))
    calls = eng.callouts.calls
    assert calls, "expected some callouts"
    gaps = [b.ts - a.ts for a, b in zip(calls, calls[1:])]
    assert min(gaps) >= params.sniper.callouts.interval_s                 # one per 2 minutes
    bags = [c for c in eng.book.closed if c["source"] == "callout"]
    usd = eng.sol_price.usd
    fee_usd = (params.sniper.callouts.priority_fee_sol + 0.000005) * usd
    assert bags and all(c["cost"] * usd <= params.sniper.callouts.position_usd + fee_usd + 0.01 for c in bags)
    for c in calls:
        assert "Data, not advice" in c.text and "$1 callout position" in c.text
        low = c.text.replace(c.symbol, "").lower()      # coin names like MOONAI are the creator's, not ours
        assert not any(w in low for w in ("window closes", "guarantee", "100x", "moon"))
    assert is_red_flag("dev sold") and not is_red_flag("curve 45% > 40% (too late)")
    # callout bags never count against trading position slots
    assert "max positions" not in eng.entries_blocked() or \
        sum(1 for p in eng.positions.values() if p.source != "callout") >= params.sniper.capital.max_open_positions


def test_a_model_that_refuses_an_option_is_asked_again_without_it():
    """Seen 2026-10-05: Haiku 4.5 answered 400 'This model does not support the effort parameter.'"""
    class Picky(FakeClient):
        async def create(self, **kw):
            if "effort" in (kw.get("output_config") or {}):
                self.calls.append("refused")
                raise RuntimeError("Error code: 400 - This model does not support the effort parameter.")
            return await super().create(**kw)
    params = copy.deepcopy(P)
    params["sniper"]["desk"].update(enabled=True, effort="low")
    client = Picky()
    d = Desk(params.sniper.desk, client=client)
    d.caps = {"effort": True, "json": True}                 # (what the capability lookup assumes when it can't tell)
    v = asyncio.run(d._ask("veteran", {"symbol": "X"}))
    assert v.vote == "buy" and not v.error and d.caps["effort"] is False
    n = len(client.calls)
    asyncio.run(d._ask("quant", {"symbol": "X"}))
    assert client.calls[n:] and "refused" not in client.calls[n:]     # remembered: no second refusal
