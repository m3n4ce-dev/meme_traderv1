"""Phone alerts via a Telegram bot: buys, sells, closes, callouts, kill switch, errors.

Set TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_ALERT_CHAT_ID (your own chat id, e.g. from
@userinfobot). Messages are queued and sent in the background, max ~1/s, so alerts never slow trading.
"""
from __future__ import annotations

import asyncio
import os


class Notifier:
    def __init__(self, levels: list[str]):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
        self.levels = set(levels)
        self.queue: asyncio.Queue | None = None
        self.sent = 0
        self.failed = 0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def push(self, level: str, text: str, mode: str) -> None:
        if not self.enabled or level not in self.levels:
            return
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=200)
            asyncio.create_task(self._worker())
        icon = {"buy": "🟦", "sell": "🟩", "close": "✅", "callout": "📣", "error": "⛔"}.get(level, "•")
        try:
            self.queue.put_nowait(f"{icon} [{mode}] {level.upper()} {text}")
        except asyncio.QueueFull:
            pass

    async def deliver(self, s, text: str, attempts: int = 3) -> bool:
        """One message, with bounded retries for rate limits (honoring retry_after) and server errors.
        Only a response Telegram marks ok counts as sent; anything else is reported, never swallowed."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for i in range(attempts):
            wait = 2.0 * 2 ** i
            try:
                async with s.post(url, json={"chat_id": self.chat, "text": text[:4000],
                                             "disable_web_page_preview": True}) as r:
                    try:
                        d = await r.json(content_type=None)
                    except Exception:
                        d = {}
                    if r.status == 200 and d.get("ok"):
                        self.sent += 1
                        return True
                    err = f"HTTP {r.status}: {str(d.get('description', ''))[:120]}"
                    if r.status == 429:
                        wait = float((d.get("parameters") or {}).get("retry_after") or wait)
                    elif r.status < 500:
                        self._fail(err)                  # 400/401/403: bad token/chat - retrying won't help
                        return False
            except Exception as e:                       # network: transient
                err = f"{type(e).__name__}: {e}"
            if i < attempts - 1:
                await asyncio.sleep(min(wait, 60))
        self._fail(err)
        return False

    def _fail(self, err: str) -> None:
        self.failed += 1
        if err != self.last_error:                       # print each new kind of failure once
            print(f"[alerts] Telegram alert NOT delivered: {err}")
        self.last_error = err

    async def _worker(self) -> None:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            while True:
                text = await self.queue.get()
                await self.deliver(s, text)
                await asyncio.sleep(1.1)
