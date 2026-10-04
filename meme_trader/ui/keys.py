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
                       "trading. A paid endpoint (Helius, QuickNode, ...) avoids the public one's rate limits.", "https",
                       "now"),
    "SOLANA_WS_URL": ("Solana websocket URL", "Where the bot reads pump.fun trades. A flat-rate paid endpoint is steadier "
                      "than the free ones (comma-separate several; the free ones stay as fallbacks).", "wss", "restart"),
    "HELIUS_API_KEY": ("Helius API key", "Wallet-funding lookups with exchange labels, for insider clusters.", "secret",
                       "restart"),
    "PUMPPORTAL_API_KEY": ("PumpPortal API key", "Live trading through PumpPortal (paper trading doesn't need it).",
                           "secret", "restart"),
    "TELEGRAM_BOT_TOKEN": ("Telegram bot token", "Phone alerts: create a bot with @BotFather.", "secret", "restart"),
    "TELEGRAM_ALERT_CHAT_ID": ("Telegram chat id", "Where alerts go: your id from @userinfobot.", "plain", "restart"),
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
                    "testable": name == "ANTHROPIC_API_KEY"})
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
    return next(s for s in status(path) if s["name"] == name)


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
