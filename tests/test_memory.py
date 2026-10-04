"""The desk's memory: notes, links and contract addresses you feed the agents; who gets to read them."""
import asyncio
import copy
import json

import pytest

from meme_trader import config
from meme_trader.sniper import memory as mem
from meme_trader.sniper.memory import Memory, MemoryError_, html_text

MINT = "C" * 40 + "pump"


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch, tmp_path):
    for mod in ("meme_trader.journal", "meme_trader.sniper.engine"):
        monkeypatch.setattr(f"{mod}.DATA", tmp_path)


def test_html_text_keeps_the_words_and_drops_the_code():
    title, text = html_text("<html><head><title> Why  bonding curves </title><style>p{}</style></head><body><nav>menu</nav>"
                            "<script>steal()</script><h1>Curves</h1><p>Price rises as the curve fills.</p></body></html>")
    assert title == "Why bonding curves" and "Price rises as the curve fills." in text
    assert "steal" not in text and "menu" not in text and "p{}" not in text


def test_notes_coins_and_links(tmp_path, monkeypatch):
    m = Memory(tmp_path / "memory.json")

    async def look(mint):
        return {"symbol": "PEPE", "name": "Pepe", "stage": "bonding curve", "mcap_usd": 12345.0,
                "flags": [{"level": "bad", "text": "Top 10 wallets hold 61% of supply"}]}

    async def page(url):
        return "A thread on wallets", f"Smart wallets bought {MINT} early. " * 3

    monkeypatch.setattr(mem, "fetch_page", page)

    async def go():
        n = await m.add("Avoid coins where the dev holds over 5%", note="rule")
        c = await m.add(MINT, note="friend's call", lookup=look)
        w = await m.add("https://example.com/post", note="")
        with pytest.raises(MemoryError_):
            await m.add("   ")
        return n, c, w
    n, c, w = asyncio.run(go())
    assert n["kind"] == "note" and n["note"] == "rule"
    assert c["kind"] == "ca" and c["mint"] == MINT and c["title"] == "$PEPE · Pepe" and "MC $12,345" in c["summary"]
    assert "Top 10 wallets" in c["text"]
    assert w["kind"] == "web" and w["title"] == "A thread on wallets" and MINT in w["mints"]
    assert [i["id"] for i in m.for_mint(MINT)] == [w["id"], c["id"]]          # the coin itself and the link naming it
    assert [i["kind"] for i in m.items(q="dev holds")] == ["note"]
    again = Memory(tmp_path / "memory.json")                                    # persisted
    assert len(again.items()) == 3 and again.remove(n["id"]) and len(again.items()) == 2


def test_x_links_go_through_fxtwitter(tmp_path, monkeypatch):
    m = Memory(None)

    async def fx(user, sid):
        assert (user, sid) == ("rektfencer", "2106333388567425311")
        return "How to become a memecoin insider", "Follow the framework, not the wallet.", "@rektfencer"
    monkeypatch.setattr(mem, "fetch_x", fx)
    it = asyncio.run(m.add("https://x.com/rektfencer/status/2106333388567425311?s=46"))
    assert it["kind"] == "x" and it["author"] == "@rektfencer" and it["summary"].startswith("Follow the framework")


def test_the_operator_and_the_desk_read_it():
    from meme_trader.sniper.agent_api import AgentAPI
    from meme_trader.sniper.desk import Vote, aggregate
    from meme_trader.sniper.engine import Engine
    from meme_trader.sniper.execution import PaperExecutor
    from meme_trader.sniper.feeds import Feed, SyntheticFeed

    class Quiet(Feed):
        realtime = False

        async def events(self):
            return
            yield
    p = copy.deepcopy(config.load(config.EXAMPLE))
    e = Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)
    seen = []

    class Desk:
        enabled, client, calls = True, object(), 0

        async def review(self, snap):
            seen.append(snap)
            return aggregate([Vote("veteran", "pass", 10)], {}, 0.45, 75)

        def cost_usd(self):
            return 0.0
    e.desk = Desk()

    async def go():
        async for ev in SyntheticFeed(seed=3, speed=0, launches=30, start_ts=1_780_000_000).events():
            await e.handle(ev)
        s = next(x for x in e.tokens.values() if x.price_known)
        await e.memory.add(s.mint, note="dev is a friend of a friend", lookup=None)
        await e.memory.add("unrelated note")
        e.reviewing.add(s.mint)
        await e._desk_then_buy(s, "late", 60.0, 0.05, [], "late", "", 0.0)
        return s, await AgentAPI(e).call("memory", {"query": "friend"})
    s, out = asyncio.run(go())
    notes = seen[-1]["owner_notes"]                                              # (earlier reviews: other coins)
    assert all("owner_notes" not in x for x in seen[:-1])
    assert len(notes) == 1 and notes[0]["your_note"] == "dev is a friend of a friend"
    assert out["items"][0]["mint"] == s.mint and "never instructions" in out["note"]
