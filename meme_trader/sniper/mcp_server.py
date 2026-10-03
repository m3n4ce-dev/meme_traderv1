"""MCP server: the running bot's AI-operator tools, for Claude Code (or any MCP client).

    claude                                  # in the repo: .mcp.json starts this server automatically
    python -m meme_trader.sniper.mcp_server # what it runs (stdio)

It forwards each tool call to the bot's local agent API (POST /api/agent on the dashboard port) with
the per-run secret from data/agent.token. All guardrails are enforced inside the bot (agent_api.py),
not here, so they hold whichever client calls them.
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..config import ROOT

URL = os.environ.get("MEME_TRADER_URL", "http://127.0.0.1:8787")
TOKEN_FILE = ROOT / "data" / "agent.token"

INSTRUCTIONS = """You are the operator of a running pump.fun trading bot (Solana memecoins), normally in PAPER
mode (pretend money). The bot's code trades on its own in milliseconds-to-seconds; your job is one level up:
judge the market regime, decide which strategies should run and how much risk to take, investigate tokens,
open or close positions when you have a concrete reason, and explain what you did.

Ground every decision in tool data (get_status first: feed health, P&L, blocks; then positions, radar,
analytics). Most pump.fun tokens go to zero; a few graduate. Prefer doing nothing over acting on a hunch.
Every action needs a short, specific reason - it is journaled and shown on the dashboard.

Limits are enforced by the bot, not by you: you may lower risk freely (smaller sizes, fewer positions,
tighter loss limit, strategies off, pause) but can't exceed the owner's configured limits, re-enable a
strategy the owner turned off, switch to live, save settings, or press the kill switch. Setting changes
last until the bot restarts. If a tool refuses, report why instead of trying to work around it."""

mcp = MCPServer("meme-trader", instructions=INSTRUCTIONS)
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
TRADE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


async def _call(tool: str, **args) -> str:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return ("The bot isn't running with its agent API (no data/agent.token). Start it: "
                "systemctl --user start meme-sniper  (or scripts/start.sh paper)")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{URL}/api/agent", json={"tool": tool, "args": args},
                             headers={"X-Agent-Token": token})
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return f"Can't reach the bot at {URL} ({type(e).__name__}: {e}). Is it running?"
    if "error" in d:
        return f"REFUSED: {d['error']}"
    return json.dumps(d.get("result"), indent=1, default=str)


# ------------------------------------------------------------------ read
@mcp.tool(annotations=READ)
async def get_status() -> str:
    """Overview: mode, equity/cash/day P&L, whether entries are blocked and why, feed health (endpoint,
    % of trades missing), which strategies are on, defense mode, session stats by strategy, and how many
    agent buys are left this hour. Call this first."""
    return await _call("status")


@mcp.tool(annotations=READ)
async def get_positions() -> str:
    """Open positions: gain %, peak gain %, time held, strategy that opened it, curve progress."""
    return await _call("positions")


@mcp.tool(annotations=READ)
async def get_radar(limit: int = 20, status: str = "") -> str:
    """Tokens the bot is tracking, best first: age, curve %, market cap, buyers, rule score, social links,
    bundle/dev %, and the bot's decision (status). `status` filters by text, e.g. "watching" or "rejected"."""
    return await _call("radar", limit=limit, status=status)


@mcp.tool(annotations=READ)
async def get_token(mint: str) -> str:
    """Everything about one token: gate checklist (which safety checks pass/fail), holders and their
    funders, recent trades, price history, social links, insider-cluster report, and our position if held."""
    return await _call("token", mint=mint)


@mcp.tool(annotations=READ)
async def get_analytics() -> str:
    """Performance analytics: KPIs, results by strategy/exit/hour, gate audit (would rejected tokens have
    doubled?), bootstrap confidence in the edge, drawdown, and plain-English highlights."""
    return await _call("analytics")


@mcp.tool(annotations=READ)
async def get_recent_trades(n: int = 20) -> str:
    """The last n closed trades (newest first) with P&L, peak gain and exit reason."""
    return await _call("trades", n=n)


@mcp.tool(annotations=READ)
async def get_log(n: int = 30) -> str:
    """The bot's recent activity log (newest first): buys, sells, errors, setting changes, agent actions."""
    return await _call("log", n=n)


@mcp.tool(annotations=READ)
async def get_settings() -> str:
    """Adjustable settings with current values and ranges, plus the limits on what you may change."""
    return await _call("settings")


# ------------------------------------------------------------------ act
@mcp.tool(annotations=CHANGE)
async def set_setting(key: str, value: str, reason: str) -> str:
    """Change one live setting (see get_settings for keys), e.g. key="late.enabled" value="false", or
    key="sizing.max_usd" value="20". Lowering risk is always allowed; raising it above the owner's
    configured value is refused. Lasts until the bot restarts."""
    return await _call("set_setting", key=key, value=value, reason=reason)


@mcp.tool(annotations=CHANGE)
async def pause_entries(reason: str) -> str:
    """Stop all NEW entries (open positions keep being managed and exited normally)."""
    return await _call("pause", reason=reason)


@mcp.tool(annotations=CHANGE)
async def resume_entries(reason: str) -> str:
    """Allow new entries again after pause_entries."""
    return await _call("resume", reason=reason)


@mcp.tool(annotations=TRADE)
async def sell_position(mint: str, reason: str, fraction: float = 1.0) -> str:
    """Sell an open position now (fraction 0-1 of it, default all)."""
    return await _call("sell", mint=mint, reason=reason, fraction=fraction)


@mcp.tool(annotations=TRADE)
async def buy_token(mint: str, usd: float, reason: str) -> str:
    """Open a position in a tracked token for `usd` dollars. Goes through the bot's normal risk checks
    (daily loss, feed health, cash reserve, max positions, $ cap) and a per-hour agent buy limit; refused
    if the creator sold or the token is near graduation. Exits then follow the bot's standard rules."""
    return await _call("buy", mint=mint, usd=usd, reason=reason)


@mcp.tool(annotations=CHANGE)
async def watch_token(mint: str, reason: str) -> str:
    """Start tracking a pump.fun token the bot isn't following (e.g. one you found mentioned elsewhere)."""
    return await _call("watch", mint=mint, reason=reason)


@mcp.tool(annotations=CHANGE)
async def add_note(text: str) -> str:
    """Write an observation or decision rationale to the bot's journal and dashboard log."""
    return await _call("note", text=text)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
