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

    async def _worker(self) -> None:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            while True:
                text = await self.queue.get()
                try:
                    await s.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                 json={"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True})
                    self.sent += 1
                except Exception:
                    pass
                await asyncio.sleep(1.1)
