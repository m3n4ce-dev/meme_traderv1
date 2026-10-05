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
- Strategy research follows docs/RESEARCH.md: one policy file per idea (`research/policies/`), evaluated with
  `python -m meme_trader.sniper research eval`. Never edit a frozen policy or tune on its holdout (data after
  `frozen_at`); a new idea is a new version. Report net results with the baselines and uncertainty, not just P&L.

## Operating the running bot (MCP tools `mcp__meme-trader__*`)
The same tools power the dashboard's Chat tab (`meme_trader/ui/chat.py` runs `claude -p` with them; actions
wait for Approve in the browser). In a terminal, the `meme-trader` MCP server (`.mcp.json`) talks to the running bot. You are its operator, one level
above the code: the engine trades on its own in seconds; you judge regime and risk, investigate tokens,
and act only with a concrete reason.

1. Start with `get_status`: feed health (`gap_pct`, `degraded_reason`), P&L, why entries are blocked.
   If the feed is degraded, don't trade on its data.
2. Then `get_positions`, `get_radar`, `get_analytics` (and `get_token` for anything you'd act on).
   For any contract address, tracked or not, `lookup_token` gives market data, holders and risk flags.
   `add_paper_funds` tops up the paper balance, only when the owner asks.
3. Act sparingly. Prefer pausing or lowering risk over adding it. Every action needs a specific reason,
   which is journaled and shown on the dashboard.
4. Report what you saw, what you did, and why, in a few lines.

Facts to keep in mind (paper results, 2026-10-03 to 10-04; see BUILD_LOG #32 and the Analytics edge check):
- Graduation plays (`late`) are the only strategy with positive results: +6.5% per SOL over 162 paper trades
  ("promising, not proven": its best three trades made 92% of the profit, 1.5 days, settings changed 8 times).
  The early sniper lost (-13.8%) and is off. The frozen `graduation-v1` test decides (~2026-10-17).
- Copying KOLs (kolscan.io wallets) loses: they hold a median 34 s, and following 1-2.5 s late lost ~13% a trade
  (BUILD_LOG #39). Volume/transaction-count rules haven't shown an edge on unseen days either.
- Market data: free public RPC first (reconnect on hang-ups), PublicNode as fallback, the owner's Helius websocket
  as a budgeted outage backup (feed.backup_mb_per_day). The stream is ~400-800 MB/h. Don't add subscription probes
  against these endpoints.
- Most pump.fun tokens go to zero. A token whose creator sold, or that is about to graduate, is refused.
- Manual trading is the owner's: never resize or block it. Positions the owner hands to the bots "ride"
  (half out at 2x, a 30% trail, a stop 40% down). Lifting a kill-switch halt is the owner's call only.

The bot enforces the limits, not you: you can't raise sizing, positions, loss limit or stop loss above
the owner's configured values, re-enable a strategy the owner turned off, switch to live, save risk
settings, or press the kill switch. Setting changes last until restart. If a tool refuses, say so - don't look for
a way around it (e.g. editing config files or restarting the bot to change limits).
