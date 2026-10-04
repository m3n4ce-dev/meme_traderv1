"""pump.fun coins quoted in something other than SOL (USDC, $PUMP, ...), and IPFS gateway fallbacks."""
import asyncio
import base64

from meme_trader.sniper.feeds import PUMP_PROGRAM, SolanaTradeFeed, ipfs_urls, trade_quote_mint

# real TradeEvents captured 2026-10-04: a SOL coin, a $PUMP-quoted coin, and a coin quoted in another token
SOL_EVENT = ('vdt/007mYe4smC7e3HlSD38Dk26qoQbOqODSog+N1gO4+/PRke3EvxNAjwEAAAAA19wi5hkBAAAAootf0mq0eaapzGy/awsj62GI'
    'WjceASCsqRO+7z0TiniCDsJqAAAAAFZQ+7UDAAAANsEVI/3EAwAKxYoDAAAAADYpA9drxgIA6JMUH7GOnxV02BDheOGeMGBOMXWq'
    'Lkoy38hgByfRBwkAAAAAAAAAAAAAAAAAAAAAXNypeVvkIao7sWpjj7DgZENUxwjl/n2GdknZ+U8LDloAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAHNlbGwBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAATQI8BAAAAAFZQ+7UDAAAACsWKAwAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAA=')
PUMP_QUOTED = ('vdt/007mYe6jXHXubkswPApQCNsEQaUjYY5+IrHmY1n85Nv6BCfRbwAAAAAAAAAAILsrLKQAAAAAjBn98mdT+TdNEvXBWjsVicgA'
    '3vE5S3l0MizOl13WN5GGDsJqAAAAAAAAAAAAAAAAfPhPzzzBAQAAAAAAAAAAAHxgPYOrwgAASsL40N1cvJfjKJwZfLUGKlTz2Va5'
    'zm5RFfllZ6pcs+ZfAAAAAAAAAPTcTQEAAAAA1Ja+Zt0hsn8dxjGNS5X/5HIL3kBZPg7DSWABzbMT7s3IAAAAAAAAAKPevgIAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAHNlbGwAAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAeu6mAAAAAAAA'
    'AAAADEX3342ecpVihJM/bZi3VwMug9+EYE+14Rf/9h1bEvmoe0eJAAAAABiDchx3AQAAHTz5bsoAAADIAAAAAAAAAKPevgIAAAAA'
    'AAAAAAAAAAA=')
OTHER_QUOTED = ('vdt/007mYe6ZxPRDCrfc79TntYkY0VvoM33DwBY6ub6c3pVoo2sBdAAAAAAAAAAAzhP+eV8IAAABk1OjjCaHvkKKvzVVnYcMa67R'
    'EAmXHpqEgZAueW4gUk2ODsJqAAAAAAAAAAAAAAAAzXWbz04cAwAAAAAAAAAAAM3diIO9HQIArRHmpPwpRKT6glG++BVCbhv7KMa2'
    'ZGZ3YHxq2fVmpkZfAAAAAAAAAGl5GQAAAAAAZucgkFuVF8ojibIizIiRkDHm9o88xBqh32XpK9BcdhFkAAAAAAAAAKXQGgAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAGJ1eQAAAAAAAAAAAAAAAAAAAAAAiBMAAAAAAAC0vAwAAAAAAAAA'
    'AADA8EKqrJ+CN0bMEOEE02NVkc8tejRxrm8HJ8CP2ydNhSCAeQoAAAAAsPKo7gMAAAB0GD65AAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAA==')


def test_quote_mint_of_real_events():
    assert trade_quote_mint(base64.b64decode(SOL_EVENT)) == ""
    assert trade_quote_mint(base64.b64decode(PUMP_QUOTED)) == "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
    assert trade_quote_mint(base64.b64decode(OTHER_QUOTED)).startswith("Dz9mQ9Nz")
    assert trade_quote_mint(base64.b64decode(SOL_EVENT)[:217]) == ""             # older, shorter layout = SOL
    raw = bytearray(base64.b64decode(SOL_EVENT))
    raw[258:262] = (10_000).to_bytes(4, "little")                                   # nonsense length: treated as SOL
    assert trade_quote_mint(bytes(raw)) == ""


def test_non_sol_coins_are_skipped_and_counted():
    def logs(ev):
        return {"err": None, "signature": "s", "logs": [f"Program {PUMP_PROGRAM} invoke [1]", f"Program data: {ev}",
                                                      f"Program {PUMP_PROGRAM} success"]}
    before = SolanaTradeFeed.non_sol_skipped
    t, = SolanaTradeFeed.parse_logs(logs(SOL_EVENT), 1.0)
    assert t.sol > 0 and t.v_sol > 0
    assert SolanaTradeFeed.parse_logs(logs(PUMP_QUOTED), 1.0) == []
    assert SolanaTradeFeed.parse_logs(logs(OTHER_QUOTED), 1.0) == []
    assert SolanaTradeFeed.non_sol_skipped == before + 2


def test_ipfs_gateway_fallbacks():
    cid = "bafkreihe7cin2hdj354mdnnxbsusujc6o5v7e7fbvznpgjlsud4bp2xbui"
    urls = ipfs_urls(f"https://ipfs.io/ipfs/{cid}")
    assert urls[0].startswith("https://ipfs.io/") and len(urls) == 4 and all(cid in u for u in urls)
    assert ipfs_urls("https://metadata.j7tracker.io/metadata/x.json") == ["https://metadata.j7tracker.io/metadata/x.json"]


def test_social_links_fall_back_to_another_gateway(monkeypatch, tmp_path):
    import copy

    from meme_trader import config
    from meme_trader.sniper import engine as eng
    from meme_trader.sniper.events import Launch
    from meme_trader.sniper.execution import PaperExecutor
    from meme_trader.sniper.feeds import Feed

    monkeypatch.setattr(eng, "DATA", tmp_path)

    class Quiet(Feed):
        realtime = False

        async def events(self):
            return
            yield
    p = copy.deepcopy(config.load(config.EXAMPLE))
    e = eng.Engine(p, Quiet(), PaperExecutor(p.sniper.execution), mode="paper", log_to_journal=False)
    asked = []

    async def fake(url, fields=("twitter", "telegram", "website")):
        asked.append(url)
        return {} if "ipfs.io" in url else {"twitter": "https://x.com/coin"}
    monkeypatch.setattr("meme_trader.sniper.feeds.fetch_metadata", fake)
    launch = Launch(mint="M" * 40 + "pump", ts=1.0, creator="C", uri="https://ipfs.io/ipfs/" + "Q" * 46)
    asyncio.run(e._enrich(launch))
    assert launch.twitter == "https://x.com/coin" and len(asked) == 2 and "pump.mypinata.cloud" in asked[1]
