"""Dashboard chat: talk to Claude about the running bot from the browser, no terminal needed.

Each message runs Claude Code headless (`claude -p`) in the repo. It uses your normal Claude login, so no API
key is needed and it draws on the same usage as Claude Code in a terminal. The bot itself still uses none.
Claude gets the meme-trader MCP tools and nothing else: no shell, no files, no other MCP servers, no
web. Replies stream to every open dashboard tab over the websocket, and the conversation continues across
messages (--resume) until you start a new chat.

Actions (pause, settings, buy, sell, watch, deposits, notes) wait for your click: Claude Code asks the
approve_action tool (--permission-prompt-tool), which lands here as an Approve / Decline card. Reads need
no approval. The bot's own guardrails (agent_api.py) still apply on top.

Pasting a contract address shows its metrics card at once (lookup.py, free); Claude then gives its read.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from ..config import ROOT
from ..journal import DATA
from ..sniper.lookup import TICKER_RE, brief, is_mint, lookup, search_ticker

TOOL_PREFIX = "mcp__meme-trader__"
READ_TOOLS = ["get_status", "get_positions", "get_radar", "get_token", "lookup_token", "get_analytics",
              "get_recent_trades", "get_log", "get_settings", "get_memory",
              "console_help", "ui_action"]        # (ui_action only changes how the page looks: no approval needed)
MODELS = ("", "opus", "sonnet", "haiku")
MAX_MESSAGES = 300
ENV_KEEP = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LANGUAGE", "TERM", "SHELL", "TMPDIR", "TZ",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

CHAT_PROMPT = """You are chatting with the bot's owner in the trading bot's web dashboard (a chat panel, not a
terminal). Work only through the meme-trader tools - you have no shell, files or web here.
Style: short and scannable. Lead with the answer, then a few bullets or a small table. Markdown is rendered
(bold, lists, tables, `code`). SOL to 3 decimals, dollars with K/M. No filler, no disclaimers paragraph.
When the owner asks you to do something, do it with the tools - they approve each action with a button, so
don't ask "should I?" first; if they decline, accept it. "More/less risk" means the risk dial
(set_risk_level, one level at a time unless they say otherwise). When a message contains token metrics from a
lookup (a contract address they pasted), they already see a metrics card: give your read instead of
repeating the numbers - what stands out, the main risks, and whether it suits what the bot trades
(pump.fun bonding-curve tokens; graduation plays). Never promise price moves. Mode is in get_status
(paper = pretend money). A second bot paper-trades young DEX coins on BNB Chain, Base and Solana ("other chains").
You DO have its data: for any question about it, call get_status (field other_chains_paper_bot: today's trades and
P&L, open coins, the real-money checklist) and get_positions (other_chain_positions), then answer with the numbers.
It is paper only; real money there waits for its "Real money?" checklist plus a wallet the owner creates and
their go-ahead.
The console: the owner does most things with a click. For any "how do I / where is / can I / turn off / hide /
delete / add" question about the dashboard, use the console manual (attached to such messages, or call
console_help) and never answer that there's no tool or no way: tell them the clicks. Call console_help first and
answer with the exact clicks or keys it gives (never invent a button). If they ask you to change what the screen
shows - the theme ("make it white" = light), a tab, $ or SOL, open a coin, pin or unpin a chart, the room's speech
bubbles, notifications, or how a character acts or where it stands - just do it with ui_action (no approval
needed), then say in one line what you did and the shortcut for next time (e.g. the t key for the theme).
You're the team's lead (the crowned Claude in the room). You can set a stop loss, take profit or trail on the
owner's positions (set_position_exits), place limit buys and sells or market-cap alerts (place_order), cancel one
(cancel_order), buy (buy_token) and sell (sell_position): each waits for the owner's Approve, so when they ask,
just do it with a one-line reason.
Each owner message starts with what they hold right now ("[Open positions right now: ...]"). "This trade", "this
position", "it" mean the position they hold when there's exactly one: use its mint and act, even if the chat was
about another coin before. With several, use the one they name or the one just discussed, and ask only when it's
truly unclear. Exits in plain words, all in one set_position_exits call: "close it / sell it if it goes over 30%"
= take_profit_pct 30 with take_profit_fraction 1 (everything); "take half at +30%" = fraction 0.5; "if it goes
under 50%" or "if it drops 50%" = stop_loss_pct 50 (half the entry gone); "trail 20" = trail_pct 20."""


# a question about using the dashboard: a how-to form, or a do-verb about something on the screen
HOWTO = re.compile(r"\b(how (do|can|to|would|does)|where('s| is| are| do| can)|is there a way|can i|what does .{1,30} (button|tab|badge|icon) do)\b", re.I)
UI_VERB = re.compile(r"\b(turn|switch|change|hide|show|delete|remove|add|pin|unpin|move|open|close|enable|disable|make)\b", re.I)
UI_NOUN = re.compile(r"\b(theme|dark|light|white|tab|page|panel|button|chart|board|note|corkboard|bulletin|character|bot|bubble|speech|"
                     r"dialog|screen|dashboard|console|layout|room|lounge|lab|view|tracker|units?|amounts|dollars|notifications?|pop-?ups?)\b", re.I)


def _is_howto(text: str) -> bool:
    t = text or ""
    return bool(HOWTO.search(t) or (UI_VERB.search(t) and UI_NOUN.search(t)))


def holdings_note(e) -> str:
    """What the owner holds right now, in front of every message, so "this trade" needs no guessing. Seen 2026-10-06:
    "close this trade if it goes under 50 percent or over 30 percent" was taken to mean a coin from earlier in the
    chat, not the one position open, and the take profit was going to sell only half."""
    rows = []
    for m, pos in list(getattr(e, "positions", {}).items()):
        s = e.tokens.get(m)
        gain = f"{pos.gain_pct(s.curve.price):+.0f}% now" if s is not None and s.price_known else "no price yet"
        ex = {k: v for k, v in (pos.manual or {}).items() if k in ("sl", "tp", "tp_frac", "trail") and v}
        whose = "yours" if pos.source == "manual" else f"the bot's ({pos.source})"
        rows.append(f"{pos.symbol or m[:6]} (mint {m}, {whose}, {gain}, "
                    + (f"exits set: {json.dumps(ex)}" if ex else "no exits set") + ")")
    return "[Open positions right now: " + ("; ".join(rows) if rows else "none") + "]"


def with_manual(prompt: str, text: str) -> str:
    """A question about using the dashboard gets the matching console-manual sections attached, so the answer
    never depends on the model remembering to look (it once answered "I don't have a tool" for a button)."""
    if not _is_howto(text):
        return prompt
    from .manual import search
    found = search(text, top=3, budget=4500)["found"]
    if not found:
        return prompt
    ref = "\n\n".join(f"## {f['section']}\n{f['text']}" for f in found)
    return (f"{prompt}\n\n[Console manual, the sections matching this message (attached automatically). If they ask how, "
            f"answer with these exact clicks or keys; if they ask you to do a screen change, use ui_action. Never say "
            f"there's no way when the manual shows a button.]\n{ref}")


def _now() -> float:
    return time.time()


def _int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


class ChatManager:
    def __init__(self, engine, port: int, cfg: dict | None = None, state_path: Path | None = None):
        cfg = dict(cfg or {})
        self.e = engine
        self.port = port
        self.bin = cfg.get("claude_bin") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        self.timeout_s = float(cfg.get("timeout_s", 600))
        self.approval_timeout_s = float(cfg.get("approval_timeout_s", 600))
        self.path = state_path or DATA / "chat.json"
        self.subs: set[asyncio.Queue] = set()
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()      # strong refs: an answer still exiting must not be collected
        self.pending: dict[str, tuple[asyncio.Future, dict]] = {}
        self.cur: dict | None = None                 # assistant message being written
        self.usage: dict | None = None               # latest Claude usage-limit info from the stream
        self._api_msg = ""                           # API message id of the current stream
        self._streamed: set[str] = set()             # API message ids whose text arrived as deltas
        self.state = self._load(cfg)

    # ------------------------------------------------------------------ state
    def _load(self, cfg: dict) -> dict:
        st = {"session_id": None, "model": cfg.get("model", "") or "", "ask_first": bool(cfg.get("ask_first", True)),
              "auto_read": bool(cfg.get("auto_read_ca", True)), "messages": []}
        try:
            st.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass
        for m in st["messages"]:                     # anything mid-flight when the bot stopped
            if m.get("role") == "assistant" and not m.get("done"):
                m["done"], m["error"] = True, m.get("error") or "interrupted (the bot restarted)"
            for p in m.get("parts", []):
                if p.get("status") in ("running", "pending"):
                    p["status"] = "error" if p["type"] == "tool" else "expired"
                if (p.get("approval") or {}).get("status") == "pending":
                    p["approval"]["status"] = "expired"
        return st

    def save(self) -> None:
        self.state["messages"] = self.state["messages"][-MAX_MESSAGES:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, default=str))
        tmp.replace(self.path)

    @property
    def busy(self) -> bool:
        """An answer is being written. (Claude Code takes a few seconds to exit after its final result; the
        next message doesn't have to wait for that.)"""
        return self.cur is not None

    def public(self) -> dict:
        return {"messages": self.state["messages"][-120:], **self._status()}

    def _status(self) -> dict:
        return {"busy": self.busy, "model": self.state["model"], "ask_first": self.state["ask_first"],
                "auto_read": self.state["auto_read"], "has_session": bool(self.state["session_id"]),
                "available": Path(self.bin).exists(), "usage": self.usage}

    # ------------------------------------------------------------------ events
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def _emit(self, ev: str, **data) -> None:
        msg = {"type": "chat", "ev": ev, **data}
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:                # a stalled tab: drop it rather than grow memory
                self.subs.discard(q)

    def _emit_msg(self, m: dict) -> None:
        self._emit("msg", msg=copy.deepcopy(m))     # as it is NOW: later deltas are sent separately

    def _emit_status(self) -> None:
        self._emit("status", **self._status())

    # ------------------------------------------------------------------ commands from the dashboard
    async def send(self, text: str) -> str:
        """Start answering a message. Returns an error text, or "" when it started."""
        text = str(text or "").strip()[:8000]
        if not text:
            return "empty message"
        if self.busy:
            return "Claude is still answering - wait, or press Stop"
        if not Path(self.bin).exists():
            return f"Claude Code isn't installed where the bot looks ({self.bin}); set sniper.chat.claude_bin"
        user = {"id": uuid.uuid4().hex[:12], "role": "user", "text": text, "ts": _now()}
        self.state["messages"].append(user)
        self._emit_msg(user)
        self.cur = {"id": uuid.uuid4().hex[:12], "role": "assistant", "parts": [], "ts": _now(), "done": False}
        self.state["messages"].append(self.cur)
        self._emit_msg(self.cur)
        self.task = asyncio.create_task(self._answer(text, self.cur))
        self._tasks.add(self.task)
        self.task.add_done_callback(self._tasks.discard)
        self._emit_status()
        return ""

    async def stop(self) -> None:
        for fut, _ in list(self.pending.values()):
            if not fut.done():
                fut.set_result(False)
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self.cur and not self.cur.get("done"):
            self._finish(self.cur, error="stopped")

    async def new_chat(self) -> None:
        if self.busy:
            await self.stop()
        self.state["session_id"] = None
        self.state["messages"] = []
        self.save()
        self._emit("cleared")
        self._emit_status()

    def set_options(self, model=None, ask_first=None, auto_read=None) -> str:
        if model is not None:
            if model not in MODELS:
                return f"unknown model {model!r}"
            self.state["model"] = model
        if ask_first is not None:
            self.state["ask_first"] = bool(ask_first)
        if auto_read is not None:
            self.state["auto_read"] = bool(auto_read)
        self.save()
        self._emit_status()
        return ""

    def decide(self, aid: str, allow: bool) -> str:
        hit = self.pending.get(str(aid))
        if not hit:
            return "that request already expired or was answered"
        if not hit[0].done():
            hit[0].set_result(bool(allow))
        return ""

    # ------------------------------------------------------------------ approval (from the MCP server)
    async def request_approval(self, tool_name: str, tool_input: dict, tool_use_id: str = "") -> dict:
        name = str(tool_name).removeprefix(TOOL_PREFIX)
        msg = self.cur
        if msg is None or msg.get("done"):
            return {"behavior": "deny", "message": "no dashboard chat is waiting for this action"}
        part = next((p for p in reversed(msg["parts"]) if p["type"] == "tool" and
                     (p["tid"] == tool_use_id or (not tool_use_id and p["name"] == name))), None)
        if part is None:
            part = {"type": "tool", "tid": tool_use_id or uuid.uuid4().hex[:8], "name": name, "input": tool_input,
                    "status": "running"}
            msg["parts"].append(part)
        raising = name == "set_risk_level" and _int(tool_input.get("level")) > self.e.risk_level
        if not self.state["ask_first"] and not raising:     # more risk is always the owner's call
            part["approval"] = {"status": "auto"}
            self._emit_msg(msg)
            return {"behavior": "allow"}
        aid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        part["input"] = tool_input or part.get("input")
        part["approval"] = {"aid": aid, "status": "pending", "ts": _now()}
        self.pending[aid] = (fut, part)
        self._emit_msg(msg)
        try:
            allow = await asyncio.wait_for(fut, self.approval_timeout_s)
            part["approval"]["status"] = "approved" if allow else "declined"
        except asyncio.TimeoutError:
            allow = False
            part["approval"]["status"] = "expired"
        finally:
            self.pending.pop(aid, None)
        self._emit_msg(msg)
        if allow:
            return {"behavior": "allow"}
        why = "timed out waiting for the owner" if part["approval"]["status"] == "expired" else "the owner declined it"
        return {"behavior": "deny", "message": f"Not done: {why} in the dashboard."}

    # ------------------------------------------------------------------ one answer
    async def _answer(self, text: str, msg: dict) -> None:
        try:
            prompt = await self._with_card(text, msg)
            if prompt is None:                       # card only (auto-read off)
                self._finish(msg)
                return
            prompt = holdings_note(self.e) + "\n\n" + with_manual(prompt, text)
            for attempt in (0, 1):
                resume = attempt == 0 and bool(self.state["session_id"])
                got_result, err = await self._run(prompt, msg, resume)
                if got_result or not resume or "No conversation found" not in err:
                    break
                self.state["session_id"] = None      # the saved session is gone: start a fresh one
            if not got_result and not msg.get("done"):
                self._finish(msg, error=(err.strip().splitlines() or ["Claude Code exited without a reply"])[-1][:300])
        except Exception as e:                       # never leave the chat stuck "busy"
            self._finish(msg, error=f"{type(e).__name__}: {e}")
        finally:
            if not msg.get("done"):
                self._finish(msg)

    async def _with_card(self, text: str, msg: dict) -> str | None:
        """A pasted contract address gets its metrics card right away (no Claude usage), and the metrics go
        into the prompt so Claude doesn't need a tool call to see them."""
        mints = list(dict.fromkeys(m for m in MINT_RE.findall(text) if is_mint(m)))
        also, tag = [], ""
        if not mints:                                     # or one $TICKER: the most liquid Solana coin trading as it
            tags = list(dict.fromkeys(t.upper() for t in TICKER_RE.findall(text)))
            if len(tags) != 1:
                return text
            tag = tags[0]
            try:
                found = await search_ticker(tag)
            except Exception:                             # DexScreener down: Claude still gets the question
                return text
            if not found:
                return text
            mints, also = [found[0]["mint"]], found[1:]
        if len(mints) != 1:
            return text
        mint = mints[0]
        part = {"type": "card", "mint": mint, "status": "loading"}
        msg["parts"].append(part)
        self._emit_msg(msg)
        try:
            data = await lookup(mint, self.e, watch=True)
            if also:                                      # (a copy: the lookup's cache keeps its own dict)
                data = {**data, "also": also}
            part.update(status="ok", data=data)
        except Exception as e:
            part.update(status="error", error=str(e) or type(e).__name__)
            self._emit_msg(msg)
            return text
        self._emit_msg(msg)
        if not self.state["auto_read"]:
            return None
        facts = json.dumps(brief(data), default=str, separators=(",", ":"))[:12000]
        ask = "What do you make of this token?" if text.strip() == mint or (tag and text.strip().upper() == f"${tag}") else text
        return f"{ask}\n\n[Metrics for {mint}, already shown to me as a card]\n{facts}"

    def _argv(self, resume: bool) -> list[str]:
        server = {"type": "stdio", "command": sys.executable, "args": ["-m", "meme_trader.sniper.mcp_server"],
                  "env": {"MEME_TRADER_CHAT": "1", "MEME_TRADER_URL": f"http://127.0.0.1:{self.port}"}}
        argv = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--tools", "", "--strict-mcp-config",
                "--mcp-config", json.dumps({"mcpServers": {"meme-trader": server}}),
                "--permission-prompt-tool", TOOL_PREFIX + "approve_action", "--append-system-prompt", CHAT_PROMPT]
        if self.state["model"]:
            argv += ["--model", self.state["model"]]
        if resume:
            argv += ["--resume", self.state["session_id"]]
        return argv + ["--allowedTools", *(TOOL_PREFIX + t for t in READ_TOOLS)]

    def _env(self) -> dict:
        """Only what Claude Code needs to run: none of the bot's secrets (.env keys, RPC URLs with keys), and
        no ANTHROPIC_API_KEY - with one set, Claude Code would bill the API instead of your Claude plan."""
        env = {k: v for k, v in os.environ.items() if k in ENV_KEEP or k.startswith(("LC_", "XDG_"))}
        env.setdefault("HOME", str(Path.home()))
        env["PATH"] = os.pathsep.join(filter(None, [env.get("PATH", "/usr/bin:/bin"), str(Path(self.bin).parent)]))
        env["MCP_TOOL_TIMEOUT"] = str(int((self.approval_timeout_s + 120) * 1000))   # approvals can take minutes
        return env

    async def _run(self, prompt: str, msg: dict, resume: bool) -> tuple[bool, str]:
        self.proc = proc = await asyncio.create_subprocess_exec(
            *self._argv(resume), cwd=str(ROOT), env=self._env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024)
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        err_task = asyncio.create_task(proc.stderr.read())
        got_result = False
        try:
            async with asyncio.timeout(self.timeout_s):
                async for line in proc.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if self._on_event(d, msg):
                        got_result = True
                        break                        # the answer is complete; the process exits on its own
        except TimeoutError:
            proc.kill()
            self._finish(msg, error=f"timed out after {self.timeout_s:.0f}s")
        try:
            await asyncio.wait_for(proc.wait(), 20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        finally:
            if self.proc is proc:
                self.proc = None
        err = (await err_task).decode(errors="replace")[-3000:]
        return got_result, err

    # ------------------------------------------------------------------ stream-json -> chat parts
    def _text(self, msg: dict, delta: str) -> None:
        parts = msg["parts"]
        if not parts or parts[-1]["type"] != "text":
            parts.append({"type": "text", "text": ""})
            self._emit_msg(msg)
        parts[-1]["text"] += delta
        self._emit("delta", id=msg["id"], text=delta)

    def _tool_part(self, msg: dict, tid: str, name: str, inp: dict | None) -> dict:
        part = next((p for p in msg["parts"] if p["type"] == "tool" and p["tid"] == tid), None)
        if part is None:
            part = {"type": "tool", "tid": tid, "name": name.removeprefix(TOOL_PREFIX), "input": inp or {},
                    "status": "running"}
            msg["parts"].append(part)
        elif inp:
            part["input"] = inp
        return part

    def _on_event(self, d: dict, msg: dict) -> bool:
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            if d.get("session_id"):
                self.state["session_id"] = d["session_id"]
            srv = next((s for s in d.get("mcp_servers") or [] if s.get("name") == "meme-trader"), None)
            if srv and srv.get("status") != "connected":
                msg["warning"] = f"bot tools {srv.get('status')} - is the bot's agent API on?"
                self._emit_msg(msg)
        elif t == "system" and d.get("subtype") == "thinking_tokens":
            self._emit("thinking", id=msg["id"], tokens=d.get("estimated_tokens"))
        elif t == "rate_limit_event":
            info = d.get("rate_limit_info") or {}
            w = info.get("unifiedWindows") or {}
            self.usage = {k: {"used": (v or {}).get("utilization"), "resets_at": (v or {}).get("resetsAt")}
                          for k, v in w.items()} or None
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                msg["warning"] = f"Claude usage limit: {info.get('status')}"
            self._emit_status()
        elif t == "stream_event":
            ev = d.get("event") or {}
            et = ev.get("type")
            if et == "message_start":
                self._api_msg = (ev.get("message") or {}).get("id", "")
            elif et == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
                b = ev["content_block"]
                self._tool_part(msg, b.get("id", ""), b.get("name", ""), None)
                self._emit_msg(msg)
            elif et == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                self._streamed.add(self._api_msg)
                self._text(msg, ev["delta"].get("text", ""))
        elif t == "assistant":
            m = d.get("message") or {}
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    self._tool_part(msg, b.get("id", ""), b.get("name", ""), b.get("input"))
                    self._emit_msg(msg)
                elif b.get("type") == "text" and m.get("id") not in self._streamed and b.get("text"):
                    self._text(msg, b["text"])
        elif t == "user":
            for b in (d.get("message") or {}).get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                part = next((p for p in msg["parts"] if p["type"] == "tool" and p["tid"] == b.get("tool_use_id")), None)
                if part is None:
                    continue
                c = b.get("content")
                text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                refused = text.startswith("REFUSED:")
                part["status"] = "error" if b.get("is_error") or refused else "ok"
                part["preview"] = re.sub(r"</?tool_use_error>", "", text)[:600]
                self._emit_msg(msg)
        elif t == "result":
            if d.get("session_id"):
                self.state["session_id"] = d["session_id"]
            meta = {"duration_s": round((d.get("duration_ms") or 0) / 1000, 1), "turns": d.get("num_turns")}
            self._finish(msg, error=(d.get("result") or "error")[:400] if d.get("is_error") else "", meta=meta)
            return True
        return False

    def _finish(self, msg: dict, error: str = "", meta: dict | None = None) -> None:
        if msg.get("done"):
            return
        msg["done"] = True
        if error:
            msg["error"] = error
        if meta:
            msg["meta"] = meta
        for p in msg["parts"]:
            if p.get("status") == "running":
                p["status"] = "error" if error else "ok"
        self.save()
        self._emit_msg(msg)
        if self.cur is msg:
            self.cur = None
            self._emit_status()
