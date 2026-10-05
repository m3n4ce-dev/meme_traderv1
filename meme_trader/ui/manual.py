"""The console manual for the chat's console_help tool: docs/CONSOLE.md (how to do things, by task) plus the
dashboard's own Guide tab, split into sections and searched by the words of the question."""
from __future__ import annotations

import html as _html
import re
from pathlib import Path

from ..config import ROOT

DOC = ROOT / "docs" / "CONSOLE.md"
PAGE = Path(__file__).parent / "static" / "index.html"
# what people say -> the words the manual uses
SYNONYMS = {"white": "light theme", "bright": "light theme", "black": "dark theme", "dark": "dark theme", "colour": "theme",
            "color": "theme", "colors": "theme", "mode": "theme", "dollar": "usd unit", "dollars": "usd unit",
            "talk": "speech", "dialog": "speech", "dialogue": "speech", "bubbles": "speech", "chatter": "speech",
            "characters": "characters bots room", "people": "characters", "move": "drag move", "walk": "move",
            "graph": "chart", "candles": "chart", "watchlist": "pin", "watch": "pin", "keyboard": "keys shortcut",
            "shortcut": "keys", "hotkey": "keys", "sell": "sell", "stop": "stop loss pause", "money": "paper balance",
            "funds": "paper balance deposit", "reset": "start over", "copycat": "og copy", "vamp": "og copy",
            "kol": "kol kolscan", "influencer": "kol", "key": "api key", "keys": "api key", "bulletin": "corkboard",
            "board": "corkboard", "notes": "corkboard note", "sticky": "corkboard note"}
STOP = set("a an and are be can do does for from get have how i in is it me my of on or show the to what when where which "
           "who why will with you your this that there".split())


def _guide() -> list[tuple[str, str]]:
    """The Guide tab's sections as plain text (title, body)."""
    try:
        h = PAGE.read_text()
    except OSError:
        return []
    i = h.find("const GUIDE = [")
    j = h.find("function renderGuide", i)
    out = []
    for m in re.finditer(r'\{id: "\w+", icon: "[^"]*", title: "([^"]*)", html: `(.*?)`\}', h[i:j], re.S):
        body = re.sub(r'\$\{K\("([^"]+)"\)\}', r"[\1]", m.group(2))
        body = re.sub(r"\$\{[^}]*\}", " ", body)
        body = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))).strip()
        out.append(("Guide tab: " + m.group(1), body))
    return out


def sections() -> list[tuple[str, str]]:
    out = []
    try:
        text = DOC.read_text()
    except OSError:
        text = ""
    for block in re.split(r"\n(?=## )", text):
        if block.startswith("## "):
            title, _, body = block[3:].partition("\n")
            out.append((title.strip(), body.strip()))
    return out + _guide()


def _stem(w: str) -> str:
    """pinned/pinning/pins -> pin, charts -> chart: enough for matching a question to a section."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            if suf in ("ing", "ed") and len(w) > 3 and w[-1] == w[-2]:
                w = w[:-1]
            break
    return w


def _words(t: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9$]+", t.lower()) if w not in STOP]


def search(question: str, top: int = 3, budget: int = 6000) -> dict:
    secs = sections()
    q = _words(question)
    q += [x for w in q for x in SYNONYMS.get(w, "").split()]
    scored = []
    for title, body in secs:
        tw, bw = set(_words(title)), _words(body)
        bc = {}
        for w in bw:
            bc[w] = bc.get(w, 0) + 1
        score = sum(3 * (w in tw) + min(bc.get(w, 0), 3) for w in set(q))
        if score:
            scored.append((score, title, body))
    scored.sort(key=lambda x: -x[0])
    found, used = [], 0
    for _, title, body in scored[:top]:
        body = body[: max(400, budget - used)]
        used += len(body)
        found.append({"section": title, "text": body})
    return {"question": question, "found": found, "all_sections": [t for t, _ in secs],
            "note": "Answer with the exact clicks or keys from these sections. If none fits, say so and point to the "
                    "closest tab; don't invent buttons."}
