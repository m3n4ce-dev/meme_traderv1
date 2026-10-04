"""Token logos for the dashboard: found, fetched carefully, shrunk to a thumbnail and cached on disk.

Sources, in order: the token's own metadata image (pump.fun launches point at an IPFS image), then DexScreener's
token info. Image URLs are written by token creators, so the fetch is as careful as feeds.fetch_metadata: public
addresses only (checked at connect time, redirects included), a size cap, and the bytes must decode as a real
raster image. Pillow re-encodes every logo as a small WebP, so the dashboard only ever serves images it made.
"""
from __future__ import annotations

import asyncio
import io
import re
import time
from pathlib import Path

from ..config import ROOT

CACHE = ROOT / "data" / "logos"
MAX_BYTES = 4 * 1024 * 1024
SIZE = 96
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
# ipfs.io (where pump.fun metadata points) rate-limits busy IPs; these serve the same content (measured 2026-10-04)
GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://4everland.io/ipfs/", "https://gateway.pinata.cloud/ipfs/")
_CID = re.compile(r"/ipfs/([A-Za-z0-9]{40,100}(?:/[^?#\s]*)?)")


def thumbnail(data: bytes, size: int = SIZE) -> bytes | None:
    """Decode (PNG/JPEG/GIF/WebP only), shrink, re-encode as WebP. None if it isn't such an image."""
    try:
        from PIL import Image
    except ImportError:
        return None
    Image.MAX_IMAGE_PIXELS = 40_000_000
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("PNG", "JPEG", "GIF", "WEBP"):
                return None
            im.seek(0)
            im = im.convert("RGBA")
            im.thumbnail((size, size))
            out = io.BytesIO()
            im.save(out, "WEBP", quality=85)
            return out.getvalue()
    except Exception:                                   # truncated, a decompression bomb, not an image
        return None


def _ipfs(url: str) -> str:
    return "https://ipfs.io/ipfs/" + url[len("ipfs://"):].removeprefix("ipfs/") if url.startswith("ipfs://") else url


def ipfs_urls(url: str) -> list[str]:
    """The URL, then the same IPFS content through other gateways."""
    url = _ipfs(str(url or "").strip())
    m = _CID.search(url)
    return [url] + ([g + m.group(1) for g in GATEWAYS if not url.startswith(g)] if m else [])


async def fetch_image(url: str, timeout: float = 8.0) -> bytes | None:
    """At most MAX_BYTES from a public http(s) URL, following at most 3 redirects, each one checked."""
    from urllib.parse import urljoin

    import aiohttp

    from ..sniper.feeds import _PublicOnlyResolver, _allowed_url, read_capped

    url = _ipfs(str(url or "").strip())
    if not _allowed_url(url):
        return None
    try:
        conn = aiohttp.TCPConnector(resolver=_PublicOnlyResolver(), limit=2)
        async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            for _ in range(4):
                async with s.get(url, allow_redirects=False, headers={"Accept": "image/*"}) as r:
                    if r.status in (301, 302, 303, 307, 308):
                        url = urljoin(url, r.headers.get("Location", ""))
                        if not _allowed_url(url):
                            return None
                        continue
                    if r.status != 200 or (r.content_length or 0) > MAX_BYTES:
                        return None
                    return await read_capped(r, MAX_BYTES)
    except Exception:
        return None
    return None


class Logos:
    """engine: optional, gives a token's metadata URI (fresh launches aren't on DexScreener yet)."""

    def __init__(self, engine=None, cache_dir: Path = CACHE):
        self.engine = engine
        self.dir = Path(cache_dir)
        self.miss_until: dict[str, float] = {}
        self.inflight: dict[str, asyncio.Task] = {}
        self.slots = asyncio.Semaphore(4)

    def path(self, mint: str) -> Path:
        return self.dir / f"{mint}.webp"

    def cached(self, mint: str) -> bytes | None:
        p = self.path(mint)
        return p.read_bytes() if MINT_RE.match(mint) and p.exists() else None

    async def get(self, mint: str) -> bytes | None:
        if not MINT_RE.match(mint or ""):
            return None
        hit = self.cached(mint)
        if hit or self.miss_until.get(mint, 0) > time.time():
            return hit
        task = self.inflight.get(mint)
        if task is None:
            task = self.inflight[mint] = asyncio.create_task(self._load(mint))
            task.add_done_callback(lambda _t, m=mint: self.inflight.pop(m, None))
        return await asyncio.shield(task)

    async def _load(self, mint: str) -> bytes | None:
        async with self.slots:
            for url in [u for c in await self._candidates(mint) for u in ipfs_urls(c)][:6]:
                data = await fetch_image(url)
                img = thumbnail(data) if data else None
                if img:
                    self.dir.mkdir(parents=True, exist_ok=True)
                    tmp = self.path(mint).with_suffix(".part")
                    tmp.write_bytes(img)
                    tmp.replace(self.path(mint))
                    return img
        # a launch's metadata can arrive a little late: try a token the bot is tracking again soon
        known = bool(self.engine and mint in getattr(self.engine, "tokens", {}))
        self.miss_until[mint] = time.time() + (90 if known else 3600)
        if len(self.miss_until) > 5000:
            cut = time.time()
            self.miss_until = {m: t for m, t in self.miss_until.items() if t > cut}
        return None

    async def _candidates(self, mint: str) -> list[str]:
        urls = []
        uri = self.engine.token_uri(mint) if self.engine and hasattr(self.engine, "token_uri") else ""
        if uri:
            from ..sniper.feeds import fetch_metadata

            for u in ipfs_urls(uri)[:3]:
                img = (await fetch_metadata(u, ("image",))).get("image")
                if img:
                    urls.append(img)
                    break
        urls += await dexscreener_images([mint])
        return urls


async def dexscreener_images(mints: list[str]) -> list[str]:
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(DS_TOKENS + ",".join(mints[:30])) as r:
                rows = await r.json(content_type=None) if r.status == 200 else []
    except Exception:
        return []
    return [u for p in rows or [] if isinstance(p, dict) and (u := (p.get("info") or {}).get("imageUrl"))][:2]
