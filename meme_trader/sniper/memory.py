"""The desk's memory: links, articles, contract addresses and notes you feed the agents.

Saved in data/memory.json. A link's text is fetched once and kept, capped: X posts and articles through FxTwitter,
other pages with the same public-only fetcher as token metadata. A contract address gets a metrics snapshot (and the
bot starts watching the coin). The Claude operator reads memory with get_memory; the AI desk sees your notes on a
coin it's voting on. Everything saved here was written by someone else: it's information for the agents to weigh,
never instructions for them.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from html.parser import HTMLParser
from pathlib import Path

MAX_ITEMS = 500
MAX_THREAD = 60
TEXT_CAP = 6000
PAGE_CAP = 1_500_000
URL = re.compile(r"^https?://\S+$")
MINT = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
X_STATUS = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")


class MemoryError_(ValueError):
    pass


class _Text(HTMLParser):
    """Title and readable text of an HTML page (no scripts, styles, navigation)."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form", "aside"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.parts, self._skip, self._in_title = "", [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "div", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))
        return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def html_text(html: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(html)
    except Exception:                                  # malformed markup: keep what parsed
        pass
    return " ".join(p.title.split())[:200], p.text()


async def fetch_page(url: str) -> tuple[str, str]:
    """(title, text) of a public web page, fetched as carefully as token metadata."""
    from urllib.parse import urljoin

    import aiohttp

    from .feeds import _PublicOnlyResolver, _allowed_url, read_capped

    if not _allowed_url(url):
        raise MemoryError_("only public http(s) links")
    conn = aiohttp.TCPConnector(resolver=_PublicOnlyResolver(), limit=2)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=15),
                                     headers={"User-Agent": "Mozilla/5.0 (meme_trader desk memory)"}) as s:
        for _ in range(4):
            async with s.get(url, allow_redirects=False) as r:
                if r.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, r.headers.get("Location", ""))
                    if not _allowed_url(url):
                        raise MemoryError_("the link redirects somewhere private")
                    continue
                if r.status != 200:
                    raise MemoryError_(f"the page answered HTTP {r.status}")
                ctype = r.headers.get("Content-Type", "")
                body = await read_capped(r, PAGE_CAP)
                if body is None:
                    raise MemoryError_("the page is too large")
                text = body.decode(r.charset or "utf-8", errors="replace")
                if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                    return html_text(text)
                return url.rsplit("/", 1)[-1][:120], text
    raise MemoryError_("too many redirects")


async def fetch_x(user: str, status_id: str) -> tuple[str, str, str]:
    """(title, text, author) of an X post or article, through FxTwitter."""
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(f"https://api.fxtwitter.com/{user}/status/{status_id}",
                         headers={"User-Agent": "meme_trader-dashboard/1.0"}) as r:
            if r.status != 200:
                raise MemoryError_(f"FxTwitter answered HTTP {r.status}")
            d = (await r.json(content_type=None)).get("tweet") or {}
        quote = d.get("quote") or {}
        art = d.get("article") or quote.get("article")
        if not art and "/i/article/" in (d.get("text") or "") and quote.get("id"):
            async with s.get(f"https://api.fxtwitter.com/{user}/status/{quote['id']}") as r2:
                art = ((await r2.json(content_type=None)).get("tweet") or {}).get("article") if r2.status == 200 else None
    a = d.get("author") or {}
    author = f"@{a.get('screen_name', user)}"
    if art:
        body = "\n".join(b.get("text", "") for b in ((art.get("content") or {}).get("blocks") or []))
        return (art.get("title") or "X article")[:200], ((d.get("text") or "") + "\n\n" + body).strip(), author
    text = d.get("text") or ""
    if quote.get("text"):
        text += f"\n\n(quoting @{(quote.get('author') or {}).get('screen_name', '?')}: {quote['text']})"
    return f"{author}: {' '.join(text.split())[:80]}", text, author


X_LINK = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:/status(?:es)?/(\d{5,25}))?/?(?:[?#].*)?$")
X_NOT_USERS = {"i", "home", "search", "intent", "explore", "hashtag", "share", "messages", "settings"}


def parse_x_link(link: str) -> tuple[str, str, str]:
    """A coin's twitter field: ("post", user, id), ("account", user, ""), ("community", "", "") or ("other", "", "")."""
    link = (link or "").strip()
    if "/i/communities/" in link:
        return "community", "", ""
    m = X_LINK.match(link)
    if not m or m.group(1).lower() in X_NOT_USERS:
        return "other", "", ""
    return ("post", m.group(1), m.group(2)) if m.group(2) else ("account", m.group(1), "")


def _year(joined: str) -> int | None:
    try:
        return int(str(joined).split()[-1])
    except (ValueError, IndexError):
        return None


async def fetch_x_link(link: str) -> dict:
    """What a coin's X link really is, through FxTwitter: the post (text, reach, when) or the account (followers,
    bio, age). The narrative persona reads it; every text in it is written by strangers."""
    import aiohttp

    kind, user, sid = parse_x_link(link)
    if kind in ("community", "other"):
        return {"kind": "X community" if kind == "community" else "not an X post or account"}
    url = f"https://api.fxtwitter.com/{user}/status/{sid}" if kind == "post" else f"https://api.fxtwitter.com/{user}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        async with s.get(url, headers={"User-Agent": "meme_trader-dashboard/1.0"}) as r:
            if r.status == 404:
                return {"kind": f"the linked {kind} doesn't exist (or is private)"}
            if r.status != 200:
                raise MemoryError_(f"FxTwitter answered HTTP {r.status}")
            d = await r.json(content_type=None)
    if kind == "account":
        u = d.get("user") or {}
        return {"kind": "account", "handle": f"@{u.get('screen_name', user)}", "followers": u.get("followers"),
                "posts": u.get("tweets"), "since": _year(u.get("joined", "")),
                "verified": bool((u.get("verification") or {}).get("verified")), "bio": str(u.get("description") or "")[:200]}
    t = d.get("tweet") or {}
    a = t.get("author") or {}
    text = " ".join(str(t.get("text") or "").split())[:400]
    q = t.get("quote") or {}
    if q.get("text"):
        text += f" (quoting @{(q.get('author') or {}).get('screen_name', '?')}: {' '.join(str(q['text']).split())[:200]})"
    return {"kind": "post", "by": f"@{a.get('screen_name', user)}", "followers": a.get("followers"),
            "account_since": _year(a.get("joined", "")), "verified": bool((a.get("verification") or {}).get("verified")),
            "text": text, "views": t.get("views"), "likes": t.get("likes"), "reposts": t.get("retweets"),
            "replies": t.get("replies"), "ts": t.get("created_timestamp")}


class Memory:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.items_: list[dict] = []
        if self.path and self.path.exists():
            try:
                self.items_ = json.loads(self.path.read_text())
            except ValueError:
                self.items_ = []

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items_[:MAX_ITEMS]))
        tmp.replace(self.path)

    def items(self, q: str = "", mint: str = "", limit: int = 50) -> list[dict]:
        q = (q or "").lower().strip()
        out = []
        for it in self.items_:
            if mint and mint != it.get("mint") and mint not in (it.get("mints") or ()):
                continue
            if q and q not in " ".join(str(it.get(k) or "") for k in ("title", "summary", "note", "text", "mint")).lower():
                continue
            out.append(it)
            if len(out) >= limit:
                break
        return out

    def for_mint(self, mint: str, limit: int = 3) -> list[dict]:
        return self.items(mint=mint, limit=limit)

    def add_reply(self, item_id: str, who: str, text: str, stance: str = "", error: str = "") -> bool:
        """One message in a note's discussion: yours ("you") or a desk persona's."""
        it = next((i for i in self.items_ if i.get("id") == item_id), None)
        if it is None:
            return False
        th = it.setdefault("thread", [])
        th.append({"who": who[:20], "text": " ".join((text or "").split())[:1200], "stance": stance[:10],
                   "error": error[:200], "ts": time.time()})
        del th[:-MAX_THREAD]
        self._save()
        return True

    def set_board(self, item_id: str, on: bool) -> bool:
        """Show or hide a note on the room's corkboard. Hidden notes stay in memory: the desk still reads them."""
        it = next((i for i in self.items_ if i.get("id") == item_id), None)
        if it is None:
            return False
        if on:
            it.pop("off_board", None)
        else:
            it["off_board"] = True
        self._save()
        return True

    def remove(self, item_id: str) -> bool:
        n = len(self.items_)
        self.items_ = [i for i in self.items_ if i.get("id") != item_id]
        self._save()
        return len(self.items_) < n

    async def add(self, raw: str, note: str = "", lookup=None, by: str = "you") -> dict:
        """raw: a link, a contract address, or plain text. lookup: async (mint) -> metrics dict."""
        raw = (raw or "").strip()
        note = " ".join((note or "").split())[:500]
        if not raw:
            raise MemoryError_("paste a link, a contract address, or a note")
        it = {"id": secrets.token_hex(5), "ts": time.time(), "by": by, "note": note, "url": "", "mint": ""}
        if MINT.fullmatch(raw):
            info = (await lookup(raw)) if lookup else {}
            sym = (info.get("symbol") or "").strip() or raw[:6]
            facts = [f"{k} {info[k]}" for k in ("stage",) if info.get(k)]
            for k, label in (("mcap_usd", "MC $"), ("liquidity_usd", "liquidity $"), ("price_usd", "price $")):
                v = info.get(k)
                if isinstance(v, (int, float)):
                    facts.append(f"{label}{v:,.6g}" if k == "price_usd" else f"{label}{v:,.0f}")
            flags = [f if isinstance(f, str) else f.get("text", "") for f in (info.get("flags") or [])][:5]
            it.update(kind="ca", mint=raw, title=f"${sym}" + (f" · {info['name']}" if info.get("name") else ""),
                      summary=" · ".join(facts), text="\n".join(x for x in flags if x)[:TEXT_CAP])
        elif URL.match(raw):
            m = X_STATUS.match(raw)
            if m:
                title, text, author = await fetch_x(m.group(1), m.group(2))
                it.update(kind="x", url=raw, title=title, text=text[:TEXT_CAP], author=author)
            else:
                title, text = await fetch_page(raw)
                it.update(kind="web", url=raw, title=title or raw[:120], text=text[:TEXT_CAP])
            it["summary"] = " ".join(it["text"].split())[:300]
        else:
            it.update(kind="note", title=" ".join(raw.split())[:80], text=raw[:TEXT_CAP],
                      summary=" ".join(raw.split())[:300])
        it["mints"] = sorted(set(MINT.findall(it.get("text") or "") + MINT.findall(note)) - {it["mint"]})[:10]
        self.items_.insert(0, it)
        del self.items_[MAX_ITEMS:]
        self._save()
        return it
