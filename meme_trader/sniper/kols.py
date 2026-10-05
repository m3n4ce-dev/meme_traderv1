"""KOL wallets (well-known memecoin traders) from kolscan.io, so the live charts can name them when they trade.

Not a signal. Measured on our own recordings (2026-10-03..05, 4,449 first buys by 241 KOLScan wallets on pump.fun
bonding curves): they hold a median 34 s (66% sell within a minute); copying them 1 s late lost 12.6% a trade
after costs, 2.5 s late 13.9%; last month's top 50 did no better, and wallets that did well one day lost the next.
So a KOL buy on a coin is a warning that a fast seller is in it, not a reason to buy (BUILD_LOG #39).

The list is fetched only when the owner asks (dashboard button or `python -m meme_trader.sniper kols`) and kept in
data/kols.json, which stays out of the repo like every other wallet address."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

URL = "https://kolscan.io/leaderboard"
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
_KOL = re.compile(r'\{"wallet_address":"([1-9A-HJ-NP-Za-km-z]{32,44})","name":"((?:[^"\\]|\\.)*)"')
_ROW = re.compile(r'\{"wallet_address":"([1-9A-HJ-NP-Za-km-z]{32,44})","name":"((?:[^"\\]|\\.)*)","telegram":(?:null|"[^"]*"),'
                  r'"twitter":(?:null|"[^"]*"),"profit":([-0-9.eE+]+),"wins":(\d+),"losses":(\d+),"timeframe":(\d+)\}')


def parse(html: str) -> dict:
    """{'kols': {wallet: name}, 'board': [{wallet, name, profit_sol, wins, losses, days}]} from the leaderboard page."""
    parts = _PUSH.findall(html)
    text = "".join(json.loads('"' + p + '"') for p in parts) if parts else html
    kols: dict[str, str] = {}
    for m in _KOL.finditer(text):
        kols.setdefault(m.group(1), json.loads('"' + m.group(2) + '"')[:40])
    board = [{"wallet": m.group(1), "name": json.loads('"' + m.group(2) + '"')[:40], "profit_sol": float(m.group(3)),
              "wins": int(m.group(4)), "losses": int(m.group(5)), "days": int(m.group(6))} for m in _ROW.finditer(text)]
    return {"kols": kols, "board": board}


def load(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d.get("kols"), dict) else {"kols": {}}
    except (OSError, ValueError, AttributeError):
        return {"kols": {}}


async def refresh(path: Path) -> tuple[dict, str]:
    """Fetch the leaderboard once and save it. (data, '') or (old data, why not)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(URL, headers={"User-Agent": "Mozilla/5.0 (meme_traderv1)"}) as r:
                if r.status != 200:
                    return load(path), f"kolscan.io answered HTTP {r.status}"
                html = await r.text()
    except Exception as e:                                # network, TLS, timeout: keep the list we had
        return load(path), f"couldn't reach kolscan.io ({type(e).__name__})"
    d = parse(html)
    if len(d["kols"]) < 10:
        return load(path), "kolscan.io's page changed: no KOL list found in it"
    d["fetched"] = time.time()
    Path(path).write_text(json.dumps(d))
    return d, ""
