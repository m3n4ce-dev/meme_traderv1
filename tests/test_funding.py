import asyncio
import copy
from collections import Counter

import pytest

from meme_trader import config
from meme_trader.sniper.curve import Curve
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Funding, Launch, Trade
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.funding import cluster_report, cohort, first_sol_source
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
MINT, DEV = "F" * 40 + "pump", "D" * 44


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


def test_first_sol_source_parses_transfer_and_create_account():
    tx = {"transaction": {"message": {"instructions": [
        {"parsed": {"type": "transfer", "info": {"source": "SRC", "destination": "W", "lamports": 5}}}]}}, "meta": {}}
    assert first_sol_source(tx, "W") == "SRC"
    tx2 = {"transaction": {"message": {"instructions": []}}, "meta": {"innerInstructions": [{"instructions": [
        {"parsed": {"type": "createAccount", "info": {"source": "PAYER", "newAccount": "W"}}}]}]}}
    assert first_sol_source(tx2, "W") == "PAYER"
    assert first_sol_source(None, "W") == ""


def token_with(holders: dict) -> TokenState:
    s = TokenState(MINT, Launch(MINT, 0, DEV), 0)
    s.on_launch(s.launch)
    for i, (w, tok) in enumerate(holders.items()):
        s.on_trade(Trade(MINT, 5 + i, w, "buy", 0.5, tok, 31, 1.0e9), 2)
    return s


def test_cluster_report_links_dev_funded_and_shared_funders_but_not_exchanges():
    s = token_with({"a": 50e6, "b": 40e6, "c": 30e6, "x1": 20e6, "x2": 20e6, "o": 10e6})
    funders = {"a": (DEV, ""), "b": ("MID", ""), "c": ("MID", ""),       # dev-funded + a shared (non-exchange) funder
               "x1": ("BINANCE", "exchange"), "x2": ("BINANCE", "exchange"),   # labelled exchange: not a link
               "o": ("SOLO", "")}
    rep = cluster_report(s, cohort(s, 20), funders, Counter({"MID": 2, "BINANCE": 9000}), 50)
    assert rep["linked"] == 3 and rep["pct"] == pytest.approx(12.0)
    # an unlabelled funder that funds many wallets is treated as an exchange too
    funders2 = dict(funders, x1=("HOT", ""), x2=("HOT", ""))
    assert cluster_report(s, cohort(s, 20), funders2, Counter({"HOT": 500, "MID": 2}), 50)["linked"] == 3
    assert cluster_report(s, cohort(s, 20), funders2, Counter({"HOT": 2, "MID": 2}), 50)["linked"] == 5


class ListFeed(Feed):
    realtime = False

    def __init__(self, events):
        self.ev = events

    async def events(self):
        for e in self.ev:
            self._last = e.ts
            yield e


def test_engine_rejects_entry_on_insider_cluster_and_replays_funding_events():
    c = Curve()
    ev = [Launch(MINT, 0, DEV, symbol="INS", twitter="x", v_sol=c.v_sol, v_tokens=c.v_tokens)]
    ts = 3.0
    for i in range(7):                                  # insiders funded by the dev, bought after the bundle window
        w = f"ins{i}"
        ev.append(Funding(w, ts - 1, DEV))
        tok = c.quote_buy(0.8, 0)
        c.apply(0.8, -tok)
        ev.append(Trade(MINT, ts, w, "buy", 0.8, tok, c.v_sol, c.v_tokens))
        ts += 0.7
    for i in range(50):                                 # organic flow so every other gate passes
        tok = c.quote_buy(0.25, 0)
        c.apply(0.25, -tok)
        ev.append(Trade(MINT, ts, f"org{i}", "buy", 0.25, tok, c.v_sol, c.v_tokens))
        ts += 0.5
    params = copy.deepcopy(P)
    params["sniper"]["callouts"]["enabled"] = False
    eng = Engine(params, ListFeed(ev), PaperExecutor(params.sniper.execution), mode="backtest", log_to_journal=False)
    asyncio.run(eng.run())
    s = eng.tokens[MINT]
    assert s.decided.startswith("rejected: insider cluster"), s.decided
    assert eng.rejects["insider cluster"] == 1 and not eng.book.closed

    params["sniper"]["entry"]["funding"]["enabled"] = False      # same tape without the gate -> it buys
    eng2 = Engine(params, ListFeed(ev), PaperExecutor(params.sniper.execution), mode="backtest", log_to_journal=False)
    asyncio.run(eng2.run())
    assert MINT in eng2.positions or eng2.book.closed


def test_notifier_only_sends_configured_levels(monkeypatch):
    from meme_trader.sniper.notify import Notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert not Notifier(["buy"]).enabled
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "1")

    async def go():
        n = Notifier(["buy", "error"])
        n._worker = lambda: asyncio.sleep(0)          # don't hit the network
        n.push("signal", "ignored", "paper")
        n.push("buy", "PEPE 0.05 SOL", "paper")
        n.push("error", "boom", "paper")
        return [n.queue.get_nowait() for _ in range(n.queue.qsize())]
    msgs = asyncio.run(go())
    assert len(msgs) == 2 and "BUY PEPE" in msgs[0] and "[paper]" in msgs[0]
