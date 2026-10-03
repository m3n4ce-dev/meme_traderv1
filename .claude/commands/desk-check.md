---
description: Review the running trading bot (feed, P&L, positions, radar, analytics) and act within its limits
---
Do a desk check of the running meme-trader bot using the `meme-trader` MCP tools.

1. `get_status`. If the bot isn't reachable, say so and stop. If the feed is degraded, report it and
   don't trade on its data.
2. `get_positions` and `get_recent_trades` (n=10): anything that needs an exit now, and why?
3. `get_radar` (limit 15) and, for at most 3 interesting tokens, `get_token`.
4. `get_analytics`: which strategy is working, the gate audit, and whether the edge looks real yet.
5. Decide. Change something only with a specific, data-backed reason: pause/resume entries, lower risk,
   turn a strategy off, sell a position, or (rarely) buy a token with a strong case. Doing nothing is
   a valid outcome.
6. Reply in at most 10 lines: market read, what you changed and why (or "no changes"), and what to
   watch next.

$ARGUMENTS
