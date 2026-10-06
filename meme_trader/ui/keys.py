"""API keys and endpoints, set from the dashboard straight into .env.

A key goes from the browser on this machine to the local .env file (mode 600) and the bot's environment; it's
never sent back to the page, logged, journaled or given to the AI agent. The page only learns whether a key is
set and a short hint (the last 4 characters, or an endpoint's host). Live trading's confirmation flag and the
wallet keypair path are deliberately not settable here.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..config import ROOT

ENV = ROOT / ".env"

# name: (label, what it's for, kind, takes effect)
KEYS: dict[str, tuple[str, str, str, str]] = {
    "ANTHROPIC_API_KEY": ("Anthropic API key", "Wakes the AI desk: four Claude personas vote on each entry, billed per "
                          "call on your Anthropic account. The Chat tab uses your Claude Code login instead.", "secret", "now"),
    "ANTHROPIC_WORKSPACE_ID": ("Anthropic workspace ID", "Only for a user key (sk-ant-usr-…), which isn't tied to "
                               "a workspace: the workspace to bill, wrkspc_… from console.anthropic.com → Settings → "
                               "Workspaces. A workspace key (sk-ant-api…) doesn't need it.", "plain", "now"),
    "SOLANA_RPC_URL": ("Solana RPC URL", "Balances, token lookups, the Portfolio tab, insider-cluster checks and live "
                       "trading. A paid endpoint avoids the public one's rate limits. Helius: "
                       "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY (filled in for you when you save a Helius key). "
                       "QuickNode: your endpoint's HTTP Provider URL.", "https", "now"),
    "SOLANA_WS_URL": ("Solana websocket URL", "Where the bot reads pump.fun trades (comma-separate several; the free ones "
                      "stay as fallbacks). Helius: wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY. QuickNode: your "
                      "endpoint's WSS Provider URL. Heads-up: the trade stream is about 20 GB a day, so on a plan billed "
                      "by data or credits a free allowance can run out in days: watch the usage page on day one.",
                      "wss", "restart"),
    "HELIUS_API_KEY": ("Helius API key", "Wallet-funding lookups with exchange labels, for insider clusters. Saving it "
                       "also sets the Solana RPC URL to Helius if that's empty.", "secret", "restart"),
    "PUMPPORTAL_API_KEY": ("PumpPortal API key", "Live trading through PumpPortal (paper trading doesn't need it).",
                           "secret", "restart"),
    "TELEGRAM_BOT_TOKEN": ("Telegram bot token", "Phone alerts: create a bot with @BotFather.", "secret", "now"),
    "TELEGRAM_ALERT_CHAT_ID": ("Telegram chat id", "Where alerts go: your id from @userinfobot.", "plain", "now"),
    "TELEGRAM_CHANNEL_ID": ("Telegram channel or group", "Where your posts and calls go: @yourchannel or a chat id. "
                            "Add the bot as an admin of the channel (or a member of the group).", "plain", "now"),
    "X_API_KEY": ("X API key", "Post to X as your own developer account: console.x.com → your app → Keys and tokens. "
                  "Set the app's permissions to Read and write first. Posts cost ~$0.015 each ($0.20 with a link).",
                  "secret", "now"),
    "X_API_SECRET": ("X API key secret", "The secret next to the X API key.", "secret", "now"),
    "X_ACCESS_TOKEN": ("X access token", "Generate it after setting Read and write (Keys and tokens → Access token).",
                       "secret", "now"),
    "X_ACCESS_SECRET": ("X access token secret", "The secret next to the access token.", "secret", "now"),
    "X_USERNAME": ("X username", "Optional: your @handle without the @, so posted links point at your profile.",
                   "plain", "now"),
    "X_CLIENT_ID": ("X OAuth client ID", "Instead of the four keys: lets the Connect X button sign in any account. "
                    "Add http://127.0.0.1:8787/x/callback to the app's callback URLs.", "plain", "now"),
    "X_CLIENT_SECRET": ("X OAuth client secret", "Only for a confidential (Web App) client.", "secret", "now"),
    "GITHUB_MODELS_TOKEN": ("GitHub Models token", "A cheaper brain for the AI desk: a GitHub token with the models:read "
                            "permission (github.com/settings/tokens). Free, rate-limited.", "secret", "now"),
    "HF_TOKEN": ("Hugging Face token", "A cheaper brain for the AI desk: huggingface.co/settings/tokens (open models).",
                 "secret", "now"),
    "OPENROUTER_API_KEY": ("OpenRouter API key", "A cheaper brain for the AI desk: openrouter.ai/keys (models ending in :free "
                           "cost nothing).", "secret", "now"),
    "X_BEARER_TOKEN": ("X API bearer token", "Optional, paid: X's own API for the social-signal stream. The Desk's X "
                       "feed works without it.", "secret", "restart"),
    "JUPITER_API_KEY": ("Jupiter API key", "Quotes for the older DexScreener bot (python -m meme_trader).", "secret",
                        "restart"),
}
_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=")
PATTERNS = {"ANTHROPIC_WORKSPACE_ID": (re.compile(r"^wrkspc_[A-Za-z0-9]{6,80}$"), "a workspace ID looks like wrkspc_…")}


class KeyError_(ValueError):
    pass


def _file_values(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _hint(kind: str, v: str) -> str:
    if kind in ("https", "wss"):
        hosts = [urlsplit(u.strip()).hostname or "?" for u in v.split(",") if u.strip()]
        return ", ".join(hosts)
    if kind == "plain":
        return v if len(v) <= 40 else v[:12] + "…"
    return "…" + v[-4:] if len(v) >= 12 else "set"


def status(path: Path = ENV) -> list[dict]:
    """What the page may know: set or not, where from, and a hint. Never a value."""
    fv = _file_values(path)
    out = []
    for name, (label, help_, kind, effect) in KEYS.items():
        v = fv.get(name) or os.environ.get(name, "")
        src = ".env" if fv.get(name) else ("environment" if v else "")
        warn = ""
        if name == "ANTHROPIC_API_KEY" and v.startswith("sk-ant-usr") and not (fv.get("ANTHROPIC_WORKSPACE_ID")
                                                                             or os.environ.get("ANTHROPIC_WORKSPACE_ID")):
            warn = "This is a user key: it also needs the Anthropic workspace ID below."
        out.append({"name": name, "label": label, "help": help_, "kind": kind, "effect": effect, "set": bool(v),
                    "source": src, "hint": _hint(kind, v) if v else "", "warn": warn,
                    "testable": name in TESTABLE})
    return out


def _validate(name: str, value: str) -> str:
    if name not in KEYS:
        raise KeyError_(f"{name} can't be set from the dashboard")
    v = (value or "").strip()
    if not v:
        raise KeyError_("empty value: use Remove to clear a key")
    if len(v) > 2000 or re.search(r"[\s\x00-\x1f\x7f\"'#\\]", v):
        raise KeyError_("that doesn't look like a key or URL (spaces, quotes, # and control characters aren't allowed)")
    kind = KEYS[name][2]
    pat = PATTERNS.get(name)
    if pat and not pat[0].match(v):
        raise KeyError_(pat[1])
    if kind in ("https", "wss"):
        scheme = "https" if kind == "https" else "wss"
        for u in v.split(","):
            p = urlsplit(u)
            if p.scheme not in (scheme, "http" if scheme == "https" else "ws") or not p.hostname:
                raise KeyError_(f"expected {scheme}://host/... (comma-separated if several)")
    return v


def _write(path: Path, name: str, value: str | None) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) == name:
            if not done:
                out.append(f"{name}={value or ''}")
                done = True
            continue                                    # a duplicate line would shadow the new value
        out.append(line)
    if not done:
        out.append(f"{name}={value or ''}")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def set_key(name: str, value: str, path: Path = ENV) -> dict:
    v = _validate(name, value)
    _write(path, name, v)
    os.environ[name] = v
    if name == "HELIUS_API_KEY" and not (_file_values(path).get("SOLANA_RPC_URL") or os.environ.get("SOLANA_RPC_URL")):
        rpc = HELIUS_RPC + v                                # one paste gives lookups a real RPC too
        _write(path, "SOLANA_RPC_URL", rpc)
        os.environ["SOLANA_RPC_URL"] = rpc
    return next(s for s in status(path) if s["name"] == name)


HELIUS_RPC = "https://mainnet.helius-rpc.com/?api-key="
TESTABLE = ("ANTHROPIC_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL", "HELIUS_API_KEY")


async def _rpc_ping(url: str) -> tuple[bool, str]:
    import time

    import aiohttp

    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}) as r:
                d = await r.json(content_type=None)
    except Exception as e:                                      # the message never includes the URL (it can hold a key)
        return False, f"no answer ({type(e).__name__})"
    if not isinstance(d, dict) or "result" not in d:
        err = (d.get("error") or {}).get("message", "") if isinstance(d, dict) else ""
        return False, f"it answered with an error{': ' + err[:120] if err else ''}"
    return True, f"answered in {(time.monotonic() - t0) * 1000:.0f} ms (slot {d['result']:,})"


async def _ws_ping(url: str) -> tuple[bool, str]:
    """Connect, take one slot update, unsubscribe: a couple of tiny messages, not the trade stream."""
    import asyncio
    import json
    import time

    import aiohttp

    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
            async with s.ws_connect(url, heartbeat=None) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"})
                sub = None
                async with asyncio.timeout(8):
                    async for m in ws:
                        d = json.loads(m.data)
                        if d.get("id") == 1:
                            if "error" in d:
                                return False, f"it refused the subscription: {str(d['error'].get('message', ''))[:120]}"
                            sub = d.get("result")
                        elif d.get("method") == "slotNotification":
                            break
                if sub is not None:
                    await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "slotUnsubscribe", "params": [sub]})
    except TimeoutError:
        return False, "connected, but no update within 8 s"
    except Exception as e:
        return False, f"couldn't connect ({type(e).__name__})"
    return True, f"connected and streaming: first update after {(time.monotonic() - t0) * 1000:.0f} ms"


async def test_key(name: str) -> tuple[bool, str]:
    if name == "ANTHROPIC_API_KEY":
        return await test_anthropic()
    v = os.environ.get(name) or _file_values(ENV).get(name, "")
    if not v:
        return False, f"no {KEYS.get(name, (name,))[0]} saved"
    if name == "SOLANA_RPC_URL":
        ok, t = await _rpc_ping(v.split(",")[0].strip())
        return ok, ("the RPC " if ok else "the RPC gave ") + t
    if name == "HELIUS_API_KEY":
        ok, t = await _rpc_ping(HELIUS_RPC + v)
        return ok, ("the Helius key works: Helius " if ok else "Helius: ") + t
    if name == "SOLANA_WS_URL":
        ok, t = await _ws_ping(v.split(",")[0].strip())
        return ok, "the first websocket endpoint: " + t
    return False, "there's no test for that one"


async def test_anthropic() -> tuple[bool, str]:
    """One tiny request (a 5-token reply from the smallest model) with the saved key and workspace."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "no Anthropic API key saved"
    try:
        import anthropic

        from ..sniper.desk import client_kwargs

        c = anthropic.AsyncAnthropic(**client_kwargs())
        await c.messages.create(model="claude-haiku-4-5", max_tokens=5, messages=[{"role": "user", "content": "ping"}])
        return True, "the Anthropic key works"
    except Exception as e:                                      # the message never includes the key
        from ..sniper.desk import friendly_error

        return False, friendly_error(f"{type(e).__name__}: {e}")


def clear_key(name: str, path: Path = ENV) -> dict:
    if name not in KEYS:
        raise KeyError_(f"{name} can't be changed from the dashboard")
    _write(path, name, None)
    os.environ.pop(name, None)
    return next(s for s in status(path) if s["name"] == name)
