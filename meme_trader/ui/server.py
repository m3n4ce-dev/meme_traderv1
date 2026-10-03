"""Local dashboard: serves the single-page UI and pushes engine snapshots over a websocket.

Binds to 127.0.0.1 by default - the control endpoints can sell positions, so don't expose
this port to the internet without putting auth in front of it.

  GET /                  the dashboard
  GET /api/analytics     KPIs, breakdowns, Monte Carlo projection, highlights
  GET /api/token/{mint}  token detail: gate checklist, model drivers, holders, tape
  GET /api/controls      live-adjustable settings
  WS  /ws                1 s snapshots; actions: pause resume kill sell posted set save

Everything that changes state goes over the websocket, which checks the page's Origin. The GET
endpoints are read-only. Every request must name 127.0.0.1/localhost as its Host, which stops DNS
rebinding (a hostile site pointing its own domain at 127.0.0.1 to read these pages).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import WSMsgType, web

from ..sniper import jsonsafe

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _local(host: str) -> bool:
    """Host header -> is it this machine? '127.0.0.1:8787', 'localhost', '[::1]:8787' are."""
    name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
    return name in LOCAL_HOSTS


def _json(obj, status: int = 200) -> web.Response:
    return web.Response(text=jsonsafe.dumps(obj), status=status, content_type="application/json",
                        headers={"Cache-Control": "no-store"})


def make_app(engine) -> web.Application:
    @web.middleware
    async def local_only(request, handler):
        if not _local(request.headers.get("Host", "")):
            return web.Response(status=403, text="dashboard only answers on 127.0.0.1 (use an SSH tunnel)")
        return await handler(request)

    app = web.Application(middlewares=[local_only])
    cache = {"snap": None, "text": ""}

    def snapshot_text() -> str:
        snap = engine.snapshot()                     # cached per tick inside the engine
        if cache["snap"] is not snap:                # encode once per tick, however many tabs are open
            cache["snap"], cache["text"] = snap, jsonsafe.dumps(snap)
        return cache["text"]

    async def index(_):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def analytics(_):
        return _json(engine.analytics())

    async def token(request):
        d = engine.token_detail(request.match_info["mint"])
        return _json(d) if d is not None else _json({"error": "not tracked (cleaned up or never seen)"}, 404)

    async def controls(_):
        return _json(engine.controls())

    async def ws(request):
        # Browsers let any website open a websocket to 127.0.0.1, so a page you visit could send KILL/SELL.
        # Only accept the dashboard's own origin (which is also what you get through an SSH tunnel).
        origin = request.headers.get("Origin", "")
        host = request.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}"} if _local(host) else set()
        if origin not in allowed:
            return web.Response(status=403, text="forbidden origin")
        # No permessage-deflate: with it, a small reply sent between the 80 KB snapshots corrupted the
        # compressed stream and Chromium dropped the socket (close 1002) the moment a setting was changed.
        # The traffic stays on this machine or inside an SSH tunnel, so compression buys nothing here.
        sock = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024, compress=False)
        await sock.prepare(request)
        lock = asyncio.Lock()                        # one writer at a time: snapshots and replies never interleave

        async def send(text: str) -> None:
            async with lock:
                if not sock.closed:
                    await sock.send_str(text)

        async def pump():
            while not sock.closed:
                await send(snapshot_text())
                await asyncio.sleep(1)

        task = asyncio.create_task(pump())
        try:
            async for msg in sock:
                if msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(cmd, dict):
                        try:
                            reply = await control(cmd)
                        except Exception as e:          # report it; never drop the dashboard's socket
                            reply = {"ok": False, "text": f"{cmd.get('action')} failed: {type(e).__name__}: {e}"}
                        if reply:
                            await send(jsonsafe.dumps({"type": "ack", "action": cmd.get("action"), **reply}))
        finally:
            task.cancel()
        return sock

    async def control(cmd: dict) -> dict | None:
        action = cmd.get("action")
        if action == "pause":
            engine.paused = True
            engine.say("info", "entries paused from dashboard")
            return {"ok": True, "text": "Entries paused"}
        if action == "resume":
            engine.paused = False
            engine.say("info", "entries resumed from dashboard")
            return {"ok": True, "text": "Entries resumed"}
        if action == "kill":
            engine.say("error", "KILL SWITCH pressed on dashboard")
            await engine.kill()
            return {"ok": True, "text": "Kill switch: selling everything, entries stopped"}
        if action == "sell" and cmd.get("mint"):
            await engine.sell_now(str(cmd["mint"]))
            return {"ok": True, "text": "Sell sent"}
        if action == "posted" and cmd.get("mint"):
            engine.mark_posted(str(cmd["mint"]))
            return None
        if action == "set" and cmd.get("key"):
            err = engine.set_control(str(cmd["key"]), cmd.get("value"))
            return {"ok": not err, "key": cmd["key"], "text": err or "Applied now. Press Save to keep it after a restart", "controls": engine.controls()}
        if action == "save":
            err = engine.save_controls()
            return {"ok": not err, "text": err or "Settings written to config/params.yaml"}
        return None

    app.add_routes([web.get("/", index), web.get("/ws", ws), web.get("/api/analytics", analytics),
                    web.get("/api/token/{mint}", token), web.get("/api/controls", controls)])
    return app


async def serve(engine, host: str = "127.0.0.1", port: int = 8787) -> None:
    runner = web.AppRunner(make_app(engine))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    await asyncio.Event().wait()
