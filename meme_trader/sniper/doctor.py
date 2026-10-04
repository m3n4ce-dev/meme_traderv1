"""`python -m meme_trader.sniper doctor` - checks everything the bot needs and says how to fix what's missing.
Never prints secret values, only whether they are set."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import time

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
OK, WARN, FAIL = "\033[32m OK \033[0m", "\033[33mWARN\033[0m", "\033[31mFAIL\033[0m"


def valid_pubkey(s: str) -> bool:
    try:
        n = 0
        for ch in s:
            n = n * 58 + B58.index(ch)
        return len(n.to_bytes((n.bit_length() + 7) // 8, "big")) == 32
    except ValueError:
        return False


def line(status: str, what: str, fix: str = "") -> None:
    print(f"[{status}] {what}" + (f"\n        -> {fix}" if fix and status != OK else ""))


async def _pumpportal() -> str:
    import aiohttp

    async with aiohttp.ClientSession() as s, s.ws_connect("wss://pumpportal.fun/api/data", timeout=10) as ws:
        await ws.send_json({"method": "subscribeNewToken"})
        for _ in range(3):
            msg = await asyncio.wait_for(ws.receive(), 15)
            d = json.loads(msg.data)
            if d.get("txType") == "create":
                return f"live launch seen: {d.get('symbol')}"
        return "connected"


async def _trade_logs(ws_url: str) -> str:
    import aiohttp

    from .feeds import PUMP_PROGRAM, SolanaTradeFeed

    async with aiohttp.ClientSession() as s, s.ws_connect(ws_url, timeout=10, max_msg_size=0) as ws:
        await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                            "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]})
        n, end = 0, time.time() + 15
        while time.time() < end and n < 20:
            d = json.loads((await asyncio.wait_for(ws.receive(), 15)).data)
            if "params" in d:
                n += len(SolanaTradeFeed.parse_logs(d["params"]["result"]["value"], 0))
        if not n:
            raise RuntimeError("no pump.fun trades in 15 s")
        return f"{n} pump.fun trades decoded"


def run(params) -> None:
    print("meme_trader doctor\n")
    line(OK if sys.version_info >= (3, 10) else FAIL, f"Python {sys.version.split()[0]}", "install Python 3.11+")
    for mod, why, req in (("aiohttp", "live feed + dashboard", True), ("httpx", "HTTP", True), ("yaml", "config", True),
                          ("solders", "live trading (signing)", False), ("anthropic", "AI desk / review", False),
                          ("telethon", "Telegram signals", False)):
        have = importlib.util.find_spec(mod) is not None
        line(OK if have else (FAIL if req else WARN), f"package {mod} ({why})",
             "" if have else f".venv/bin/pip install {'pyyaml' if mod == 'yaml' else mod}")

    pk = params.wallet.pubkey
    line(OK if valid_pubkey(pk) else FAIL, f"wallet.pubkey {pk or '(empty)'}",
         "" if valid_pubkey(pk) else "set wallet.pubkey in config/params.yaml")

    on_chain = params.sniper.feed.trades != "pumpportal"
    env = {
        "PUMPPORTAL_API_KEY": "not needed: trades come from Solana logs (sniper.feed.trades: solana)" if on_chain
        else "REQUIRED for the live feed: per-token trades + wallet streams (copy trading)",
        "ANTHROPIC_API_KEY": "AI desk + review agent",
        "SOLANA_RPC_URL": "live trading + insider-cluster lookups (use a paid RPC, e.g. Helius)",
        "SOLANA_KEYPAIR_PATH": "live trading (bot wallet keypair file)",
        "TELEGRAM_API_ID": "Telegram call ingestion", "TELEGRAM_API_HASH": "Telegram call ingestion",
        "X_BEARER_TOKEN": "X call ingestion",
        "HELIUS_API_KEY": "wallet-funding lookups with exchange labels (else SOLANA_RPC_URL is used)",
        "TELEGRAM_BOT_TOKEN": "phone alerts / posting callout cards",
        "TELEGRAM_ALERT_CHAT_ID": "phone alerts (your own chat id)",
    }
    for k, why in env.items():
        need = k == "PUMPPORTAL_API_KEY" and not on_chain
        ok = os.environ.get(k) or (k == "PUMPPORTAL_API_KEY" and on_chain)
        line(OK if ok else (FAIL if need else WARN), f"{k} {'set' if os.environ.get(k) else 'not set'} - {why}",
             "" if os.environ.get(k) else "add it to .env (see .env.example)")

    try:
        line(OK, "PumpPortal websocket: " + asyncio.run(_pumpportal()))
    except Exception as e:
        line(FAIL, f"PumpPortal websocket unreachable ({type(e).__name__})", "check internet / firewall")
    if on_chain:
        from .feeds import ws_urls
        urls = ws_urls(params.sniper.feed.ws_url)
        ws_url = urls[0]
        print(f"         trade-log endpoints, in order: {', '.join(u.split('/')[2].split('?')[0] for u in urls)}")
        try:
            line(OK, f"trade logs {ws_url.split('/')[2].split('?')[0]}: " + asyncio.run(_trade_logs(ws_url)))
        except Exception as e:
            line(FAIL, f"trade logs unreachable at {ws_url.split('/')[2].split('?')[0]} ({type(e).__name__}: {e})",
                 "check internet, or set SOLANA_WS_URL in .env to another Solana RPC websocket")

    rpc = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
    try:
        import httpx

        bal = httpx.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pk]}, timeout=10).json()
        sol = bal["result"]["value"] / 1e9
        need = params.sniper.capital.starting_sol
        line(OK if sol >= need else WARN, f"RPC reachable; {pk[:6]}… holds {sol:.4f} SOL (sniper budget {need})",
             "" if sol >= need else "fund the wallet or lower sniper.capital.starting_sol before going live")
    except Exception as e:
        line(WARN, f"RPC check failed ({type(e).__name__})", "set SOLANA_RPC_URL to a working RPC endpoint")

    kp = os.environ.get("SOLANA_KEYPAIR_PATH")
    if kp:
        try:
            mode = os.stat(kp).st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                line(WARN, "keypair file is readable by other users", f"chmod 600 {kp}")
            from solders.keypair import Keypair

            with open(kp) as f:
                got = str(Keypair.from_bytes(bytes(json.load(f))).pubkey())
            line(OK if got == pk else FAIL, f"keypair pubkey {got[:6]}… {'matches' if got == pk else '!= wallet.pubkey'}",
                 "" if got == pk else "point SOLANA_KEYPAIR_PATH at the wallet in wallet.pubkey (or update the pubkey)")
        except Exception as e:
            line(FAIL, f"keypair unreadable ({type(e).__name__})", "check SOLANA_KEYPAIR_PATH")

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic

            from .desk import client_kwargs

            anthropic.Anthropic(**client_kwargs()).models.retrieve(params.sniper.desk.model)
            line(OK, f"Anthropic API reachable, model {params.sniper.desk.model} available")
        except Exception as e:
            line(FAIL, f"Anthropic API check failed ({type(e).__name__})", "check ANTHROPIC_API_KEY")
    n = len(params.sniper.copy.leaders)
    line(OK if n else WARN, f"copy trading: {n} leader wallet(s) configured",
         "" if n else "add wallets under sniper.copy.leaders (find some with the `leaders` command)")
    print("\nDemo (scripts/start.sh demo) needs nothing. Paper trading on the real market needs the PumpPortal\n"
          "websocket plus trade data (the trade logs row, or PUMPPORTAL_API_KEY with sniper.feed.trades: pumpportal).\n"
          "Live trading needs every row OK.")
