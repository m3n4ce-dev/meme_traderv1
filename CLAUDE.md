# meme_traderv1

Paper-first pump.fun trading bot for Solana (main engine: `meme_trader/sniper/`) plus an older
DexScreener momentum bot (`meme_trader/agents/`). Read `README.md` for the overview and
`docs/BUILD_LOG.md` (newest first) for every decision and measurement.

## Developing
- Tests: `.venv/bin/python -m pytest -q` (all must pass). Add a regression test with every fix.
- Settings: `config/params.example.yaml` (all options, commented) merged with the local, git-ignored
  `config/params.yaml`. `meme_trader/config.py::validate_sniper` must accept every default.
- The running bot is a user service: `systemctl --user status|restart meme-sniper`. Don't edit the
  checkout it runs from mid-change; restart it after merging.
- Never commit `.env`, `config/params.yaml`, `data/`, or anything with a key or wallet secret.

## Operating the running bot (MCP tools `mcp__meme-trader__*`)
The `meme-trader` MCP server (`.mcp.json`) talks to the running bot. You are its operator, one level
above the code: the engine trades on its own in seconds; you judge regime and risk, investigate tokens,
and act only with a concrete reason.

1. Start with `get_status`: feed health (`gap_pct`, `degraded_reason`), P&L, why entries are blocked.
   If the feed is degraded, don't trade on its data.
2. Then `get_positions`, `get_radar`, `get_analytics` (and `get_token` for anything you'd act on).
3. Act sparingly. Prefer pausing or lowering risk over adding it. Every action needs a specific reason,
   which is journaled and shown on the dashboard.
4. Report what you saw, what you did, and why, in a few lines.

Facts to keep in mind (paper results, 2026-10-03; see BUILD_LOG #12-#13):
- Graduation plays (`late`) were the only strategy with positive results; the early sniper and $1
  callouts lost and are off in the owner's config. The edge is small and concentrated in a few big wins.
- Most pump.fun tokens go to zero. A token whose creator sold, or that is about to graduate, is refused.

The bot enforces the limits, not you: you can't raise sizing, positions, loss limit or stop loss above
the owner's configured values, re-enable a strategy the owner turned off, switch to live, save settings,
or press the kill switch. Setting changes last until restart. If a tool refuses, say so - don't look for
a way around it (e.g. editing config files or restarting the bot to change limits).
