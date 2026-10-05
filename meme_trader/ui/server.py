"""Local dashboard: serves the single-page UI and pushes engine snapshots over a websocket.

Binds to 127.0.0.1 by default - the control endpoints can sell positions, so don't expose
this port to the internet without putting auth in front of it.

  GET /                  the dashboard
  GET /api/analytics     KPIs, breakdowns, Monte Carlo projection, highlights
  GET /api/token/{mint}  token detail: gate checklist, model drivers, holders, tape
  GET /api/controls      live-adjustable settings (curated); /api/controls/advanced: every other one
  GET /api/chat          chat history and status (chat.py)
  GET /api/desk          what the bots are thinking, the recorder and the research tests (the Desk tab)
  GET /api/keys          which API keys are set (hints only, never values; keys.py)
  GET /api/portfolio     watched wallets (portfolio.py); GET /api/xfeed: the X feed (xfeed.py)
  GET /api/logo/{mint}   a token's logo as a small WebP (logos.py)
  WS  /ws                a hello with the UI version (an open page reloads itself after an update), then
                         1 s snapshots + chat events; actions: pause resume kill sell posted set save deposit
                         risk lookup chat chat_stop chat_new chat_decide chat_opts key_set key_clear rec
                         desk_wake pf_add pf_remove pf_refresh x_add x_remove x_auto key_test mem_add mem_del
                         mem_reply mem_ask call anchor post x_disconnect order_place order_cancel
                         m_buy m_sell m_initials m_exits m_hand away (manual trading) restart
  POST /api/agent        AI operator tools (agent_api.py), only with the X-Agent-Token from data/agent.token

Everything that changes state goes over the websocket, which checks the page's Origin. The GET
endpoints are read-only. Every request must name 127.0.0.1/localhost as its Host, which stops DNS
rebinding (a hostile site pointing its own domain at 127.0.0.1 to read these pages).
"""
from __future__ import annotations

import asyncio
import time
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



# the Live page's strategy buttons (key -> name): the owner's on/off is saved at once
STRATEGY_SWITCHES = {"entry.enabled": "Sniper", "copy.enabled": "Copy trading", "late.enabled": "Graduation plays",
                     "callouts.enabled": "Callouts"}

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


def post_summary(res: dict) -> str:
    out = []
    for ch, r in res.items():
        name = {"x": "X", "telegram": "Telegram"}.get(ch, ch)
        if r.get("ok"):
            out.append(f"posted on {name}" + (f" (~${r['cost_usd']:.3f})" if r.get("cost_usd") else "")
                       + (f"; {r['note']}" if r.get("note") else ""))
        else:
            out.append(f"{name}: {r.get('error', 'failed')}")
    return " · ".join(out)


def restart_process(engine) -> None:
    """Save everything, then run the same command again in this process (same PID, so a systemd service doesn't
    notice). Fresh code from disk is loaded, which also applies an update; open positions and orders resume from
    the saved state."""
    import sys

    for step in (engine.save_state, lambda: engine.record_file and engine.record_file.flush(),
                 lambda: engine.ledger.save(), sys.stdout.flush, sys.stderr.flush):
        try:
            step()
        except Exception:
            pass
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    os.execv(sys.executable, [sys.executable, *argv[1:]])


def make_app(engine, agent_token: str | None = None, chat=None, data_dir: Path | None = None,
             restarter=None) -> web.Application:
    """data_dir: where the portfolio, X feed and logo cache live (tests pass a temp dir)."""
    from . import keys as keymod, sidecars
    from .logos import CACHE, Logos
    from .portfolio import PATH as PF_PATH, Portfolio, PortfolioError
    from .xfeed import PATH as XF_PATH, XFeed, XFeedError

    logos = Logos(engine, (data_dir / "logos") if data_dir else CACHE)
    pf = Portfolio((data_dir / "portfolio.json") if data_dir else PF_PATH, sol_usd=lambda: engine.sol_price.usd)
    xf = XFeed((data_dir / "xfeed.json") if data_dir else XF_PATH,
               held=lambda: [(p.symbol, m) for m, p in list(engine.positions.items()) if p.source != "callout"])
    env_path = (data_dir / ".env") if data_dir else keymod.ENV
    from ..config import ROOT

    ui_path = (data_dir or ROOT / "data") / "ui.json"        # page layouts and the bots' looks (any browser)
    from . import cards
    from .social import Social, SocialError

    soc = Social(data_dir or ROOT / "data")                  # X and Telegram posting (your clicks only)

    def ui_read() -> dict:
        try:
            return json.loads(ui_path.read_text()) if ui_path.exists() else {}
        except ValueError:
            return {}
    tasks: set = set()

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    @web.middleware
    async def local_only(request, handler):
        if not _local(request.headers.get("Host", "")):
            return web.Response(status=403, text="dashboard only answers on 127.0.0.1 (use an SSH tunnel)")
        return await handler(request)

    app = web.Application(middlewares=[local_only])
    cache = {"snap": None, "text": ""}
    ui_subs: set[asyncio.Queue] = set()                # every open dashboard: screen actions from the chat

    def ui_broadcast(msg: dict) -> int:
        n = 0
        for q in list(ui_subs):
            try:
                q.put_nowait(msg)
                n += 1
            except asyncio.QueueFull:
                ui_subs.discard(q)
        return n

    def snapshot_text() -> str:
        snap = engine.snapshot()                     # cached per tick inside the engine
        if cache["snap"] is not snap:                # encode once per tick, however many tabs are open
            cache["snap"], cache["text"] = snap, jsonsafe.dumps(snap)
        return cache["text"]

    async def index(_):
        version, raw = ui_version()
        return web.Response(body=raw.replace(b"__UI_VERSION__", version.encode()), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    edge_cache: list = [0.0, None]

    wallet_view = {"proc": None}

    async def wallets(_):
        """The wallet study for Analytics: read from data/wallets/view.json, rebuilt in a separate process at
        most every 30 minutes (its memory goes away when it finishes; the bot's doesn't grow)."""
        import sys

        from ..wallets.recorder import DATA as WDATA
        path = WDATA / "view.json"
        age = time.time() - path.stat().st_mtime if path.exists() else None
        proc = wallet_view["proc"]
        running = proc is not None and proc.returncode is None
        if (age is None or age > 1800) and not running and WDATA.exists():
            wallet_view["proc"] = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "meme_trader.wallets", "view", "wallets-v1", "--out", str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parents[2]))
            running = True
        if not path.exists():
            return _json({"pending": True, "refreshing": running})
        try:
            d = json.loads(path.read_text())
        except ValueError:
            return _json({"pending": True, "refreshing": running})
        return _json({**d, "age_s": round(age or 0), "refreshing": running})

    async def analytics(_):
        d = await engine.analytics_async()
        if isinstance(d, dict):
            if time.time() - edge_cache[0] > 120:             # the trade files only change when a trade closes
                from ..journal import DATA
                from ..sniper.edge import as_text, report
                from ..sniper.report import load_trades
                closed = list(engine.book.closed)

                def build():                                  # the real bot: 14 days of its trade files; a demo: this run
                    rep = report(load_trades(DATA, 14, engine.mode) if engine.persist else closed, engine.mode)
                    return {**rep, "text": as_text(rep)}
                edge_cache[:] = [time.time(), await asyncio.to_thread(build)]
            d = {**d, "exit_lab": {"late": engine.lab.view("late"), "sniper": engine.lab.view("sniper"), "desk_pass": engine.lab.view("desk-pass")}, "edge_check": edge_cache[1]}   # ("edge" is analytics' own)
        return _json(d)

    async def token(request):
        d = engine.token_detail(request.match_info["mint"])
        return _json(d) if d is not None else _json({"error": "not tracked (cleaned up or never seen)"}, 404)

    async def controls(_):
        return _json(engine.controls())

    async def advanced(_):
        return _json(engine.advanced_controls())

    async def chat_state(_):
        return _json(chat.public() if chat else {"messages": [], "available": False, "disabled": True})

    async def desk(_):
        d = engine.desk_view()
        d["recorder"] = sidecars.recorder_info()
        d["research"] = sidecars.research_info()
        d["anthropic_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
        d["chat"] = {"available": bool(chat and chat.public().get("available"))} if chat else {"available": False}
        return _json(d)

    async def keys(_):
        return _json(keymod.status(env_path))

    async def portfolio(_):
        if pf.stale():
            spawn(pf.refresh())
        return _json(pf.view())

    async def xfeed(_):
        return _json(xf.view())

    async def ui_state(_):
        return _json(ui_read())

    def memory_view(q: str = "", mint: str = "") -> dict:
        return {"items": engine.memory.items(q=q, mint=mint, limit=100), "total": len(engine.memory.items_),
                "waiting": {k: sorted(v) for k, v in engine.note_waiting.items()},
                "replies": {**engine.note_stats, "on": bool(engine.p.desk.get("note_replies", True)),
                            "key": bool(os.environ.get("ANTHROPIC_API_KEY"))}}

    async def memory(request):
        return _json(memory_view(request.query.get("q", ""), request.query.get("mint", "")))

    async def calls(request):
        caller = request.query.get("caller", "")
        L = engine.ledger
        return _json({"rows": L.rows(caller, limit=300), "stats": L.stats(caller), "verify": L.verify(),
                      "head": L.head, "anchors": [r for r in L.records if r.get("type") == "anchor"][-6:][::-1]})

    async def calls_export(_):
        L = engine.ledger
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in L.records)
        return web.Response(text=body, content_type="application/x-ndjson", headers={
            "Content-Disposition": f'attachment; filename="calls-{L.head[:12]}.jsonl"', "Cache-Control": "no-store"})

    from .chains import Chains
    chains = engine.chains = Chains()            # other chains, read-only (GeckoTerminal, cached 90 s)

    async def chains_view(_):
        return _json(await chains.get(viewer=True, wait=False))

    async def lab_view(_):                      # the team's lab: running, queued, results
        from ..sniper.lab import TESTABLE
        return _json({**engine.xlab.view(), "baseline": engine.lab_baseline(),
                      "testable": {k: [lo, hi] for k, (lo, hi) in TESTABLE.items()}})

    hq_started = time.time()

    async def hq(_):
        """Everything running on this machine, at a glance (Controls -> HQ)."""
        import shutil

        from ..sniper.lab import free_mb

        async def unit(name):
            try:
                p = await asyncio.create_subprocess_exec("systemctl", "--user", "is-active", name,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(p.communicate(), 3)
                return out.decode().strip() or "unknown"
            except (OSError, asyncio.TimeoutError):
                return "unknown"
        rss = 0
        try:
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) // 1024
        except OSError:
            pass
        du = shutil.disk_usage(str(data_dir or "."))
        f = engine.snapshot().get("feed") or {}
        lv = engine.xlab.view()
        recorder = {}
        try:
            from ..wallets.recorder import DATA as WDATA
            st = json.loads((WDATA / "status.json").read_text())
            recorder = {"active": st.get("active"), "pause_reason": st.get("pause_reason"), "updated_s": round(time.time() - st.get("updated", 0))}
        except (OSError, ValueError, ImportError):
            pass
        d = engine.desk_view()
        return _json({
            "bot": {"up_s": round(time.time() - hq_started), "rss_mb": rss, "mode": engine.mode, "paused": engine.paused,
                    "halted": engine.book.halted},
            "services": {u: await unit(u) for u in ("meme-sniper", "meme-wallets", "edge-scout")},
            "feed": {k: f.get(k) for k in ("host", "lag_s", "degraded_reason", "reconnects_1h", "switches_1h", "mb_per_hour", "backup")},
            "recorder": recorder,
            "lab": {"running": bool(lv["running"]), "queued": len(lv["queued"]), "tests": lv["tries"]},
            "xchain": (lambda x: {"active": x["active"], "note": x["mode_note"] or x["blocked"], "open": len(x["positions"]),
                                  "today": x["today"], "ready": f"{x['ready']['passed']} of {len(x['ready']['checks'])}",
                                  "last_scan_s": round(time.time() - x["last_scan"]) if x["last_scan"] else None})(engine.xchain.view()),
            "desk": {"awake": bool(engine.desk), "model": (d.get("desk") or {}).get("model") if isinstance(d.get("desk"), dict) else None,
                     "vote_s": engine.vote_speed(),
                     "huddles": len(engine.huddles), "huddle_error": engine.huddle_error},
            "chat": {"available": bool(chat and chat._status().get("available"))} if chat else {"available": False},
            "machine": {"disk_free_gb": round(du.free / 1e9, 1), "disk_used_pct": round(du.used / du.total * 100), "mem_free_mb": round(free_mb() or 0)},
        })

    async def kols_view(_):                     # the Charts tab's KOL tracker
        return _json(engine.kol_view())

    async def hot_names(_):                     # the Charts tab's copycat waves
        return _json({"names": engine.hot_names()})

    async def pulse_board(_):
        return _json(engine.pulse_view())

    async def social(_):
        return _json(soc.status())

    async def make_card(spec: dict) -> bytes | None:
        kind, ident = str(spec.get("kind") or ""), str(spec.get("id") or "")
        if kind == "trade":
            mint, _, closed = ident.partition(":")
            t = next((c for c in reversed(engine.book.closed) if c.get("mint") == mint
                      and (not closed or abs(float(c.get("closed", 0)) - float(closed)) < 2)), None)
            if t is None:
                return None
            return await asyncio.to_thread(cards.trade_card, t, await logos.get(mint), engine.mode)
        if kind == "call":
            row = next((r for r in engine.ledger.rows(limit=100_000) if r["id"] == ident), None)
            if row is None:
                return None
            return await asyncio.to_thread(cards.call_card, row, await logos.get(row["mint"]))
        if kind == "record":
            return await asyncio.to_thread(cards.record_card, engine.ledger.stats(ident), engine.ledger.head)
        return None

    async def card_png(request):
        png = await make_card(dict(request.query))
        if not png:
            return web.Response(status=404, text="no such trade or call")
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    async def x_connect(request):
        try:
            url = soc.oauth2_start(f"http://{request.host}/x/callback")
        except SocialError as e:
            return web.Response(text=str(e), status=400)
        raise web.HTTPFound(url)

    async def x_callback(request):
        if request.query.get("error"):
            raise web.HTTPFound("/#x-denied")
        try:
            user = await soc.oauth2_finish(request.query.get("code", ""), request.query.get("state", ""))
        except SocialError as e:
            return web.Response(text=f"Couldn't connect X: {e}", status=400)
        engine.say("info", f"X connected as @{user.get('username') or '?'}")
        raise web.HTTPFound("/#x-connected")

    async def logo(request):
        img = await logos.get(request.match_info["mint"])
        if not img:     # 204, not 404: the page falls back to initials either way, without an error per coin in the console
            return web.Response(status=204, headers={"Cache-Control": "max-age=60"})
        return web.Response(body=img, content_type="image/webp",
                            headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

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
        # live charts: the page names the coins it's charting; every trade on them goes out within ~0.25 s
        # (the full snapshot only goes out once a second, and the charts used to poll every 2-2.5 s)
        charts: dict[str, float] = {}               # mint -> time of the last trade sent

        async def chart_pump():
            marks_seen: dict[str, str] = {}
            while not sock.closed:
                await asyncio.sleep(0.25)
                out = {}
                for m in list(charts)[:12]:
                    d = engine.chart_data(m, charts[m])
                    if d is None:
                        continue
                    mk = json.dumps(d["marks"])[-4000:]
                    d["full"] = charts[m] == 0.0              # the whole history: the page replaces what it had
                    fresh = d["full"] or marks_seen.get(m) != mk
                    if d["pts"] or fresh:
                        if d["pts"]:
                            charts[m] = d["pts"][-1][0]
                        elif charts[m] == 0.0:
                            charts[m] = 0.001
                        if not fresh:
                            d.pop("marks")
                        marks_seen[m] = mk
                        out[m] = d
                if out:
                    await send(jsonsafe.dumps({"type": "ticks", "charts": out}))

        chart_task = asyncio.create_task(chart_pump())
        ui_q: asyncio.Queue = asyncio.Queue(maxsize=50)
        ui_subs.add(ui_q)

        async def ui_pump():
            while not sock.closed:
                await send(jsonsafe.dumps(await ui_q.get()))

        ui_task = asyncio.create_task(ui_pump())
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
                    if isinstance(cmd, dict) and cmd.get("action") == "chart_sub":   # (not a bot action: just what to stream)
                        want = [m for m in (cmd.get("mints") or []) if isinstance(m, str) and 30 <= len(m) <= 50][:12]
                        for m in list(charts):
                            if m not in want:
                                charts.pop(m)
                        for m in want:
                            charts.setdefault(m, 0.0)
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
            chart_task.cancel()
            ui_task.cancel()
            ui_subs.discard(ui_q)
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
        if action == "unhalt":                            # the owner lifts the kill switch (never the agent)
            err = engine.clear_halt("dashboard")
            return {"ok": not err, "text": err or f"Trading resumed. The kill switch trips again at {engine.kill_at():.3f} SOL"}
        if action == "sell" and cmd.get("mint"):
            await engine.sell_now(str(cmd["mint"]))
            return {"ok": True, "text": "Sell sent"}
        if action == "posted" and cmd.get("mint"):
            engine.mark_posted(str(cmd["mint"]))
            return None
        if action == "set" and cmd.get("key") and cmd.get("save") and str(cmd["key"]) in STRATEGY_SWITCHES:
            # the strategy buttons on the Live page: the owner's on/off is kept across restarts, like the risk dial
            # (applied-only, a restart for a deploy turned the sniper back on behind the owner's back)
            key = str(cmd["key"])
            err = engine.set_control(key, cmd.get("value"))
            if not err:
                try:
                    engine.save_setting(key, bool(next((c['value'] for c in engine.controls() if c['key'] == key), False)))
                except Exception as e:                    # disk, YAML: the change still applies until a restart
                    err = f"applied, but not saved: {e}"
            on = bool(next((c['value'] for c in engine.controls() if c['key'] == key), False))
            return {"ok": not err, "key": key, "text": err or f"{STRATEGY_SWITCHES[key]} {'on' if on else 'off'} (saved)",
                    "controls": engine.controls(), "advanced": engine.advanced_controls()}
        if action == "set" and cmd.get("key"):
            err = engine.set_control(str(cmd["key"]), cmd.get("value"))
            return {"ok": not err, "key": cmd["key"], "text": err or "Applied now. Press Save to keep it after a restart",
                    "controls": engine.controls(), "advanced": engine.advanced_controls()}
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
        if action == "key_set":
            try:
                k = keymod.set_key(str(cmd.get("name") or ""), str(cmd.get("value") or ""), env_path)
            except keymod.KeyError_ as e:
                return {"ok": False, "text": str(e)}
            engine.say("info", f"{k['label']} set from the dashboard")
            note = " (restart the bot to use it)" if k["effect"] == "restart" else ""
            if k["name"].startswith("ANTHROPIC_") and engine.desk and engine.desk.enabled:
                err = engine.set_desk(True, who="key")   # rebuild the desk's client with the new key now
                note = f" · AI desk {'restarted with it' if not err else 'could not restart: ' + err}"
            return {"ok": True, "keys": keymod.status(env_path), "text": f"{k['label']} saved to .env{note}"}
        if action == "key_test":
            ok, text = await keymod.test_key(str(cmd.get("name") or "ANTHROPIC_API_KEY"))
            return {"ok": ok, "text": ("✓ " if ok else "✕ ") + text}
        if action == "key_clear":
            try:
                k = keymod.clear_key(str(cmd.get("name") or ""), env_path)
            except keymod.KeyError_ as e:
                return {"ok": False, "text": str(e)}
            if k["name"] == "ANTHROPIC_API_KEY" and engine.desk and engine.desk.enabled:
                engine.set_desk(False, who="key")
            return {"ok": True, "keys": keymod.status(env_path), "text": f"{k['label']} removed"}
        if action == "rec":
            try:
                info = sidecars.set_recorder(cmd)
            except (ValueError, TypeError) as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "recorder": info, "text": "Wallet recorder: " + ("recording" if info["active"] else info["pause_reason"])
                    + ("" if info["running"] else " (the meme-wallets service isn't running)")}
        if action == "desk_brain":
            from ..sniper import desk as deskmod

            err = engine.set_desk_brain(str(cmd.get("provider") or ""), str(cmd.get("model") or ""), str(cmd.get("base_url") or ""))
            note = ""
            if not err and engine.p.desk.get("provider") != "anthropic":
                spec = deskmod.PROVIDERS[engine.p.desk["provider"]]
                price = await deskmod.model_price(engine.p.desk.get("base_url") or spec["base_url"],
                                                  os.environ.get(spec["key"], "") if spec["key"] else "", deskmod.model_of(engine.p.desk))
                engine.set_desk_prices(*(price or (0.0, 0.0)))
                note = f" · ${price[0]:g} / ${price[1]:g} per million tokens" if price else " · no price listed: the estimate shows 0"
                if engine.p.desk["provider"] == "huggingface" and os.environ.get("HF_TOKEN"):
                    try:                                  # a free account can't pay past its small monthly credit
                        import aiohttp
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as hs:
                            async with hs.get("https://huggingface.co/api/whoami-v2",
                                              headers={"Authorization": "Bearer " + os.environ["HF_TOKEN"]}) as r:
                                who = await r.json(content_type=None)
                        if not who.get("isPro") and not who.get("canPay"):
                            note += (" · your Hugging Face account is free: a few cents of credit a month, then the desk's"
                                     " calls fail until it resets (and a desk that can't vote blocks entries)")
                    except Exception:
                        pass
            return {"ok": not err, "text": err or "AI desk model saved" + note, "brain": engine.desk_brain()}
        if action == "desk_models":                       # what a local / compatible server has installed
            import aiohttp

            from ..sniper import desk as deskmod
            base = (str(cmd.get("base_url") or "") or deskmod.PROVIDERS["local"]["base_url"]).rstrip("/")
            if not base.startswith(("http://", "https://")):
                return {"ok": False, "models": []}
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                    async with s.get(base + "/models") as r:
                        d = await r.json(content_type=None)
                ids = sorted(str(m.get("id")) for m in (d.get("data") or []) if isinstance(m, dict) and m.get("id"))
            except Exception:
                ids = []
            return {"ok": True, "action": "desk_models", "models": ids[:300]}
        if action == "huddle":                            # the owner calls a team meeting
            from ..sniper import desk as deskmod

            why = deskmod.provider_ready(engine.p.desk)
            if why:
                return {"ok": False, "text": why}
            asyncio.ensure_future(engine.huddle("the owner called a meeting"))
            return {"ok": True, "text": "The team is gathering at the AI table…"}
        if action == "kols_refresh":                      # the owner's click: one fetch of kolscan.io's list
            err = await engine.refresh_kols()
            n = len(engine.kols.get("kols") or {})
            return {"ok": not err, "text": err or f"{n} KOL wallets loaded: the live charts name them when they trade"}
        if action == "xchain_sell":                       # the owner sells an other-chain paper position
            err = await engine.xchain.sell_now(str(cmd.get("key") or ""))
            return {"ok": not err, "text": err or "Sold (paper)"}
        if action == "lab_add":                           # the owner queues a test for the lab
            x, err = engine.lab_add(str(cmd.get("key") or ""), cmd.get("value"), str(cmd.get("why") or "the owner's idea"), "you")
            return {"ok": not err, "text": err or f"Queued: {x['key']} {x['now']} → {x['value']}. The team replays the last 24 hours with it."}
        if action == "lab_drop":
            before = len(engine.xlab.items)
            engine.xlab.items = [x for x in engine.xlab.items if not (x["id"] == cmd.get("id") and x["status"] == "queued")]
            engine.xlab.save()
            return {"ok": len(engine.xlab.items) < before, "text": "Taken off the queue" if len(engine.xlab.items) < before else "only a queued test can be dropped"}
        if action == "huddle_apply":                      # the owner approves one of the team's setting changes
            try:
                err = engine.apply_huddle_action(str(cmd.get("id") or ""), int(cmd.get("i")))
            except (TypeError, ValueError):
                err = "which change?"
            return {"ok": not err, "text": err or "Applied and saved"}
        if action == "desk_test":
            from ..config import Params
            from ..sniper import desk as deskmod

            why = deskmod.provider_ready(engine.p.desk)
            if why:
                return {"ok": False, "text": why}
            brain = deskmod.Desk(Params({**engine.p.desk, "enabled": True}))
            t0 = time.time()
            try:
                d, _ = await asyncio.wait_for(brain.chat("You are a connectivity check.", 'Reply with {"ok": true}.',
                                                          {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                           "required": ["ok"], "additionalProperties": False}, max_tokens=50), 60)
            except Exception as e:
                return {"ok": False, "text": "✕ " + deskmod.friendly_error(f"{type(e).__name__}: {e}")}
            return {"ok": True, "text": f"✓ {deskmod.model_of(engine.p.desk)} answered in {time.time() - t0:.1f} s"
                                        + (" (too slow for trade votes: they time out at 12 s; fine for note replies)" if time.time() - t0 > 10 else "")}
        if action == "desk_wake":
            on = bool(cmd.get("on"))
            err = engine.set_desk(on)
            return {"ok": not err, "text": err or ("AI desk is awake: it votes on every entry (stays on after a restart)" if on
                                                   else "AI desk is resting (stays off after a restart)")}
        if action == "paper_reset":
            err = engine.reset_paper()
            return {"ok": not err, "text": err or f"Paper account started over at {engine.book.start_sol:g} SOL"}
        if action in ("pf_add", "pf_remove", "pf_refresh"):
            try:
                if action == "pf_add":
                    pf.add(str(cmd.get("address") or ""), str(cmd.get("label") or ""), bool(cmd.get("mine")))
                    spawn(pf.refresh([str(cmd.get("address")).strip()]))
                elif action == "pf_remove":
                    pf.remove(str(cmd.get("address") or ""))
                else:
                    spawn(pf.refresh([w["address"] for w in pf.wallets]))
            except PortfolioError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "portfolio": pf.view(), "text": {"pf_add": "Watching it", "pf_remove": "Removed",
                                                                  "pf_refresh": "Refreshing"}[action]}
        if action == "ui_set":
            key, value = str(cmd.get("key") or ""), cmd.get("value")
            if key not in ("layout", "bots", "pulse", "room") or not isinstance(value, dict) or len(json.dumps(value)) > 64_000:
                return {"ok": False, "text": "can't save that"}
            data = ui_read()
            data[key] = value
            ui_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = ui_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(ui_path)
            return None
        if action == "mem_add":
            from ..sniper.lookup import lookup
            from ..sniper.memory import MemoryError_

            async def look(mint):
                try:
                    return await lookup(mint, engine, watch=True)
                except ValueError:
                    return {}
            try:
                it = await engine.memory.add(str(cmd.get("text") or ""), str(cmd.get("note") or ""), lookup=look)
            except (MemoryError_, OSError, asyncio.TimeoutError) as e:
                return {"ok": False, "text": f"couldn't save that: {e}"}
            engine.say("info", f"memory: saved {it['kind']} \"{it['title'][:60]}\"", it.get("mint") or "")
            spawn(engine.discuss_note(it["id"]))                  # every persona reads it and replies
            return {"ok": True, "memory": memory_view(), "added": it,
                    "text": f"Saved to the desk's memory: {it['title'][:70]}"}
        if action in ("mem_reply", "mem_ask"):
            iid = str(cmd.get("id") or "")
            if action == "mem_reply":
                if not engine.memory.add_reply(iid, "you", " ".join(str(cmd.get("text") or "").split())[:600]):
                    return {"ok": False, "text": "that note is gone"}
                import re as _re

                from ..sniper.desk import PERSONAS
                asked = [p for p in _re.findall(r"@(\w+)", str(cmd.get("text") or "").lower()) if p in PERSONAS]
                spawn(engine.discuss_note(iid, asked or None))
            else:
                spawn(engine.discuss_note(iid))
            return {"ok": True, "memory": memory_view(), "text": "The desk is reading it"}
        if action == "mem_board":                         # the corkboard: hide a note from it, or put it back
            ok = engine.memory.set_board(str(cmd.get("id") or ""), bool(cmd.get("on")))
            return {"ok": ok, "memory": memory_view(), "text": ("Back on the board" if cmd.get("on") else
                    "Hidden from the board: the desk still remembers it") if ok else "that note is gone"}
        if action == "mem_del":
            engine.memory.remove(str(cmd.get("id") or ""))
            return {"ok": True, "memory": memory_view(), "text": "Removed from memory"}
        if action == "restart":
            engine.say("info", "restarting from the dashboard: state saved, back in a few seconds")
            asyncio.get_running_loop().call_later(0.6, (restarter or restart_process), engine)
            return {"ok": True, "text": "Restarting… this page reconnects by itself", "restarting": True}
        if action == "m_hand":
            n, err = engine.hand_over(str(cmd.get("mint") or ""), bool(cmd.get("on")))
            return {"ok": not err, "text": err or (("🤖 The bots ride it now: half out at 2x, then a trailing stop" if cmd.get("on")
                                                    else "✋ It's yours again: only your exits apply") if cmd.get("mint")
                                                   else f"{n} position(s) {'handed to the bots' if cmd.get('on') else 'back to you'}")}
        if action == "away":
            return {"ok": True, "text": engine.set_away(bool(cmd.get("on")))}
        if action == "order_place":
            err, o = await engine.place_order(str(cmd.get("mint") or ""), str(cmd.get("side") or ""),
                                              str(cmd.get("op") or ""), cmd.get("mcap_usd"), cmd.get("sol"),
                                              cmd.get("frac"), cmd.get("ttl_h") or 24)
            return {"ok": not err, "text": err or f"Order placed: {engine.order_label(o)}"}
        if action == "order_cancel":
            err = engine.cancel_order(str(cmd.get("id") or ""))
            return {"ok": not err, "text": err or "Order cancelled"}
        if action == "call":
            from ..sniper.calls import LedgerError
            try:
                rec = await engine.make_call(str(cmd.get("mint") or ""), str(cmd.get("thesis") or ""))
            except (LedgerError, ValueError) as e:
                return {"ok": False, "text": str(e)}
            out = {"ok": True, "call": rec, "text": f"📣 Call #{rec['n']} on the record: {rec['symbol']} at "
                                                    f"${rec['mcap_usd']:,.0f} market cap"}
            to = tuple(t for t in (cmd.get("to") or []) if t in ("x", "telegram"))
            if to:
                mc = rec["mcap_usd"]
                mc_s = f"${mc / 1e6:.2f}M" if mc >= 1e6 else f"${mc / 1e3:.1f}K"
                text = (f"📣 ${rec['symbol']} at {mc_s} market cap" + (f"\n{rec['thesis']}" if rec["thesis"] else "")
                        + f"\n\nCA: {rec['mint']}\nCall #{rec['n']} · ledger {rec['hash'][:12]}")
                png = await make_card({"kind": "call", "id": rec["id"]})
                res = await soc.post(text, png, to)
                out["posted"] = res
                out["text"] += " · " + post_summary(res)
            return out
        if action == "anchor":
            from ..sniper.calls import proof_text
            to = tuple(t for t in (cmd.get("to") or []) if t in ("x", "telegram"))
            if not to:
                return {"ok": False, "text": "pick where to publish it"}
            res = await soc.post(proof_text(engine.ledger), None, to)
            for ch, r in res.items():
                if r.get("ok"):
                    engine.ledger.anchor(ch, r.get("url", ""))
            return {"ok": any(r.get("ok") for r in res.values()), "text": "Ledger head: " + post_summary(res),
                    "social": soc.status()}
        if action == "post":
            to = tuple(t for t in (cmd.get("to") or []) if t in ("x", "telegram"))
            if not to:
                return {"ok": False, "text": "pick X, Telegram or both"}
            png = await make_card(cmd["card"]) if isinstance(cmd.get("card"), dict) else None
            res = await soc.post(str(cmd.get("text") or ""), png, to)
            return {"ok": any(r.get("ok") for r in res.values()), "text": post_summary(res), "results": res,
                    "social": soc.status()}
        if action == "x_disconnect":
            soc.disconnect()
            return {"ok": True, "text": "X disconnected (keys in .env, if any, still work)", "social": soc.status()}
        if action == "m_buy":
            sol = cmd.get("sol")
            mint = str(cmd.get("mint") or "").strip()
            held = engine.positions.get(mint)
            err = await engine.manual_buy(mint, sol)
            if not err and cmd.get("hand"):                # "Buy & give to bots": the bots ride it from the fill
                engine.hand_after[mint] = engine.now
            if not err and held is not None:
                return {"ok": True, "text": f"Adding {float(sol):g} SOL to {held.symbol}" + (" · the bots ride it" if cmd.get("hand") else "")}
            if err:
                return {"ok": False, "text": err}
            if mint in engine.manual_queue:
                return {"ok": True, "text": f"Queued {float(sol):g} SOL: buying at its first trade on the curve"}
            return {"ok": True, "text": f"Buy {float(sol):g} SOL sent" + (": the bots ride it once it fills" if cmd.get("hand") else "")}
        if action == "m_sell":
            err = await engine.manual_sell(str(cmd.get("mint") or ""), cmd.get("fraction"))
            return {"ok": not err, "text": err or "Sell sent"}
        if action == "m_initials":
            err = await engine.take_initials(str(cmd.get("mint") or ""))
            return {"ok": not err, "text": err or "Taking initials: the rest rides for free"}
        if action == "m_exits":
            err = engine.set_manual_exits(str(cmd.get("mint") or ""), cmd.get("sl"), cmd.get("tp"), cmd.get("tp_frac"),
                                          cmd.get("trail"))
            return {"ok": not err, "text": err or "Exit rules saved for that position"}
        if action == "social_test":
            return {"ok": True, "social": soc.status()}
        if action in ("x_add", "x_remove", "x_auto"):
            try:
                if action == "x_add":
                    xf.add(str(cmd.get("kind") or ""), str(cmd.get("value") or ""))
                elif action == "x_remove":
                    xf.remove(str(cmd.get("source") or ""))
                else:
                    xf.set_auto(bool(cmd.get("on")))
            except XFeedError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "xfeed": xf.view(), "text": "X feed updated"}
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

    app.add_routes([web.get("/", index), web.get("/ws", ws), web.get("/api/analytics", analytics), web.get("/api/wallets", wallets),
                    web.get("/api/token/{mint}", token), web.get("/api/controls", controls),
                    web.get("/api/chat", chat_state), web.get("/api/controls/advanced", advanced),
                    web.get("/api/desk", desk), web.get("/api/keys", keys), web.get("/api/portfolio", portfolio),
                    web.get("/api/xfeed", xfeed), web.get("/api/logo/{mint}", logo), web.get("/api/memory", memory),
                    web.get("/api/calls", calls), web.get("/api/calls/export", calls_export),
                    web.get("/api/pulse", pulse_board), web.get("/api/social", social),
                    web.get("/api/kols", kols_view), web.get("/api/hot", hot_names), web.get("/api/lab", lab_view), web.get("/api/chains", chains_view), web.get("/api/hq", hq),
                    web.get("/api/card.png", card_png), web.get("/x/connect", x_connect),
                    web.get("/x/callback", x_callback),
                    web.get("/api/ui", ui_state)])

    async def _start_bg(_app):
        spawn(xf.run())

    async def _stop_bg(_app):
        for t in list(tasks):
            t.cancel()

    app.on_startup.append(_start_bg)
    app.on_cleanup.append(_stop_bg)
    if agent_token:
        from ..sniper.agent_api import AgentAPI, AgentError

        api = AgentAPI(engine)
        api.ui_sink = ui_broadcast

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
