"""Local dashboard: serves the single-page UI and pushes engine snapshots over a websocket.

Binds to 127.0.0.1 by default - the control endpoints can sell positions, so don't expose
this port to the internet without putting auth in front of it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import WSMsgType, web

STATIC = Path(__file__).parent / "static"


def make_app(engine) -> web.Application:
    app = web.Application()

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def ws(request):
        sock = web.WebSocketResponse(heartbeat=20)
        await sock.prepare(request)

        async def pump():
            while not sock.closed:
                await sock.send_str(json.dumps(engine.snapshot(), default=str))
                await asyncio.sleep(1)

        task = asyncio.create_task(pump())
        try:
            async for msg in sock:
                if msg.type == WSMsgType.TEXT:
                    await control(json.loads(msg.data))
        finally:
            task.cancel()
        return sock

    async def control(cmd: dict) -> None:
        action = cmd.get("action")
        if action == "pause":
            engine.paused = True
            engine.say("info", "entries paused from dashboard")
        elif action == "resume":
            engine.paused = False
            engine.say("info", "entries resumed from dashboard")
        elif action == "kill":
            engine.say("error", "KILL SWITCH pressed on dashboard")
            await engine.kill()
        elif action == "sell" and cmd.get("mint"):
            await engine.sell_now(cmd["mint"])

    app.add_routes([web.get("/", index), web.get("/ws", ws)])
    return app


async def serve(engine, host: str = "127.0.0.1", port: int = 8787) -> None:
    runner = web.AppRunner(make_app(engine))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    await asyncio.Event().wait()
