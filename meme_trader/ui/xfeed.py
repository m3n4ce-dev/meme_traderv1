"""A live X feed with no X login and no paid API: public posts through FxTwitter (api.fxtwitter.com) for the
accounts and searches you pick, plus a search for each coin the bot holds (by contract address).

Polled gently: one request every few seconds at most, each source every `every_s`, and only while someone has
looked at the feed in the last 10 minutes. Post text is written by strangers: the page shows it as plain text
and links only to x.com. Sources live in data/xfeed.json (git-ignored).
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from ..config import ROOT

PATH = ROOT / "data" / "xfeed.json"
API = "https://api.fxtwitter.com/2"
HANDLE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
MINT = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,14})\b")
DEFAULT = {"accounts": [], "searches": ["pump.fun"], "auto": True}
MAX_SOURCES = 20


class XFeedError(ValueError):
    pass


def _media_ok(u: str) -> bool:
    try:
        p = urlsplit(u)
    except ValueError:
        return False
    return p.scheme == "https" and (p.hostname or "").endswith(("twimg.com",))


def normalize(r: dict, source: str) -> dict | None:
    try:
        a = r.get("author") or {}
        url = r.get("url") or ""
        if not url.startswith("https://x.com/"):
            url = f"https://x.com/{a.get('screen_name', 'i')}/status/{r['id']}"
        text = str(r.get("text") or "")[:800]
        photos = [m.get("url") for m in ((r.get("media") or {}).get("photos") or []) if _media_ok(m.get("url", ""))][:2]
        av = a.get("avatar_url") or ""
        return {"id": str(r["id"]), "url": url, "text": text, "ts": int(r.get("created_timestamp") or 0),
                "author": {"handle": str(a.get("screen_name") or "")[:20], "name": str(a.get("name") or "")[:60],
                           "avatar": av if _media_ok(av) else "", "followers": a.get("followers"),
                           "verified": bool((a.get("verification") or {}).get("verified"))},
                "likes": r.get("likes") or 0, "reposts": r.get("reposts") or 0, "replies": r.get("replies") or 0,
                "views": r.get("views"), "photos": photos, "source": source,
                "mints": sorted(set(MINT.findall(text)))[:3], "cashtags": sorted(set(CASHTAG.findall(text)))[:5]}
    except (KeyError, TypeError, ValueError):
        return None


class XFeed:
    def __init__(self, path: Path = PATH, held=lambda: [], every_s: float = 150.0, gap_s: float = 4.0):
        self.path = Path(path)
        self.held = held                                  # -> [(symbol, mint)] the bot holds now
        self.every_s, self.gap_s = every_s, gap_s
        self.cfg = copy.deepcopy(DEFAULT)                 # its lists get appended to: never share them
        if self.path.exists():
            try:
                self.cfg.update(json.loads(self.path.read_text()))
            except ValueError:
                pass
        self.posts: dict[str, dict] = {}
        self.state: dict[str, dict] = {}                  # source -> {last, ok, error, n}
        self.last_view = 0.0

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1))
        tmp.replace(self.path)

    def sources(self) -> list[str]:
        out = [f"@{h}" for h in self.cfg["accounts"]] + [f"q:{q}" for q in self.cfg["searches"]]
        if self.cfg.get("auto"):
            out += [f"held:{sym}:{mint}" for sym, mint in self.held()][:5]
        return out

    def add(self, kind: str, value: str) -> None:
        value = (value or "").strip()
        if len(self.cfg["accounts"]) + len(self.cfg["searches"]) >= MAX_SOURCES:
            raise XFeedError(f"at most {MAX_SOURCES} sources")
        if kind == "account":
            m = HANDLE.match(value)
            if not m:
                raise XFeedError("an X handle is 1-15 letters, digits or _")
            if m.group(1).lower() in (h.lower() for h in self.cfg["accounts"]):
                raise XFeedError("already following that account")
            self.cfg["accounts"].append(m.group(1))
        elif kind == "search":
            value = " ".join(value.split())
            if not 1 <= len(value) <= 80 or not value.isprintable():
                raise XFeedError("a search is 1-80 characters")
            if value in self.cfg["searches"]:
                raise XFeedError("already searching that")
            self.cfg["searches"].append(value)
        else:
            raise XFeedError(f"unknown source kind {kind!r}")
        self._save()
        self.state.pop(f"@{value.lstrip('@')}" if kind == "account" else f"q:{value}", None)

    def remove(self, source: str) -> None:
        if source.startswith("@"):
            self.cfg["accounts"] = [h for h in self.cfg["accounts"] if h != source[1:]]
        elif source.startswith("q:"):
            self.cfg["searches"] = [q for q in self.cfg["searches"] if q != source[2:]]
        self.posts = {k: p for k, p in self.posts.items() if p["source"] != source}
        self._save()

    def set_auto(self, on: bool) -> None:
        self.cfg["auto"] = bool(on)
        self._save()

    def view(self, limit: int = 80) -> dict:
        self.last_view = time.time()
        posts = sorted(self.posts.values(), key=lambda p: -p["ts"])[:limit]
        return {"posts": posts, "sources": [{"source": s, **self.state.get(s, {})} for s in self.sources()],
                "auto": bool(self.cfg.get("auto"))}

    def _url(self, source: str) -> str:
        if source.startswith("@"):
            return f"{API}/profile/{source[1:]}/statuses?count=20"
        q = source[2:] if source.startswith("q:") else source.split(":", 2)[2]
        return f"{API}/search?q={quote(q)}&count=20"

    async def fetch(self, s: aiohttp.ClientSession, source: str) -> int:
        st = self.state.setdefault(source, {})
        st["last"] = time.time()
        try:
            async with s.get(self._url(source), headers={"User-Agent": "meme_trader-dashboard/1.0"}) as r:
                d = await r.json(content_type=None) if r.status == 200 else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            st.update(ok=False, error=type(e).__name__)
            return 0
        rows = d.get("results") if isinstance(d, dict) else None
        if not isinstance(rows, list) or (d.get("code") not in (200, None)):
            st.update(ok=False, error=f"no results (code {d.get('code') if isinstance(d, dict) else '?'})")
            return 0
        n = 0
        for r in rows:
            p = normalize(r, source) if isinstance(r, dict) else None
            if p and p["id"] not in self.posts:
                self.posts[p["id"]] = p
                n += 1
        if len(self.posts) > 600:
            keep = sorted(self.posts.values(), key=lambda p: -p["ts"])[:400]
            self.posts = {p["id"]: p for p in keep}
        st.update(ok=True, error="", n=len(rows))
        return n

    async def run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            while True:
                await asyncio.sleep(self.gap_s)
                if time.time() - self.last_view > 600:          # nobody's looking: don't poll
                    continue
                now = time.time()
                due = [x for x in self.sources() if now - (self.state.get(x) or {}).get("last", 0) >= self.every_s]
                if due:
                    await self.fetch(s, min(due, key=lambda x: (self.state.get(x) or {}).get("last", 0)))
