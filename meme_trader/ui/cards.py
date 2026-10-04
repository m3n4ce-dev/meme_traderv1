"""Share cards: 1200x675 PNGs (X's 16:9 preview) for a closed trade, a call, or the call record.

Drawn with Pillow and its built-in font, so nothing extra to install. A card from a paper trade always says PAPER,
and a call card always shows the fixed-horizon results next to the peak: the card shouldn't claim more than the
ledger can back.
"""
from __future__ import annotations

import io
import time

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 675
BG_TOP, BG_BOTTOM = (22, 27, 45), (8, 10, 18)
INK, INK2, MUTED = (236, 240, 248), (178, 188, 208), (112, 124, 148)
UP, DOWN, WARN, ACCENT = (64, 214, 128), (255, 104, 104), (255, 196, 64), (122, 162, 255)


def _font(size: int):
    return ImageFont.load_default(size=size)


def _canvas(accent):
    img = Image.new("RGB", (W, H), BG_BOTTOM)
    d = ImageDraw.Draw(img)
    for y in range(H):                                    # vertical gradient
        k = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(BG_TOP, BG_BOTTOM)))
    for x in range(0, W, 60):                             # faint chart grid
        d.line([(x, 0), (x, H)], fill=(30, 36, 56))
    for y in range(0, H, 60):
        d.line([(0, y), (W, y)], fill=(30, 36, 56))
    d.rectangle([0, 0, 12, H], fill=accent)
    return img, d


def _text(d, xy, s, size, fill=INK, bold=False, anchor="la"):
    d.text(xy, s, font=_font(size), fill=fill, anchor=anchor,
           stroke_width=(2 if size >= 70 else 1) if bold else 0, stroke_fill=fill)


def _chip(d, x, y, label, fill, ink=(16, 18, 26), size=26):
    f = _font(size)
    w = d.textlength(label, font=f)
    d.rounded_rectangle([x, y, x + w + 28, y + size + 18], radius=14, fill=fill)
    d.text((x + 14, y + 8), label, font=f, fill=ink, stroke_width=1, stroke_fill=ink)
    return x + w + 40


def _logo(img, logo: bytes | None, symbol: str, xy, size=150, color=ACCENT):
    x, y = xy
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    tile = None
    if logo:
        try:
            tile = Image.open(io.BytesIO(logo)).convert("RGB").resize((size, size))
        except Exception:
            tile = None
    if tile is None:
        tile = Image.new("RGB", (size, size), color)
        ImageDraw.Draw(tile).text((size / 2, size / 2), (symbol or "?")[:1].upper(), font=_font(int(size * .55)),
                                  fill=INK, anchor="mm", stroke_width=2, stroke_fill=INK)
    img.paste(tile, (x, y), mask)
    ImageDraw.Draw(img).ellipse([x - 3, y - 3, x + size + 2, y + size + 2], outline=(255, 255, 255), width=3)


def _footer(d, left: str, right: str = "meme_trader"):
    d.line([(60, H - 92), (W - 60, H - 92)], fill=(48, 56, 82), width=2)
    _text(d, (60, H - 62), left, 24, MUTED)
    _text(d, (W - 60, H - 62), right, 24, INK2, anchor="ra")


def _png(img) -> bytes:
    b = io.BytesIO()
    img.save(b, "PNG", optimize=True)
    return b.getvalue()


def _usd(v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.3g}{suf}".replace(".0" + suf, suf)
    return f"${v:,.0f}"


def _held(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def trade_card(t: dict, logo: bytes | None = None, mode: str = "paper") -> bytes:
    """A closed trade (a row of the bot's closed list)."""
    pct, pnl = float(t.get("pnl_pct") or 0), float(t.get("pnl") or 0)
    color = UP if pnl >= 0 else DOWN
    img, d = _canvas(color)
    x = _chip(d, 60, 48, "PAPER TRADE" if not str(mode).startswith("live") else "LIVE TRADE",
              WARN if not str(mode).startswith("live") else UP)
    _chip(d, x, 48, {"late": "graduation play", "sniper": "sniper", "manual": "manual",
                     "copy": "copy trade"}.get(str(t.get("source", "")).split(":")[0], str(t.get("source", ""))),
          (44, 52, 78), INK)
    _logo(img, logo, t.get("symbol", ""), (60, 150), color=color)
    _text(d, (240, 160), f"${t.get('symbol', '?')}", 76, INK, bold=True)
    _text(d, (242, 258), f"exit: {str(t.get('exit', ''))[:48]}", 28, INK2)
    _text(d, (60, 318), f"{'+' if pct >= 0 else ''}{pct:.0f}%", 180, color, bold=True)
    _text(d, (64, 522), f"{'+' if pnl >= 0 else ''}{pnl:.3f} SOL   ·   held {_held(t.get('closed', 0) - t.get('opened', 0))}"
                        f"   ·   peak {'+' if (t.get('peak_gain_pct') or 0) >= 0 else ''}{float(t.get('peak_gain_pct') or 0):.0f}%",
          32, INK2)
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t.get("closed") or time.time()))
    _footer(d, f"{when}  ·  pump.fun bonding curve")
    return _png(img)


def call_card(c: dict, logo: bytes | None = None) -> bytes:
    """A call from the ledger, with what happened after (a CallLedger.rows() row)."""
    peak = float(c.get("peak_x") or 1)
    color = UP if peak >= 1.5 else ACCENT
    img, d = _canvas(color)
    x = _chip(d, 60, 48, f"CALL #{c.get('n', '?')}", ACCENT)
    _chip(d, x, 48, f"by {c.get('caller', '?')}", (44, 52, 78), INK)
    _logo(img, logo, c.get("symbol", ""), (60, 150), color=color)
    _text(d, (240, 160), f"${c.get('symbol', '?')}", 76, INK, bold=True)
    _text(d, (242, 258), f"called at {_usd(c.get('mcap_usd'))} market cap", 30, INK2)
    _text(d, (60, 316), f"{peak:.1f}x", 150, color, bold=True)
    _text(d, (64, 474), "peak within 24 h (sampled)", 26, MUTED)
    parts = []
    for label in ("1h", "24h"):
        v = c.get(label)
        parts.append(f"{label}: {'pending' if v is None else ('+' if v >= 0 else '') + f'{v:.0f}%'}")
    _text(d, (64, 518), "if held   " + "   ·   ".join(parts), 32, INK2)
    if c.get("thesis"):
        _text(d, (W - 60, 175), str(c["thesis"])[:44], 24, MUTED, anchor="ra")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(c.get("ts") or time.time()))
    _footer(d, f"{when}  ·  ledger {str(c.get('hash', ''))[:16]}…")
    return _png(img)


def record_card(st: dict, head: str = "") -> bytes:
    """The call record (CallLedger.stats()), leading with fixed-horizon returns after costs."""
    img, d = _canvas(ACCENT)
    _chip(d, 60, 48, "TRACK RECORD", ACCENT)
    _text(d, (60, 130), f"{st.get('calls', 0)} calls", 96, INK, bold=True)
    _text(d, (64, 240), f"by {st.get('caller', 'everyone')}  ·  every call timestamped and hash-chained", 28, INK2)
    y = 310
    for label, title in (("1h", "Held 1 hour"), ("24h", "Held 24 hours")):
        s = st.get(label)
        if s:
            col = UP if s["median_pct"] > 0 else DOWN
            _text(d, (64, y), title, 30, INK2)
            _text(d, (380, y - 8), f"median {'+' if s['median_pct'] >= 0 else ''}{s['median_pct']:.0f}%", 44, col, bold=True)
            _text(d, (760, y), f"won {s['win_rate']:.0%} of {s['n']}", 32, INK2)
            y += 80
    pk = st.get("peak")
    if pk:
        _text(d, (64, y), "Peak (sampled)", 30, INK2)
        _text(d, (380, y - 8), f"{pk['hit_rate']:.0%} hit {st.get('hit_x', 2):g}x", 44, UP, bold=True)
        _text(d, (760, y), f"median {pk['median_x']:.2f}x", 32, INK2)
        y += 80
    _text(d, (64, min(y + 4, H - 140)), f"returns after ~{st.get('cost_pct', 5):g}% costs", 24, MUTED)
    _footer(d, f"{time.strftime('%Y-%m-%d', time.gmtime())}  ·  ledger head {head[:16]}…")
    return _png(img)
