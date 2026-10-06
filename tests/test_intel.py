"""Data for the next studies: every active coin's X link (data/xlinks.jsonl) and graduated coins' 1-minute candles
(data/graduated-*.jsonl). Nothing is traded on either."""
import asyncio
import copy
import json

import pytest

from meme_trader import config
from meme_trader.sniper import gradlog as gl
from meme_trader.sniper.engine import Engine
from meme_trader.sniper.events import Launch
from meme_trader.sniper.execution import PaperExecutor
from meme_trader.sniper.feeds import Feed
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


class Quiet(Feed):
    def __init__(self, realtime=True):
        self.realtime = realtime

    async def events(self):
        return
        yield


def coin(i, link, buyers=25):
    m = chr(65 + i) * 40 + "pump"
    s = TokenState(m, Launch(m, 0.0, "dev", symbol=f"C{i}", twitter=link), 0.0)
    s.price_known = True
    s.buyers.update(f"w{j}" for j in range(buyers))
    return s


def test_active_coins_x_links_are_read_once_per_link_and_logged(monkeypatch, tmp_path):
    e = Engine(copy.deepcopy(P), Quiet(), PaperExecutor(P.sniper.execution), mode="paper", log_to_journal=False, persist=True)
    calls = []

    async def fake_fetch(link):
        calls.append(link)
        return {"kind": "post", "by": "@big", "followers": 120000, "verified": True, "views": 5000, "ts": 1.0, "text": "gm"}
    monkeypatch.setattr("meme_trader.sniper.memory.fetch_x_link", fake_fetch)
    post = "https://x.com/big/status/1234567890"
    a, b, quiet = coin(0, post), coin(1, post.replace("x.com", "twitter.com")), coin(2, "https://x.com/other/status/9876543210", buyers=5)
    c = coin(3, "https://x.com/i/communities/77")
    for s in (a, b, quiet, c):
        e.tokens[s.mint] = s

    async def go():
        e.now = 50.0
        e._x_intel()                                  # a: read (b waits on the 2 s gap); c: a community, no call
        for _ in range(3):
            await asyncio.sleep(0)
        e.now, e._last_x_scan = 52.0, 0.0
        e._x_intel()                                  # b: the same post, from the cache
    asyncio.run(go())
    assert calls == [post] and quiet.mint not in e.linked_x               # 5 buyers: not active yet
    rows = [json.loads(x) for x in (tmp_path / "xlinks.jsonl").read_text().splitlines()]
    by = {r["symbol"]: r for r in rows}
    assert by["C0"]["followers"] == 120000 and not by["C0"]["cached"] and by["C0"]["buyers"] == 25
    assert by["C1"]["cached"] and by["C3"]["kind"] == "X community" and "C2" not in by
    assert e.linked_x[b.mint]["by"] == "@big" and e.intel_view()["x_reads_today"] == 1


class FakeChains:
    def __init__(self, candles=None, error=""):
        self.calls, self.candles, self.error = [], candles or [], error

    async def history(self, net, pool, limit):
        self.calls.append(pool)
        return {"candles": self.candles, "error": self.error}


def test_graduated_coins_are_fetched_once_after_the_wait_and_kept(monkeypatch, tmp_path):
    g = gl.GradLog(tmp_path, after_h=1, every_s=0)
    g.add("M1", "ONE", 1000.0, 69000)
    g.add("M2", "TWO", 1000.0, 70000)
    assert json.loads((tmp_path / "graduated_queue.json").read_text())["M1"]["symbol"] == "ONE"
    assert gl.GradLog(tmp_path).queue.keys() == {"M1", "M2"}                    # survives a restart
    monkeypatch.setattr("meme_trader.clients.dexscreener.best_pair_by_mint",
                        lambda mints: {m: {"pairAddress": "P" + m, "dexId": "pumpswap"} for m in mints if m == "M1"})
    ch = FakeChains([[900, 1, 1, 1, 1, 5], [1000, 1, 2, 1, 2, 9], [1060, 2, 3, 2, 3, 4]])

    async def go(now):
        g.tick(now, ch)
        for _ in range(10):
            await asyncio.sleep(0)
    asyncio.run(go(2000.0))                                                    # not yet: an hour after graduating
    assert not ch.calls
    asyncio.run(go(4700.0))
    assert ch.calls == ["PM1"] and "M1" not in g.queue and g.queue["M2"]["tries"] == 1
    row = json.loads((tmp_path / "graduated-1970-01-01.jsonl").read_text())
    assert row["pool"] == "PM1" and [c[0] for c in row["candles"]] == [900, 1000, 1060]
    for _ in range(2):
        asyncio.run(go(4800.0))
    assert "M2" not in g.queue and g.failed_today == 1                         # no pool found three times
    assert g.view()["logged_today"] == 1
