"""Local dashboard: serves the single-page UI and pushes engine snapshots over a websocket.

Binds to 127.0.0.1 by default - the control endpoints can sell positions, so don't expose
this port to the internet without putting auth in front of it.

  GET /                  the dashboard
  GET /api/analytics     KPIs, breakdowns, Monte Carlo projection, highlights
  GET /api/token/{mint}  token detail: gate checklist, model drivers, holders, tape
  GET /api/controls      live-adjustable settings
  GET /api/chat          chat history and status (chat.py)
  WS  /ws                a hello with the UI version (an open page reloads itself after an update), then
                         1 s snapshots + chat events; actions: pause resume kill sell posted set save deposit
                         risk lookup chat chat_stop chat_new chat_decide chat_opts
  POST /api/agent        AI operator tools (agent_api.py), only with the X-Agent-Token from data/agent.token

Everything that changes state goes over the websocket, which checks the page's Origin. The GET
endpoints are read-only. Every request must name 127.0.0.1/localhost as its Host, which stops DNS
rebinding (a hostile site pointing its own domain at 127.0.0.1 to read these pages).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from aiohttp import WSMsgType, web

from ..sniper import jsonsafe

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _local(host: str) -> bool:
    """Host header -> is it this machine? '127.0.0.1:8787', 'localhost', '[::1]:8787' are."""
    name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
    return name in LOCAL_HOSTS


_UI: dict = {}


def ui_version() -> tuple[str, bytes]:
    """(version, page) from the file on disk now, re-read only when it changes."""
    path = STATIC / "index.html"
    mtime = path.stat().st_mtime_ns
    if _UI.get("mtime") != mtime:
        raw = path.read_bytes()
        _UI.update(mtime=mtime, raw=raw, version=hashlib.sha1(raw).hexdigest()[:10])
    return _UI["version"], _UI["raw"]


def _json(obj, status: int = 200) -> web.Response:
    return web.Response(text=jsonsafe.dumps(obj), status=status, content_type="application/json",
                        headers={"Cache-Control": "no-store"})


def make_app(engine, agent_token: str | None = None, chat=None) -> web.Application:
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
        version, raw = ui_version()
        return web.Response(body=raw.replace(b"__UI_VERSION__", version.encode()), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def analytics(_):
        return _json(await engine.analytics_async())

    async def token(request):
        d = engine.token_detail(request.match_info["mint"])
        return _json(d) if d is not None else _json({"error": "not tracked (cleaned up or never seen)"}, 404)

    async def controls(_):
        return _json(engine.controls())

    async def chat_state(_):
        return _json(chat.public() if chat else {"messages": [], "available": False, "disabled": True})

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

        await send(jsonsafe.dumps({"type": "hello", "ui": ui_version()[0]}))
        task = asyncio.create_task(pump())
        chat_q = chat.subscribe() if chat else None

        async def chat_pump():
            while not sock.closed:
                await send(jsonsafe.dumps(await chat_q.get()))

        chat_task = asyncio.create_task(chat_pump()) if chat else None
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
            if chat_task:
                chat_task.cancel()
                chat.unsubscribe(chat_q)
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
        if action == "risk":
            err = engine.set_risk(cmd.get("level"), who="dashboard")
            info = engine.risk_info()
            return {"ok": not err, "text": err or f"Risk dial: {info['name']} (level {info['level']} of 5), saved"}
        if action == "deposit":
            try:
                out = engine.deposit_paper(float(cmd.get("sol") or 0))
            except (TypeError, ValueError) as e:
                return {"ok": False, "text": str(e)}
            if cmd.get("keep"):
                base = float(engine.p.capital.starting_sol) + float(cmd["sol"])
                engine.p.capital["starting_sol"] = base
                engine.save_setting("capital.starting_sol", round(base, 6))
            return {"ok": True, "text": f"Added {float(cmd['sol']):g} paper SOL · cash {out['cash_sol']:.3f} SOL"
                    + (" · saved as the starting balance" if cmd.get("keep") else "")}
        if action == "lookup":
            from ..sniper.lookup import lookup

            try:
                return {"ok": True, "mint": cmd.get("mint"), "lookup": await lookup(str(cmd.get("mint") or ""), engine,
                                                                                  watch=bool(cmd.get("watch")))}
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if action.startswith("chat") and chat is None:
            return {"ok": False, "text": "Chat is off (sniper.chat.enabled, and the agent API must be on)"}
        if action == "chat":
            err = await chat.send(str(cmd.get("text") or ""))
            return {"ok": False, "text": err} if err else None
        if action == "chat_stop":
            await chat.stop()
            return None
        if action == "chat_new":
            await chat.new_chat()
            return None
        if action == "chat_decide":
            err = chat.decide(str(cmd.get("aid") or ""), bool(cmd.get("allow")))
            return {"ok": False, "text": err} if err else None
        if action == "chat_opts":
            err = chat.set_options(cmd.get("model"), cmd.get("ask_first"), cmd.get("auto_read"))
            return {"ok": False, "text": err} if err else None
        return None

    app.add_routes([web.get("/", index), web.get("/ws", ws), web.get("/api/analytics", analytics),
                    web.get("/api/token/{mint}", token), web.get("/api/controls", controls),
                    web.get("/api/chat", chat_state)])
    if agent_token:
        from ..sniper.agent_api import AgentAPI, AgentError

        api = AgentAPI(engine)

        async def agent(request):
            # a secret header, not a cookie: a web page can't send it (custom headers need a CORS
            # preflight this server never grants), and only processes that can read data/agent.token can
            if not hmac.compare_digest(request.headers.get("X-Agent-Token", ""), agent_token):
                return _json({"error": "missing or wrong X-Agent-Token"}, 403)
            try:
                body = await request.json()
                if body.get("tool") == "_approval":      # the dashboard chat's permission prompt (chat.py)
                    a = body.get("args") or {}
                    if chat is None:
                        return _json({"result": {"behavior": "deny", "message": "the dashboard chat is off"}})
                    res = await chat.request_approval(str(a.get("tool_name", "")), a.get("input") or {},
                                                      str(a.get("tool_use_id") or ""))
                    return _json({"result": res})
                result = await api.call(str(body.get("tool", "")), body.get("args") or {})
            except AgentError as e:
                return _json({"error": str(e)}, 400)
            except (ValueError, TypeError) as e:
                return _json({"error": f"bad request: {e}"}, 400)
            return _json({"result": result})

        app.add_routes([web.post("/api/agent", agent)])
    return app


def write_agent_token(path: Path) -> str:
    """A fresh secret for this run, readable only by this user (the MCP server reads it from here)."""
    token = secrets.token_hex(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    tmp.replace(path)
    return token


async def start(engine, host: str = "127.0.0.1", port: int = 8787) -> web.AppRunner:
    """Bind the dashboard now (raises OSError if the port is taken) and return its runner. With the agent
    API on (sniper.agent.enabled), a new token is written to data/agent.token for the MCP server, and the
    dashboard chat (sniper.chat.enabled) can run Claude Code against it."""
    from ..journal import DATA

    token = write_agent_token(DATA / "agent.token") if (engine.p.get("agent") or {}).get("enabled") else None
    chat = None
    cc = engine.p.get("chat") or {}
    if token and cc.get("enabled", True):
        from .chat import ChatManager

        chat = ChatManager(engine, port, cc)
    runner = web.AppRunner(make_app(engine, token, chat))
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


async def serve(engine, host: str = "127.0.0.1", port: int = 8787) -> None:
    await start(engine, host, port)
    await asyncio.Event().wait()
