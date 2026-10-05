# Proving an edge in public, and how the project could split into repos

October 2026. Written for showing results to trading groups: what counts as proof, how the call ledger provides it, and a plan for splitting the work into a few clean public repos.

## What a group will (and should) believe

| Claim | Believable when |
|---|---|
| "I called it" | The call was published **before** the move, with a timestamp and the market cap at that moment. |
| "My calls make money" | Every call is on the record, not just the winners, and results are measured the same way for all of them. |
| "The bot has an edge" | It was tested on data it was never tuned on (a frozen strategy plus a holdout), with a large enough sample. |

Screenshots of winners prove nothing; anyone can crop out the losers. A tamper-evident ledger plus fixed measurement rules is what separates a track record from marketing.

## The call ledger (in this repo)

- **Where:** `data/calls.jsonl`, append-only. Outcomes are in `data/calls_outcomes.json` and can be recomputed from market data.
- **Who calls:** you, from the 📣 Call button (Live → Manual trade, Analytics → Calls & track record, the coin menu, Ctrl+K). The bot's own entries go in as caller `bot`.
- **Tamper-evident:** each record stores the SHA-256 of the record before it. Editing, deleting or reordering any call changes every hash after it.
- **Publishing the head:** the newest hash pins the whole history. Analytics → Calls & track record → *Publish* (or `python -m meme_trader.social proof --to x,telegram`) posts it and records the anchor in the chain.
- **Verifying:** anyone with the exported file runs `python -m meme_trader.sniper calls verify --file calls.jsonl` and compares the head with the published one.
- **Scoring, honestly:**
  - The market cap 5 min, 1 h, 6 h and 24 h after the call; the stats use these fixed-hold returns minus ~5% round-trip costs.
  - The peak multiple within 24 h (what groups quote) is shown as well, labelled as sampled, since nobody sells the exact top.
  - Prices come from the live curve while a coin is on pump.fun, and from DexScreener after it graduates.
- **What it can't prove:** that you didn't make calls somewhere else and keep only this ledger, or that you hold what you call. Post each call publicly as you make it (the Call form can post to X and Telegram in one click), and keep one ledger.

### How much data before claiming an edge

The 1-hour median after costs and the win rate need **100+ calls** before they mean much: memecoin returns are dominated by a few huge outliers, so small samples swing wildly. The bot's own verdict comes from the frozen research test (`research final graduation-v1`, about 2026-10-17), on data it was never tuned on.

## Splitting into repos (proposal: nothing created yet)

| Repo | What goes in | Public? | Comes from |
|---|---|---|---|
| **meme_traderv1** (this one) | The bot, the dashboard, the room | Public, with no wallet addresses or keys | today |
| **wallet-tracker** | Smart-wallet discovery with pre-registered tests: the recorder, the study rules (wallets-v1), freeze/eval, reports | Public code; recorded data stays private (size, and it identifies wallets) | `meme_trader/wallets/`, `deploy/meme-wallets.service`, docs/WALLETS.md |
| **research** (the proof repo) | Frozen policies and their lock files, holdout verdicts, the published call-ledger heads, a verify script, monthly reports | Public: this is the one a group checks | `meme_trader/sniper/research.py`, `calls.py`, research outputs |
| **desk-agents** (custom agent) | The persona desk (votes, vetoes, note discussions), the MCP server and operator guardrails, as a reusable library | Public | `sniper/desk.py`, `mcp_server.py`, `agent_api.py` guardrails |
| **telegram-bot** | A group bot: /call, /record, /verify, leaderboard, alerts; posts calls and proofs; reads the ledger | Public code, private token | `ui/social.py` (Telegram half), `calls.py` |

**Order that makes sense:**

1. Keep the ledger running here for a few weeks first, so the research repo launches with history.
2. **research** next: it's the credibility anchor.
3. **telegram-bot**: it plugs straight into groups.
4. **wallet-tracker** once wallets-v1 has its verdict (around 2026-11-01).
5. **desk-agents** whenever.

Each new repo needs its own README with real, sample-sized numbers, a license, and CI that runs the verify script on the published ledger.
