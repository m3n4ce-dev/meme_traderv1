# Build Log — meme_traderv1

A running record of decisions, research, parameters and status. Newest entries at the top.

---

## 2026-10-03 — Entry #17: Risk dial, self-updating dashboard, locked policy settings

### Owner input
- "We need to have a risk dial or something or be able to tell the agent to take more risk etc"
- "The UI looks the same on my end": the server was already sending the new page, but an open tab never reloads itself; it only reconnects its websocket.

### Built
- **Risk dial** (`Engine.set_risk`, `sniper.risk.level` / `max_level`):
  - five levels scale trade size, open positions and the daily loss limit around the configured settings (= Normal): x0.5 / x1 / x1.5 / x2 / x3 (positions x2.5 at Max);
  - the kill switch, stop losses, entry rules and the 3%-of-curve cap don't move;
  - the dashboard's change is saved to `params.yaml`;
  - saving settings writes the Normal values, so a restart doesn't scale twice;
  - an owner edit to a scaled setting moves the Normal baseline; an agent edit doesn't.
- **Claude and the dial:**
  - new tool `set_risk_level`;
  - the agent's ceilings follow the owner's dial level;
  - lowering is free; raising never goes above `max_level`, and the dashboard chat **always** shows an Approve card for it, even with "Ask before actions" off;
  - in a terminal it's on the "ask" list.
- **UI:**
  - a dial on Live and a table on Controls, each with a confirm showing exact numbers (a stronger warning in live mode);
  - a risk badge in the header;
  - "More risk" / "Less risk" quick buttons in Chat.
- **Self-updating page:** the server stamps the page with a hash of its file and sends a `hello` with the current hash on every websocket connect. An open page that finds itself out of date reloads.
- **Locked policy settings:**
  - `research freeze` now saves `<policy>.lock.json`, the complete settings snapshot, and a frozen policy replays with it;
  - adding the dial's settings would otherwise have changed graduation-v1's hash and voided its holdout;
  - graduation-v1's lock was rebuilt from the unchanged config and reproduces its original signature (0f0fb20e990b);
  - editing a frozen policy's file is still refused.

Tests: 196 pass, including 8 new ones in `tests/test_risk_dial.py`.

---

## 2026-10-03 — Entry #16: Research pipeline: one frozen strategy, clean data, an evaluation that can say no

### Why
An external review ("Astra") made the case that the bot has no demonstrated edge, only a few lucky paper trades. Its prescription: concentrate on one written-down strategy, make the data reflect what the bot actually knew, and evaluate in a way that can reject a favourite idea. The owner said yes to steps 1–3.

### Measured while checking the review's claims
- **Fees on chain:** the TradeEvent's own fee fields show **0.95% protocol + 0.30% creator = 1.25%**, as assumed. Some trades pay 0.
- **Feed delay:** receive time vs the chain tip, in slots (clock NTP-synced):

  | Endpoint | Median | p99 |
  |---|---|---|
  | PublicNode `logsSubscribe` (confirmed) | **12.3 s** | 13.6 s |
  | PublicNode (processed) | 12.8 s | 13.7 s |
  | api.mainnet-beta (confirmed) | 1.0 s | 2.1 s |

  The watchdog only checked for missing trades, and `.env` listed PublicNode first, retried every 30 min. So parts of the paper run and the recordings were ~12 s stale. For a strategy that often holds ~15 s, that's decisive.
- **Mayhem agent:** wallet `BwWK17…` made **22.2% of all recorded trades** (298k of 1.35M). The tracker counted it as an ordinary buyer and holder; its 1B-token bag made Mayhem tokens look 100% concentrated.
  - On this data, no Mayhem token passed the other checks anyway (serial deployer 167, red flags 151, momentum 35 of 461 evaluated), so results are identical with or without the fix.
  - Mayhem tokens are 34% of tokens traded and 26% of those reaching the 55% window.
- **Data on hand:** one day, 16.3 h, 30.6k launches, 1.36M trades.
- **Paper journal:** 36 graduation trades, +0.585 SOL. Without the top 3 of each session: +0.02 and −0.34 SOL.

### Built
- **Recordings:**
  - each trade now carries its `slot`, on-chain `chain_ts`, `fee_bps` and `creator_fee_bps`, decoded from the TradeEvent (offsets checked on live data);
  - a `health` record each minute: host, gap %, lag, degraded reason, SOL/USD.
- **Watchdog:** median delay above `feed.max_lag_s` (5 s) counts as degraded: entries pause and the endpoint is dropped. Lag is shown in the snapshot.
- **`sniper.market.non_organic_wallets`** (default: the Mayhem agent):
  - their trades move the price but count as no demand, no buyer and no holder;
  - a Mayhem token is detected when the agent trades it, and its market cap uses the 2B supply (`TokenState.market_cap_sol`).
- **Replays:**
  - honour recorded health: no entries where the live feed was degraded, and the recorded SOL price is used;
  - `late.entry_mode: window` gives a no-filter baseline;
  - `late.scan_interval_s` is now a setting.
- **`research` command** (`sniper/research.py`, `docs/RESEARCH.md`):
  - policies in `research/policies/`;
  - `eval` (result, day/4h-block bootstrap, winner dependence, slippage and size curves, timing, no-filter and random baselines, ablations), run in parallel;
  - `freeze`: its signature covers settings, gates and operating costs;
  - `final`: shows nothing before the minimum holdout, then judges once and stores the verdict;
  - `log`: an experiment registry with a variants-tried count.
- **Prefilter:** a graduation replay only needs tokens whose curve gets near the window, but dropping events moved the engine's 1-second clock and changed results (191 vs 201 trades). Dropped events that would have ticked now become Ticks at the same instant. The full and prefiltered replays now match exactly: 201 trades, +1.5506 SOL.
- **`graduation-v1`:**
  - the current graduation rules with organic demand, fixed $20, 6 open, 1 SOL daily loss limit;
  - gates: ≥ 14 holdout days, ≥ 150 trades, P(mean daily > 0) ≥ 0.9, ≥ 0 without the top 3, break-even slippage ≥ 5%.
- **Live paper bot:** copy trading and agent buys are switched off for the evaluation window.

### First development report (graduation-v1, 16.3 h, before freezing)
| | |
|---|---|
| Net | **+1.55 SOL** on 201 trades at $20 (+7.8% on cost), win 26%, PF 1.48 |
| Uncertainty (5 × 4 h blocks) | mean +0.31 SOL/block, 90% [−0.02, +0.64], P(>0) 0.94 |
| Without best 1 / 3 / 5 / 10 | +1.23 / +0.63 / +0.16 / −0.72 SOL |
| By exit | graduation exit 22× +3.41; momentum decay 58× +1.03; stop 99× −2.40; dev sold 17× −0.45 |
| Slippage per side 0 / 1.5 / 3 / 5 / 8% | +2.70 / +2.19 / +1.55 / +0.68 / −1.01 → break-even 6.2% |
| Size $5 / 10 / 20 / 50 / 100 / 250 | +1.2% / 5.3% / 7.8% / 8.2% / 7.4% / 2.4% on cost; at $250, 68 trades (3%-of-curve cap) |
| Candidate scan every 1 / 2 / 3 s | +1.55 / +1.55 / **+0.13** |
| No momentum filter | 294 trades, −0.74 SOL; the rules beat 100% of random picks of 201 |
| Ablations | removing the buyer-count filter hurts most (+0.44); the other filters cost little either way |

Reading: there's something here worth testing, but two warnings stand out.
- **Entry speed:** checking 1 s later removes most of the profit, so the result likely depends on entering faster than real orders can.
- **Stale data:** the development data was partly 12 s stale.

Next after the holdout is collecting: **execution measurement**. That means signal-to-landing delay, and how far price moves in that time by curve stage and size, in place of the flat 3% fill assumption.

---

## 2026-10-03 — Entry #15: Chat in the dashboard, contract-address lookup, paper deposits, more charts

### Owner input
- "I want to not have to go to my terminal to start and talk to it. I want a chat section and more visual pleasers and graphs etc."
- "Want the bot to be able to raise paper trading balance if I ask it to and also I want to be able to type a CA to it and it pull metrics."

### Built
- **Chat tab** (`ui/chat.py`): each message runs Claude Code headless (`claude -p`, stream-json) in the repo, on the owner's Claude plan, with no API key.
  - **Isolation:**
    - `--tools ""` removes the shell and file tools;
    - `--strict-mcp-config` loads only the meme-trader server;
    - the environment is an allowlist, so no `.env` secrets, RPC URLs or `ANTHROPIC_API_KEY` reach it (with that key set, Claude Code would bill the API).
  - **Streaming:** replies stream token by token to every open tab over the existing origin-checked websocket. The conversation continues with `--resume`, and **New chat** resets it.
  - **Approvals:** Claude Code's `--permission-prompt-tool` is `approve_action` on the MCP server. It exists only when the chat starts it. Through `POST /api/agent` (`_approval`) it shows an Approve / Decline card and waits up to 10 min; no answer means declined. Read tools are pre-allowed.
  - **Usage meter:** the stream's `rate_limit_event` feeds a 5-hour / weekly usage meter.
  - **Model:** chosen per chat (Default / Opus / Sonnet / Haiku).
- **Contract-address lookup** (`sniper/lookup.py`): metrics for any Solana token, tracked or not, from five sources in parallel. Each source is optional, and results are cached 15 s.
  - **On-chain:** the bonding-curve account is decoded directly (reserves, graduated flag, creator, Mayhem flag), plus mint/freeze authority and supply.
  - **DexScreener:** price, liquidity, volume, buys/sells, change, socials.
  - **RugCheck:** risk list, top holders with insider flags, holder count.
  - **GeckoTerminal:** candles.
  - **The bot's own view,** when it tracks the token.
  - **Output:** flags summarise what stands out. Pasting a CA in Chat shows the card at once, with no Claude usage. The metrics then go into the prompt, so Claude's read needs no tool call. The token drawer also falls back to this card for untracked tokens.
  - **Holder lists:** PublicNode rejects `getTokenLargestAccounts` without a personal token, and the public RPC rate-limits it. So holders come from RugCheck, then the owner's own `SOLANA_RPC_URL`, then the bot's tape.
- **Paper deposits:** `Engine.deposit_paper` adds pretend SOL as capital, not profit (start, peak and equity history shift with it). Three ways in: the agent tool `add_paper_funds` (capped by `agent.max_deposit_sol`, refused live, optionally saved as `capital.starting_sol`), Controls → Paper balance, and Chat.
- **Charts:**
  - Live KPIs gained an equity sparkline, a win-rate ring, a daily-loss-limit meter and a drawdown-vs-kill-switch meter.
  - New Live panels: a recent-trades strip, the per-minute **market pulse** (launches, trades on tracked tokens, graduations; `Engine.pulse`) and a strategy-mix donut.
  - Analytics gained cumulative P&L by strategy (`timeline`) and P&L by exit reason.
  - A Chat sidebar shows account, positions, market pulse and agent actions.

### Found
- **Mayhem mode** tokens (pump.fun, newer curves) mint **2B** tokens; half goes to pump.fun's trading agent. They have a flag byte after the creator in the curve account. The engine's `market_cap_sol` assumes 1B supply, so it shows **half** the real market cap for these tokens. The lookup uses the real supply. Follow-up: carry supply into `TokenState` and check market-cap-based gates.
- On a Mayhem token the virtual SOL reserve moved far more than the real SOL (k not constant). Treat curve math for Mayhem tokens with care.

### Verified
- **Tests:** 173 pass. The 15 new ones drive the chat with a fake `claude` and cover streaming, resume, lost-session recovery, the env allowlist, approvals (approve, decline, auto, no run), the CA card, restart recovery, routes, MCP tool sets, lookup decode and flags, deposits and the pulse.
- **Real Claude Code against the demo bot (Haiku):**
  - pause with approval: done in 4.6 s;
  - declined resume: not done, and Claude said so;
  - pasted BONK CA: card plus read in one turn.
- **Screenshots** checked in headless Chromium, dark and light, at 1440 px and 400 px. No page errors.

---

## 2026-10-03 — Entry #14: AI operator tools (MCP) for Claude Code

### Owner input
- "What would be the best way to get an agent inside the terminal to decide on trades farther than just parameters?" Then: "Yes please" to step one (MCP tools + interactive Claude Code), with an autonomous loop as step two.

### Design
- Speed stays deterministic: graduation plays often exit ~15 s after entry, and an LLM call takes seconds.
- The agent works one level up: regime, strategy selection, risk dial, token investigation, discretionary entries and exits, and explanations.

### Built
- `sniper/agent_api.py`: the tools and their guardrails, enforced in the bot.
  - **Reads:** status, positions, radar, token, analytics, trades, log, settings.
  - **Actions:** set_setting, pause, resume, sell, buy, watch, note.
  - The configured risk values are ceilings: sizing, max positions, daily loss limit, stop loss. Entry gates are floors.
  - Strategies the config has off stay off; defense mode can't be switched off if configured on.
  - No live switch, no settings save, no kill switch.
  - Every action needs a reason and is journaled (level `agent`, shown on the dashboard). Actions are rate-limited (20/min); agent buys are capped at 6/hour, at most `sizing.max_usd` each, and pass the engine's `_authorize` (daily loss, feed health, cash, max positions).
  - Buys are refused when the creator has sold or the token is near or past graduation.
- `POST /api/agent` on the dashboard server, guarded by a per-run secret in `data/agent.token` (mode 600) sent as `X-Agent-Token`. That's a custom header, so a web page can't send it without a CORS preflight, which the server never grants.
- `sniper/mcp_server.py`: an MCP 2.x `MCPServer` (stdio) with 15 tools and read-only/destructive annotations. It forwards each call to the bot.
- `.mcp.json` registers the server. `.claude/settings.json` auto-allows the 8 read tools and keeps the 7 action tools on "ask". `CLAUDE.md` holds operating notes, and `/desk-check` (`.claude/commands/desk-check.md`) runs a full review.
- Config: new `sniper.agent` section (enabled, can_buy, max_buy_usd, max_buys_per_hour, max_actions_per_min), validated.

Tests: 158 passing (+9 in `tests/test_agent_api.py`, including an MCP-to-engine round trip).

---

## 2026-10-03 — Entry #13: Feed watchdog with endpoint failover

### Why
The overnight debrief found the free endpoints degrade silently. PublicNode went quiet for **91 minutes** (03:12–04:43) on an open socket. Its missing-trade rate then rose from 0.7% (03h) to **32.8%** (12h). The engine only treated a closed connection as "down", so it would have traded on that data.

### Built
- `FeedQuality`: a live completeness check. Each pump.fun trade carries the curve's token reserves after it, so consecutive trades of a token must chain exactly. The share that doesn't, over the last 2,000 checks, is the gap rate.
- `SolanaTradeFeed` watchdog. It drops the current endpoint when:
  - no pump.fun trade has been decoded for `feed.stall_s` (60 s), even if the socket is open;
  - the gap rate exceeds `feed.max_gap_pct` (5%).

  On a fallback endpoint it retries the first one every 30 minutes.
- Endpoints, in order: `feed.ws_url` or `SOLANA_WS_URL` (comma-separated lists allowed), then PublicNode, then the public RPC.
- New buys stay paused while the feed is down, unmeasured (~20–30 s after each connect) or incomplete. `degraded_reason` says why, on the dashboard and in the "blocked" reason. The snapshot carries `gap_pct`.
- AI desk `max_tokens` 2048 → 8000. Thinking is always on with Opus 5.5, and a truncated vote would fail the desk closed.

### Live check (45 s)
PublicNode measured 6.8% missing, so the feed switched to `api.mainnet-beta`, which had 0.0% missing over 2,000 checks (its data allowance had reset).

### Paid flat-rate options researched (no per-message or per-MB metering)
| Provider | Plan | Price | Notes |
|---|---|---|---|
| NoLimitNodes | Pro | $49/mo | WSS with logsSubscribe, "no per-message meter" |
| RPC Fast | Focus | $45/mo | 10 WebSockets, unlimited bandwidth (free tier: 50 GB/month, ~3 days of this stream) |
| Chainstack | Unlimited Node | ~$149/mo | flat, unmetered RPC |
| Subglow / Solana Tracker | gRPC | $99/mo / €200/mo | Yellowstone gRPC: needs a new feed type |
| Helius, QuickNode | | | logsSubscribe billed per MB: expensive for ~10–20 GB/day |

Tests: 149 passing.

---

## 2026-10-03 — Entry #12: Trade feed moved to PublicNode, at "confirmed"

- After restarting onto the review fixes, the free public RPC (`api.mainnet-beta.solana.com`) refused the
  trade stream: HTTP 413, "You have used your data allowance". It caps data per IP, and this stream is
  ~10-20 GB/day. The previous process had kept its old connection open; the restart needed a new one.
  The engine reported the feed as degraded and paused entries, as designed.
- `wss://solana-rpc.publicnode.com` (free, no key) serves the same `logsSubscribe` stream. dRPC's free plan
  doesn't allow the method. Set as `SOLANA_WS_URL` in `.env`, and documented in `.env.example`.
- Reserve-chain completeness check on PublicNode, 40 s each:

| commitment | messages | chain gaps |
|---|---|---|
| processed | 2,749 | **25.3%** of trades missing |
| confirmed | 3,886 | 0.4% |

- New `sniper.feed.commitment`, default **confirmed** (validated). It costs a fraction of a second, which neither the
  confirm-then-ride sniper nor graduation plays depend on. `doctor` checks at confirmed too. Feed-down log
  lines are now one short line instead of a full handshake dump.

Tests: 145 passing.

---

## 2026-10-03 — Entry #11: Second full code review (29 findings), all fixed

### Owner input
- A full read-only review of `7a26092` came back from another session: 29 findings, 8 of them P1, plus offline probe scripts. The owner chose "fix everything (A-D)" and to keep the DexScreener bot's live mode, with isolated state.
- The probe scripts weren't run here (running code from the zip was blocked by the permission system). Each finding was checked against the source instead (R01, R05, R14, R15 and R16 by direct reading, all confirmed), and every fix got its own regression test written to the review's acceptance check.

### Fixed
| # | Finding | Fix |
|---|---|---|
| R01 P1 | Trade logs accepted from any program in a pump.fun transaction | `parse_logs` tracks the invoke/success stack and takes a `TradeEvent` only while pump.fun itself is executing. Bad depth, unmatched success or truncated logs reject the whole tx. **Live check: 1,354 of 1,354 real trades still accepted** |
| R02 P1 | Order confirmation blocked the feed | Live orders run as their own tasks (per-mint serialized, cash reserved first), so other tokens' stops and dev sells keep being processed. Shutdown waits for sent orders. Backtests stay inline (deterministic) |
| R03 P1 | Unknown outcomes treated as failures and re-sent | Signed locally first (signature known before sending). Unclear sends/confirmations return `unknown`, never retried. `Engine.unresolved` is persisted, resolved on-chain every 5 s (a late buy becomes a managed position), and released after the blockhash must have expired |
| R04 P1 | Buys didn't reserve cash; final size not re-checked | One central `_authorize` with the final size, plus `book.reserved` (principal + fees + live rent) that every approval counts |
| R05 P1 | Callouts skipped the daily-loss / feed-health gates | `_global_block` (halt, pause, degraded feed, daily loss) applies to every entry source. Callout cards post only after the bag fills; one callout in flight at a time |
| R06 P1 | Ledger missed failed-tx fees, clamped negative sells, never re-read SOL | `fees_lost`, signed sell proceeds and account rent (locked/reclaimed, confirmed) all move cash. Live: wallet SOL is checked every 2 min and on restore; a shortfall lowers the ledger and counts against today's loss |
| R07 | Restart lost creator/dev-sell/defense context | State saves each held token's launch and `dev_sold`, plus defense mode. A later Launch fills a missing launch object |
| R08 P1 | UI bind failure left a second engine trading; no instance lock | `flock` per mode per data folder, plus per live wallet across checkouts. The dashboard binds before trading; a busy port exits with nothing traded. Helper-task failures are reported |
| R09 | Metadata URLs could reach private services | http(s) only. Literal IPs and DNS results must be public (custom resolver, so redirects too). Manual redirects (max 3, each checked), 64 KB cap, 8 at once, string fields only |
| R10 | SOL quote recognised by ticker | Canonical wrapped-SOL mint address + requested base mint required |
| R11 | Failed txs made fake funding links | Failed signatures/transactions skipped. Unavailable data raises (retried later) instead of being cached as "unknown" |
| R12 | Demo/paper/live trades mixed in reports | Trade rows carry mode, session, start balance, config hash and model id. `report` refuses mixed modes without `--mode`, has `--session`, flags differing start balances. Review uses paper/live rows only |
| R13 | Leader discovery mis-ranked wallets | Open bags = value minus remaining cost; a win is decided on the whole round trip |
| R14 | Replays used future information | Purged splits (training stops at the cutoff, embargo of max checkpoint + horizon for training, longest hold for sweeps). Models record their data window; replays only use a model trained before their first event. Replays start with neutral caller weights |
| R15 | Synthetic models could deploy; "display only" still sized | `train` writes a candidate; `promote` requires recorded data + held-out AUC ≥ 0.6. Live loads promoted models only. New `predict.display_only: true` (default): the model changes no trade |
| R16 | Unfinished label windows counted as losses | Censored (dropped) unless the recording covers the full horizon |
| R17 | Social links dropped in training | New `Metadata` event stamped at arrival, applied the same way in training, replay and live |
| R18 | Max drawdown forgot losses outside the chart buffer | `Book.mark` tracks the session peak and max drawdown on every tick; analytics and compare use it |
| R19 | AI desk failures made approval easier | Errors stay in the denominator. The skeptic must answer; at least half the voting weight must respond |
| R20 | X setup deleted other apps' rules; no error backoff | Rules tagged `meme_trader`, only ours replaced. HTTP status checked: 401/403 stop, 429 honours the reset header, others back off exponentially |
| R21 | Failed Telegram alerts counted as sent | Only `ok` responses count; 429 honours `retry_after`; bounded retries; failures counted and printed |
| R22 | Review commands interpolated model output | Proposals validated against real keys and types, JSON-encoded, `shlex`-quoted |
| R23 | Sniper config never validated | `validate_sniper`: ranges, finite numbers, non-empty retry lists, cross-field rules. Runs at load, after every `--set`, and on dashboard changes (reverted if invalid). `ConfigError` instead of `assert` |
| R24 P1 | DexScreener bot reused paper state live | State per mode and wallet (`state-paper.json`, `state-live-<wallet>.json`). A mismatched file is refused. Live reconciles tokens and SOL before trading; resuming needs only the fee reserve |
| R25 | Legacy kill switch could sell partially or not at all | Halt is evaluated first and always sells the full balance, with or without a display price |
| R26 | Legacy LP check could validate the wrong pool | Uses the selected pool's lock; missing data fails |
| R27 | Dust write-offs understated the daily loss | The remaining basis is written off into `day_pnl` exactly once in `_close` |
| R28 | Unknown price bypassed the graduation max hold | Unknown-price exits use each strategy's own limit (late, callout, generic) |
| R29 | Minimum size overrode the liquidity cap | The cap is a hard ceiling; below a viable minimum (half the base size) it returns 0 = no trade |
| extra | PumpPortal-built transactions were signed unseen | `Wallet.sign` checks we're the fee payer. `execution.verify_tx` simulates each transaction and refuses one that would move more SOL than the order allows |

Also: `_lock` closes its file when refusing; reservations restore only for still-unresolved buys; order-task exceptions are logged; the cash check skips if the ledger moved mid-read.

### Checks
- Tests: **144 passing** (+52). The new ones in `tests/test_review_round2.py` follow the review's acceptance checks.
- Backtest of the recorded free-feed hour: **identical results before and after** (13 trades, +0.149 SOL). The fixes change failure handling, not normal paper behaviour.
- A 70 s paper run of the new code from a separate folder recorded launches, trades and a `metadata` event. A second copy was refused by the lock, and the demo was refused on the service's busy port before trading.

### Behaviour changes to know
- `train` no longer changes the running bot: run `promote` after it. The model is display-only until `predict.display_only: false`.
- Backtests, sweeps and compares on the days a model was trained on run without it. To test the model as a filter, train on older days and sweep newer ones (HOWTO §9).
- The DexScreener bot now fails tokens whose selected pool has no LP-lock data. If RugCheck's market ids don't match DexScreener's pair addresses for some pools, those tokens will be skipped (conservative by design).

---

## 2026-10-03 — Entry #10: Free on-chain trade feed (PumpPortal's meter was the bottleneck)

### What happened
- First real-market paper run (2026-10-02). Trade data streamed 22:52–23:40 CDT (~48 min, 1,959 launches). PumpPortal billed 0.01 SOL every ~4 min (23:00, 23:04, 23:08, 23:12). That took its wallet from 0.046 SOL to under the 0.02 minimum, and the trade stream stopped.
- Measured volume: 1,000–2,000 streamed trades/min with recording on ≈ **3 SOL/day for data alone**, against $5–20 positions.
- Paper result while it could see: 7 trades (2 wins), +0.136 SOL on 1 SOL. That's almost all one graduation play (BOOBIES +208%). The sniper made 1 trade (−17%). Far too few trades to mean anything.

### Analysis (recorded feed, 50k trades)
- Top 10 tokens = 58% of trades; top 100 = 90%. The bill is driven by the popular tokens, which are exactly what the strategies want to watch.
- 82% of trades came from tokens the sniper rejects on launch facts alone (dev buy 62%, serial deployer 16%, copycat 13%). But graduation plays skip those gates, and the one big winner was a copycat ticker, so a launch-time filter would have dropped it.
- Best cheap policy found (launch prefilter for the sniper + subscribe graduation candidates only at ≥45% curve): still ~34% of the volume (~1 SOL/day). Per-trade billing can't be fixed by being choosy.

### Built: `SolanaTradeFeed` (`sniper.feed.trades: solana`, now the default)
- Launches and migrations from PumpPortal's **free** streams (keyless). Trades from the pump.fun program's own logs (`logsSubscribe`, Anchor `TradeEvent`) over a Solana websocket: `SOLANA_WS_URL`, default the free public RPC.
- Decoding verified on mainnet: on standard curves, every trade's reserve change matched its amounts (1,392/1,392). Curves with non-standard virtual reserves (v_sol − real_sol ≠ 30) don't chain; the bot's curve model doesn't fit them on any feed.
- **Early-trade replay:** 16% of launches had trades (the insider bundle) before PumpPortal announced the launch. Unwatched trades are held 15 s and replayed on `watch()`, restamped to arrival time. The creator's launch buy is dropped because the Launch already carries it.
- If trade logs stall for 30 s, the feed reconnects and reports `degraded`, which pauses entries (same rule as the backup launch feed).
- `doctor` gets a `trade logs` row; `PUMPPORTAL_API_KEY` is only required with `trades: pumpportal`.
- Deliberately **not** derived from `SOLANA_RPC_URL`: the stream is ~20 GB/day, ~12M Helius credits/month on per-MB billing.

### Measured (same machine; different hours, so volume isn't comparable)
| | PumpPortal (22:35–23:10) | Solana logs (00:28–00:33) |
|---|---|---|
| Launches with trade data | 58% | 84% |
| Median first-trade delay after launch | 0.9 s | 0.1 s |
| Trades missing/out of order (reserve chain check) | **20.6%** | **0.0%** (1 of 3,946) |
| Cost | ~3 SOL/day | $0 |

The PumpPortal recording has holes, so treat backtests on it with care.

### Ops
- The P8 now runs the bot as a **user-level** systemd service (`~/.config/systemd/user/meme-sniper.service`, linger on). It needs no sudo, starts at boot, and restarts on crash (tested: back in 10 s). `systemctl --user status meme-sniper`, `journalctl --user -u meme-sniper -f`.
- Demo leftovers were moved out of `data/` to `demo-leftovers/` so simulated trades don't mix into real analytics.

Tests: 92 passing (+4).

---

## 2026-10-03 — Entry #9: Prediction model, graduation plays, analytics, new dashboard, A/B at scale

### Owner input
- "Full power improve for scalability, UI, interactiveness, highlights, how-tos, metrics, analysis… profitability, extreme out-of-the-box ideas and proven strats and quantum-level predictions… run it massive."
- "Quantum-level predictions" was taken honestly. There is no quantum computing here. What was built is the strongest prediction that can be checked:
  - a calibrated probability, graded on launches the model never saw;
  - a Monte Carlo projection of the next 100 trades from the bot's own results.

### Built
**Prediction**
- `features.py` takes a 36-number snapshot per token: curve state, first-minute flow, holders and insiders, socials, smart wallets.
- `predictor.py` models P(+100% before −30% within 10 min):
  - walk-forward in three slices (fit on the oldest launches, calibrate with temperature scaling on the next, grade on the newest);
  - reports AUC, Brier skill and top-decile lift.
- `train` saves `data/model.json`, and a running bot hot-reloads it within a minute.
- P(2x) shows on the radar, positions, closed trades and the token panel.
- The model feeds quarter-Kelly sizing. It can also gate entries (`predict.min_p`, `require_positive_ev`), which is off until trained on your data.

**Strategies and risk**
- Graduation plays (`late.*`): late-curve momentum, sold at 94% of the curve, before migration.
- Defense mode: after 5 losses in a row or a bad 10-trade window, half size and +10 entry score for 30 min.
- Gate audit: every decision is followed for 10 minutes and scored by the model's first-passage rule, against our own buys as the yardstick.
  - The first version measured "fell 50%" from the rejection price. That was impossible for early rejects, which sit at the bonding curve's price floor, so it was replaced.

**Analysis**
- `analytics.py` computes:
  - expectancy, payoff, SQN, profit factor, capture of peak runs, and MAE of winners;
  - breakdowns by strategy, exit, hour, score and P(2x);
  - bootstrap confidence in the edge, and a block-bootstrap projection with kill-switch odds;
  - plain-English highlights.
- `report` writes a static, shareable HTML page and CSV. The AI review agent now receives the analytics too.

**Dashboard**
- Four views: Live, Analytics, Controls, Guide.
- A token detail panel: gate checklist, P(2x) with EV, model drivers, holders and funders, tape.
- Live settings with Save, strategy chips, radar filter, toasts, highlights, feed-health and model badges, keyboard shortcuts, theme toggle.

**Scale**
- `sweep --jobs` and the new `compare` run on every core.
- The summary is cached until a trade closes. Analytics runs in a worker thread with bounded bootstrap work. The snapshot is built and encoded once per tick.
- Recordings rotate at UTC midnight and finished days are gzipped (~8–10× smaller). Replays read `.gz` and tolerate crash damage.
- `scripts/start.sh train|sweep|compare|backtest|leaders` use `data/feed-*` automatically.

**Docs**
- `docs/HOWTO.md` (task recipes), STRATEGY §8c (research), README, new screenshots.

### Bugs found and fixed
- **Dashboard froze after an all-wins start.** Profit factor = ∞ was sent as bare `Infinity`, which `JSON.parse` rejects. All JSON now goes through `jsonsafe`.
- **Websocket dropped on the first settings change.**
  - Cause: with permessage-deflate (negotiated by browsers), a reply sent between the ~80 KB snapshots corrupted the compressed stream, and Chromium closed the socket with 1002.
  - Fix: compression is off for this local socket and writes are serialized. A test guards it.
- **Graduation plays could never trigger live.** Rejected tokens were unsubscribed (immediately, or after 10 minutes when recording) before the 15-minute late window. They are now kept, and recordings always cover the full window, so `compare` can evaluate late plays even on days they were off.

### "Run it massive": 72 backtests (8 simulated markets × 1500 launches × 9 variants, 235 s on 4 cores)
The model used by the variants was trained on a **different** market seed (101) and scored AUC 0.946 on held-out launches. The simulated market is learnable by design: expect far lower AUC on real data.

| Variant | Mean P&L / market | sd | Beat base | Paired t | Median PF | Trades |
|---|---|---|---|---|---|---|
| base | +2.408 | 0.850 | – | – | 8.9 | 107 |
| **late + gate 0.15** | **+4.446** | 0.993 | **8/8** | **+12.1** | 9.8 | 135 |
| late (graduation plays) | +4.017 | 1.017 | 8/8 | +9.0 | 7.6 | 150 |
| defense mode off | +3.149 | 0.766 | 7/8 | +5.3 | 9.3 | 112 |
| model Kelly sizing | +2.468 | 0.894 | 7/8 | +2.7 | 9.9 | 107 |
| gate 0.15 | +2.544 | 0.806 | 4/8 | +1.0 | 15.5 | 91 |
| gate 0.30 | +2.452 | 0.790 | 3/8 | +0.3 | 14.9 | 86 |
| positive-EV filter | +2.453 | 0.787 | 3/8 | +0.3 | 14.9 | 86 |
| ladder exits | +1.665 | 0.513 | 2/8 | −2.3 | 7.2 | 82 |

### Decisions (simulated market = logic check, not evidence)
- **Graduation plays: on by default in paper.** They won on all 8 markets, and paper costs nothing. The by-strategy table in Analytics decides before going live.
- **Model gate: off by default.**
  - Alone it is noise (4/8): it raises profit factor but cuts trades.
  - Combined with late plays it was the best variant, so test `predict.min_p` with `sweep` once a model is trained on real data.
  - The positive-EV filter behaves exactly like gate 0.30, because with these costs EV ≥ 0 ⟺ P ≥ ~0.30.
- **Kelly sizing input: stays at ¼.** It is a small but consistent gain (7/8).
- **Ladder exits: stay off.** They lost on 6 of 8 markets.
- **Defense mode: stays ON, knowingly.**
  - It cost ~24% of profit here. That's expected: simulated trades are independent, so a brake can only cost money.
  - Real markets have regimes (cold memecoin weeks, an edge that decays), and that is what the brake is for.
  - Test it on your recordings: `scripts/start.sh compare --variant "no_defense: risk_adapt.enabled=false"`.

Tests: 88 passing (+29 this entry).

### Owner next steps
1. Run `scripts/start.sh paper` for 2–3 days, on the P8 as a service.
2. Then run `scripts/start.sh train`. Keep the model display-only unless the verdict says "useful".
3. Then run `scripts/start.sh compare --variant "no_late: late.enabled=false" --variant "no_defense: risk_adapt.enabled=false"`.
4. Read Analytics, and share `scripts/start.sh report`.

---

## 2026-10-03 — Entry #8: Walk-forward tuning (`sweep`)

- `python -m meme_trader.sniper sweep --file data/feed-*.jsonl --grid key=v1,v2 [--grid ...]` (or `scripts/start.sh sweep ...`).
- **How it works:**
  - recorded launches are split by time: the first 60% are for tuning, the rest for testing;
  - each token stays on one side;
  - funding data is shared by both sides;
  - every combination runs on the tuning set; the top 3, plus the current settings, then run on the unseen test set.
- **A change is recommended only if it beats the current settings on the test set by a real margin** (≥ 5% and ≥ 0.02 SOL, or 0.1 profit factor). This guard was added after the first demo run "recommended" a change that was identical to the current settings, to within rounding.
- Demo on the simulated market (1200 launches, 9 combinations, ~2 min): stop loss at 20/30/40% made no difference. Momentum-decay and no-follow-through exits fire first. Real data may differ.
- Tests: 59 passing (+3: split integrity, noise guard, a no-op change is never recommended).

**Owner workflow once data exists:** record → `sweep` the two or three settings in question → apply only a recommended change → paper trade a few days → sweep again.

---

## 2026-10-03 — Entry #7: Line-by-line review (25 fixes), restart safety, fee research

### Owner input
- "Improve", then "keep improving, comb every line and do more research."

### Restart safety (new)
Live positions used to live only in memory. The Ubuntu service restarts the bot after a crash, which would have orphaned open positions.
- State (book, positions, called coins) is saved atomically after every fill and every 10 s.
- On restart it's restored and **checked against the wallet**: positions that are gone get closed, changed balances get corrected, and unknown pump tokens get flagged.
- Restored positions wait for a real price before any exit runs. They get a DexScreener fallback price, and the max-hold time still applies.

### Full code review (26 findings; 25 fixed)
Ranked by severity, with the money-path issues first:
1. A preflight failure raised out of the executor. The sell never tried the next slippage step and the engine stopped. **Fixed:** every executor error is now a failed fill.
2. Fills were read from wallet balances at "finalized" right after a "confirmed" confirmation, so a fill could show 0 tokens, then a divide-by-zero, then repeated sells. Concurrent trades also polluted each other's SOL changes, and token-account rent was booked as cost. **Fixed:** fills are measured from the confirmed transaction's own pre/post balances (`Wallet.tx_deltas`), with rent kept out of cost.
3. Ticks could overlap during slow live sells, leading to a KeyError and a dead ticker. **Fixed:** a tick lock, safe lookups, and one bad event can no longer stop the live engine.
4. A dashboard sell or KILL during an in-flight sell could double-count proceeds and crash on close. **Fixed:** `_sell` guards itself and close is idempotent.
5. **Any website could send KILL/SELL to the local dashboard** over a websocket. **Fixed:** origin check. A foreign page is verified blocked in a real browser.
6. Live start-up refused to run after a restart, since part of the budget sits in positions. **Fixed:** resuming needs only the fee reserve.
7. Restored or quiet positions had no price and no exits. **Fixed:** DexScreener fallback price every 15 s for stale holdings, and max-hold applies even without a price.
8. Callouts could stack a $1 bag on top of an existing position. **Fixed:** `_buy` never stacks on a held mint.
9. Feed parsing could die on a null field (`newTokenBalance: null`). **Fixed.**
10. Other fixes:
    - pending sells were double-counted as positions;
    - the AI desk was re-paid every tick after a "skip";
    - funding lookups never re-ran for new early buyers;
    - RPC errors were cached as "no funder";
    - paused leaders' sells weren't followed;
    - callers were scored against a placeholder price;
    - `callers.json` was never saved;
    - callout 1 h outcomes were lost;
    - the funder cache was unbounded and saved on the event loop;
    - each lookup batch opened a new HTTP session;
    - config sections were copied on every access;
    - old DexScreener bot: a missing price zeroed equity, and late landings were lost.
- Not fixed (minor): a shared HTTP session for the SOL price, metadata and Telegram calls. The per-few-seconds funding lookups already share one.
- 11 regression tests added, one per class of bug.

### Research (2026-10-03)
| Finding | Effect on the bot |
|---|---|
| Bonding-curve fee 1.25% (0.95% protocol + 0.30% creator); PumpSwap tiers 1.25% → 0.30% by market cap | `curve_fee_pct` 1.25 confirmed |
| A pump.fun buy is ~250k compute units; normal priority/tip 0.001–0.005 SOL, 0.01+ in congestion | **Paper fills now pay a fixed tx cost**, which at $5 is ~6% of the round trip. Buy priority raised 0.0005 → 0.001; sells escalate 0.001 → 0.003 → 0.008 alongside slippage |
| Callout Rewards: daily USDC from fixed pots, pro-rata by caller rank, on volume app users trade because of a call | Rank and real volume matter more than call count. **Callout bags use a minimal 0.00005 SOL priority**: normal fees ate ~27% of a $1 bag. The bags went from about −46% per losing bag to break-even overall in the simulation |
| Cashback coins return the 0.3% creator fee to traders, but only on trades made through pump.fun's own Terminal | Not available to us via PumpPortal (noted, not used) |
| pump.fun now offers built-in bundled launches (initial buy across up to 17 wallets) and a "Fair Launch Shield" | Already counted by the bundle window and the funding tracer. Re-check `max_bundle_pct` on real data, since some honest creators will use it |
| PumpPortal message format unchanged (create/buy/sell/migrate, `marketCapSol`, `pool`) | Parser confirmed |

Tests: 56 passing.

---

## 2026-10-03 — Entry #6: Callout agent, insider-cluster detection, phone alerts

### Owner input
- pump.fun callouts require buying and holding at least $1 of the coin; Callout Rewards pay on the volume calls attract; one call per 2 minutes. Example account shown: TGMetrics (data-driven calls, $1 positions, a public record).
- "Keep improving."

### Position on callouts (revised)
With the $1 hold enforced by the platform and the bag kept at that minimum, the income is the rewards, not dumping on followers. That's a legitimate model, so we built it.

What the TGMetrics screenshots show: their closed calls mostly fell from about $10–20K market cap to about $3K, and their trading P&L is +$6.68 on $326 of buys. Called coins mostly die, and the money is in the rewards. That makes picking clickable coins and keeping an honest record the whole game.

Your old callout bot posted to Telegram, not to pump.fun. We know of no public API for posting pump.fun callouts, so posting there is two clicks from the dashboard (Copy + Open on pump.fun) until the owner captures the request the site sends.

### Built
- **Callout agent** (`sniper/callouts.py`):
  - picks the most "clickable" eligible coin at most once per 2 min (holder growth, inflow, curve fill, smart wallets, near the high);
  - only after the sniper has finished deciding on that coin;
  - same red-flag exclusions as trading;
  - buys a $1.10 bag (stays ≥ $1 after fees), held 1 h with an early exit only if the dev sells;
  - bags don't take trading slots;
  - factual card text: measured numbers only, no urgency, holding disclosed;
  - 5 m / 1 h / peak outcome of every call tracked;
  - optional Telegram channel posting;
  - dashboard Callouts panel with Copy / Open on pump.fun / Mark posted.
- **Insider-cluster detection** (`sniper/funding.py`):
  - looks up the first funder of each early buyer and of the current top 10. Helius `funded-by` is used if `HELIUS_API_KEY` is set (paid, 100 credits per call, labels exchanges); otherwise any RPC, by reading the wallet's first transaction;
  - links wallets funded by the dev, by the dev's funder, by another early buyer, or sharing one non-exchange funder;
  - exchanges are excluded by label or by fan-out (a funder seen funding 50+ wallets);
  - entry gate: linked wallets holding > 15% → reject. While holding: re-checked every 5 s, exit if the cluster grows past 20%;
  - lookups are cached in `data/funders.json` and **recorded into the feed file**, so backtests replay the same graph;
  - capped by a cost guard (240 lookups/min) and never holds an entry more than 6 s.
- **Phone alerts** (`sniper/notify.py`): Telegram bot messages for buys, closes and errors (configurable). Sent in the background, rate-limited.
- Dashboard fixes:
  - callout bags moved out of Open positions;
  - bag peak tracking fixed;
  - capacity limits (copy rate limit, max positions) no longer counted as coin rejections.
- Synthetic market: new "stealth rug" archetype. Insiders funded by the dev (directly or via a middle wallet) buy over the first minute, dodging the timing gates, then dump. Funding events are emitted for every wallet.
- Tests: 44 passing (+5: transfer/createAccount parsing, cluster linking incl. exchange label and fan-out, engine rejects an insider-cluster entry from replayed funding events but buys the same tape with the gate off, callout rate limit/bag size/factual text, notifier level filtering).

### Synthetic findings (logic check, not evidence)
- Detection is accurate: on stealth rugs it measured 5–19% linked supply where the true figure was 11–21%.
- **The P&L impact in the simulation is zero.** The simulated insiders never crossed the 15/20% limits, and the bot's fast exits made those coins profitable anyway. Real-world value is protection against insider farms. Tune `entry.funding.max_cluster_pct` on recorded real data.
- Callout bags are roughly break-even in the simulation. Rewards aren't simulated, and they're the actual income.

### Needs from owner
- To automate pump.fun posting: post one callout by hand with the browser dev tools open (Network tab), then send the request it makes (URL plus payload, **without** cookies/tokens). Then I can wire up "auto_post: pumpfun".
- `SOLANA_RPC_URL` (Helius free tier is fine) enables insider lookups in paper mode too. `HELIUS_API_KEY` (paid) adds exchange labels.
- Telegram alerts: create a bot with @BotFather, get your chat id from @userinfobot, and put both in `.env`.

---

## 2026-10-03 — Entry #5: Intel brief from the owner's other bot, integrated

### Owner input
- Uploaded `memecoin-bot-bundle`: an intel brief (filter stack, Instagram-sourced filter settings, Sep 23–24 alert data, sizing/exit/risk rules) and the `pump-callout-bot` code.
- Instruction: "do whatever you think will be the most profitable."

### Assessment (summary of what was said in chat)
- **Strongest intel:** spikes round-trip within 2–5 min, so speed matters. Your other bot was minutes late by design: it scanned DexScreener for pairs up to 12 h old and alerted a human. Our websocket sniper acts in seconds.
- The Instagram filter settings come from marketing funnels. They're hypotheses for backtesting, not proven. The 32-alert, one-night sample is too small to tune on.
- **Callout bot:** not integrated. "Post callouts and auto-buy alongside, rewarded on the volume the calls attract" slides into buying ahead of your own followers and selling into them. That harms the followers and carries legal risk. We offered a version with guardrails (post first, cooldown before trading the call, disclosure, public track record), pending a decision.
- **Your other bot's SOL price source (`lite-api.jup.ag`) was shut down on 2026-01-31**, so its dollar sizing will fail. Replaced here with Jupiter v3 (needs a key) → CoinGecko → config fallback.

### Built
- **Bug fix (could have trapped live money):** live trades now use PumpPortal `pool=auto`. Before, they always targeted the bonding curve, so selling a token that had graduated mid-position would have failed. Graduated-pool trades are now priced from `marketCapSol`.
- **Live execution hardening:**
  - each attempt gets a fresh quote; buys are never blindly retried;
  - sells re-quote with rising slippage (15 → 25 → 40%);
  - fills that land after the confirmation timeout are still booked;
  - emptied token accounts are closed after a full exit, reclaiming rent. Works for SPL Token and Token-2022, and can't close a non-empty account.
- **New entry filters (from the intel):**
  - fees paid ≥ 0.1 SOL (from volume × 1.25%);
  - snipers (bought in the first 10 s) still holding ≤ 20%;
  - insiders (dev + bundle wallets) still holding ≤ 30%.
- **Sizing agent:**
  - $5 base, scaling to $20 with signal strength: rule score, buy pressure, momentum, smart wallets in, AI-desk conviction;
  - copies sized ×0.6;
  - capped at 3% of the SOL in the curve;
  - **a $20 hard cap re-checked in `_buy`**, whatever asks for more;
  - live SOL/USD price refreshed every 5 min.
- **The owner's exit rules as `exit.profile: ladder`:** 2x sell half, 5x sell the rest, 0.3x stop, plus a ratchet after 3x so a big winner can't round-trip to entry. The generic −30% stop doesn't apply to this profile.
- **Backup feed:** if PumpPortal drops, the bot switches to `wss://pumpdev.io/ws` (from the owner's bot). New entries pause while on the backup, since it may not carry trades, and it retries PumpPortal every 5 min.
- Tests: 39 passing (+6: sell re-quote and slippage steps with `pool=auto`, no blind buy retry plus late-landing booking, a real solders-built CloseAccount transaction for Token-2022, sizing bounds and hard cap, graduated pricing, the ladder profile).

### Synthetic backtests, 3 seeds × 1500 launches (logic check, not evidence)
| Config | P&L per seed (SOL) | Note |
|---|---|---|
| trail exits (default) | +1.99 / +2.39 / +2.31 | |
| ladder exits (owner's rules) | +1.31 / +1.25 / +1.74 | Holds through pullbacks, so fewer trades fit under the position cap |
| trail, new filters off | +2.66 / +2.71 / +2.50 | The synthetic market has no insider-farmed tokens, so the filters can only cost here |

**Decision:** keep `trail` as the default and the new filters on. Re-decide both on recorded real data:
```bash
backtest --file data/feed-*.jsonl --set exit.profile=ladder
backtest --file data/feed-*.jsonl --set entry.max_sniper_pct=100
```

### Open decisions for owner
- Callout bot: build the guarded version, or skip it?
- "Survivor pass" (10 h+ tokens, Wayne's filters) as a second strategy on the DexScreener bot: yes or no?
- Robinhood Chain: out of scope for now (no RugCheck coverage).

---

## 2026-10-02 — Entry #4: One-command setup for Mac + Ubuntu, illustrated guides

### Owner input
- Will run it on a Mac first, then on a home mini PC (WOWE P8) running Ubuntu. Wants it as easy as possible, with docs and screenshots.

### Built
- `scripts/setup_mac.sh`: checks Command Line Tools (git) and finds Python ≥3.10 (or installs it via Homebrew, or points to python.org). Then it creates `.venv`, installs packages, creates `.env` and runs the doctor. Written to work with macOS's bash 3.2.
- `scripts/setup_ubuntu.sh [--service]`: apt-installs anything missing, creates `.venv`, installs packages, creates `.env` and runs the doctor. `--service` installs a systemd unit (`deploy/meme-sniper.service`) that starts at boot, restarts on failure, and paper trades by default.
- `scripts/start.sh demo|paper|live|doctor|backtest|leaders|review`: one launcher that opens the dashboard and keeps a Mac awake (`caffeinate`). Live mode stays locked unless `MEME_TRADER_CONFIRM_LIVE=yes`.
- Double-clickable Mac launchers in `scripts/mac/`: Start Demo, Start Paper Trading, Check Setup.
- `docs/MAC_SETUP.md`, `docs/UBUNTU_SETUP.md`, with screenshots in `docs/img/`:
  - terminal walkthroughs for both systems;
  - the real setup-script output from a fresh Ubuntu 24.04 run;
  - where to put the keys in `.env`;
  - the dashboard.

### Tested
- `setup_ubuntu.sh` ran end-to-end on a fresh copy of the repo (Ubuntu 24.04, ~19 s) and is idempotent: a re-run leaves `.env` untouched.
- `start.sh`: demo starts and serves the dashboard; `--port` is honoured; live mode refuses without the confirm flag.
- `systemd-analyze verify` passes on the service unit. It wasn't started here, because the sandbox doesn't run systemd.
- **Not run:** `setup_mac.sh` and the `.command` launchers. No Mac is available, so they have only been syntax-checked.

### Fixes found while testing
- `.env.example` shipped placeholder RPC/keypair values, so a fresh `.env` reported them as "set" and the doctor then failed on a missing keypair file. They are now empty, with the examples in comments.
- An empty value in `.env` now counts as unset. Before, an empty `SOLANA_RPC_URL` would have replaced the default RPC with "".
- The doctor's install hints now point at `.venv/bin/pip`, and its closing summary correctly says the PumpPortal key is needed for the real market.

---

## 2026-10-02 — Entry #3: Copy trading, AI agent desk, setup tooling

### Owner input
- Wallet (public address, redacted here). It's a valid 32-byte Solana key and is now set as `wallet.pubkey` in `config/params.yaml`. The sandbox can't reach Solana RPC, so its balance is unverified.
- Wants a fully automatic bot with an agent team: senior dev, senior meme trader, Ansem-style trader, other profitable traders.
- Wants copy trading of profitable wallets in real time.

### Answers / research
- **pump.fun trades are fully public in real time, with no callout needed.**
  - PumpPortal `subscribeAccountTrade` streams any list of wallets (metered, needs an API key).
  - Kolscan (owned by pump.fun) and GMGN are free leaderboards for finding wallets.
- **Copy-trading risks:**
  - latency: we fill after the leader and the other copy bots;
  - bait wallets that dump on copiers;
  - rotating wallets.

  The design handles these with chase guards, results-based leader scoring, auto-pause and data-driven discovery. Research shows the edge exists but is thin (≈3% per copied trade in a 2026 multi-agent study).
- **Correction:** the PumpPortal API key is **required** for live paper trading, not optional. Without it the bot only sees launches, not trades. The doctor now marks it FAIL.

### Built
- `sniper/copytrade.py`:
  - LeaderBook tracks each leader's own P&L and **our copied P&L**, with an hourly copy limit and auto-pause on a losing streak.
  - `discover()` ranks wallets on recorded data and flags insider-like, deployer and bot-speed wallets.
- Engine copy path:
  - `mirror` / `signal` modes, plus guards for max chase, minimum leader buy size, curve-progress cap and red flags (dev sold / bundle / serial deployer);
  - follows leader sells (proportional, or all-out when the leader dumps ≥50%), with our own exits as a backstop.
- `sniper/desk.py`, the **AI desk**:
  - four Claude personas (veteran, narrative, skeptic, quant) vote in parallel with structured JSON output;
  - deterministic weighted aggregation with a skeptic veto, a timeout fallback, prompt-injection-safe framing, and a cost meter;
  - live: runs off the event loop, and price is re-checked after the vote. Backtest: inline and deterministic.
- `sniper/review.py`, a **head trader + senior engineer** post-mortem agent. It writes `data/reviews/*.md`; `--validate` backtests its proposed changes against the recorded feed and gives a verdict. Nothing is auto-applied.
- `sniper/doctor.py` checks packages, keys (presence only), the PumpPortal websocket, RPC, the wallet balance and keypair match, and the Anthropic model.
- `.env` loader + `.env.example`, `docs/SETUP.md` (connect & run), and `config/params.yaml` with the owner's wallet.
- Recording now keeps every launch's trades for 10 min, regardless of decisions, so backtests aren't biased.
- Synthetic market now includes two skilled wallets and one bait KOL, to exercise copy trading.
- Dashboard: Copy trading panel, AI desk KPI, source tag on positions (sniper / copy:label · AI ✓), P&L split by source.
- Tests: 33 passing (+6: leader accounting/pause, discovery, desk aggregation/veto, engine with fake Claude client + copy trading, pubkey validation, config `copy` key).

### Synthetic result (seed 11, 1500 launches; logic check only, not evidence)
| Source | Closed | Win | Profit factor | P&L |
|---|---|---|---|---|
| copy | 31 | 58% | 12.0 | +1.17 SOL |
| sniper | 56 | 48% | 4.7 | +0.65 SOL |

The bait wallet was auto-paused after 5 copied losses. Leader discovery ranked the two skilled wallets first.

### Needs from owner
1. Run locally per `docs/SETUP.md`: `doctor`, then `run --synthetic`, then `run` (paper, with the PumpPortal key).
2. PumpPortal API key (required), Anthropic API key (for the desk/review).
3. Wallets to copy: run `leaders` after a few days of recording, and/or pick some from kolscan/GMGN.
4. Decide on the bot wallet: create a dedicated one (recommended) rather than using the main wallet's key.

---

## 2026-10-02 — Entry #2: Pump.fun early-entry sniper + dashboard

### Owner input
- Wants **early snipes on pump.fun**. Has traded pump.fun manually and "got screwed". Needs earlier entry.
- Wants the classic exit: get initials out after the rise, ride the continuation, sell everything before it rolls over.
- CAs often appear on X/Telegram at or before launch, so those should be signal sources.
- Wants a cool UI. Long-term, wants verifiable results worth showing off.

### Key research conclusions (details + sources in docs/STRATEGY.md)
- Over half of launches are sniped in the creation block, mostly by **dev-funded insider wallets** (87% hit rate). Racing them at block 0 is how retail becomes exit liquidity. **Decision: confirm-then-ride.** Watch 15–240 s, reject insider patterns, buy organic acceleration while the curve is still early.
- Call-channel insiders buy ~100 s before posting. **Decision:** social calls are a learned, weak signal (per-caller track record). No blind buy-on-call by default.
- Price often drops 30–50% after graduation. **Decision:** exit at 92% curve progress by default.
- Tools chosen:
  - Feed: PumpPortal websocket. New-token and migration streams are free. Token-trade data costs 0.01 SOL per 10k trades.
  - Execution: PumpPortal local-transaction API (0.5% fee, signed locally).
  - Later upgrades: Helius LaserStream/Yellowstone gRPC and Helius Sender/Jito.
  - X API pay-per-use: filtered stream, 1,000 rules.
  - Telegram: Telethon user session.

### Built
- `meme_trader/sniper/`:
  - `curve.py` — exact bonding-curve math
  - `tracker.py` — per-token holders, bundle %, dev activity, flow
  - `strategy.py` — entry gates/score and exit logic
  - `engine.py` — event loop, risk, book, kill switch
  - `feeds.py` — PumpPortal live, file replay, synthetic market
  - `signals.py` — CA extraction, Telegram/X adapters, caller book
  - `execution.py` — paper fills on the curve; live via PumpPortal plus local signing
- `meme_trader/ui/`: local dashboard (aiohttp + websocket) at http://127.0.0.1:8787. It shows:
  - KPIs: equity, P&L, win rate, profit factor, funnel
  - live position cards with gain charts, initials badge, curve bar and a sell button
  - launch radar with score/curve/sparkline per token, and a "why we passed" rejection breakdown
  - equity curve, closed trades, activity log, caller leaderboard
  - pause and KILL controls
  - light/dark themes and a mobile layout
- CLI: `python -m meme_trader.sniper run [--synthetic] [--live]` and `backtest [--file …|--synthetic N] [--set k=v]`.
- Live runs record the feed to `data/feed-*.jsonl` for backtesting.
- Tests: 27 passing (14 new for the sniper: curve math, entry gates, exits, parsing, engine cash conservation).

### Results so far (synthetic only, NOT evidence of real profitability)
| Seed | Launches | Entries | Win rate | Profit factor | P&L on 1 SOL |
|---|---|---|---|---|---|
| 11 | 1500 | 74 | 64% | 9.6 | +1.74 SOL |
| 12 | 1500 | 53 | 68% | 18.8 | +1.65 SOL |
| 13 | 1500 | 61 | 56% | 10.7 | +1.62 SOL |

This shows the logic works as designed: about 2% of synthetic bundle-rugs got through, and runners made most of the profit. The synthetic market has no competing bots and no adversarial devs, so these numbers will **not** carry over to the real market.

### Blockers / needs from owner
- The cloud sandbox blocks pumpportal.fun, so there is no live data yet. **Run locally** (`run` on a laptop/VPS), or allow `pumpportal.fun`, `api.dexscreener.com` and `api.rugcheck.xyz` in the environment's network settings.
- Optional credentials:
  - `PUMPPORTAL_API_KEY` (trade-data stream)
  - `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` + channel list
  - `X_BEARER_TOKEN` + account list
  - a paid RPC for live trading
- Telegram channels and X accounts to watch.

### Next steps
1. Record 3–7 days of the live feed in paper mode.
2. Backtest on the recordings and sweep parameters walk-forward.
3. Add wallet-graph bundle detection and dev history.
4. Train a learned scorer on recorded launches.
5. Go live small.

---

## 2026-10-02 — Entry #1: Research + initial scaffold

### Goal
A multi-agent trading bot for Solana meme tokens that trades from the owner's Solana wallet.
It has layered safety checks and runs in **paper mode by default**.

### Research findings

| Need | Choice | Notes |
|---|---|---|
| Discovery + market data | **DexScreener** public API | No key. Limits: ~300 req/min (pairs/tokens), 60 req/min (profiles/boosts). `tokens/v1/solana/{up to 30 mints}`. |
| Rug / scam checks | **RugCheck** public API | No key for reads. `GET /v1/tokens/{mint}/report`: score, risks (warn/danger), mint/freeze authority, top holders, insiders, LP lock %. |
| Routing + swaps | **Jupiter Swap API v1** (`api.jup.ag/swap/v1/quote`, `/swap`) | **Needs an API key** (`x-api-key`) from portal.jup.ag. `lite-api.jup.ag` was deprecated on 2026-01-31. |
| Signing | `solders` (Python) | Signs locally. The key never leaves the machine. |
| Sending | Solana JSON-RPC (`SOLANA_RPC_URL`) | The public RPC is rate-limited and unreliable for trading. **Use a paid RPC** (Helius, Triton, QuickNode). Helius Sender / Jito bundles are a later option for landing transactions faster and protecting against MEV. |

Main risks in meme trading, and the checks built for each:
- **Rug pulls** (LP pulled, supply minted, wallets frozen): mint and freeze authority must be revoked, LP must be locked, no RugCheck "danger" risks.
- **Concentrated supply / insiders**: limits on the top-10 holders' share and the largest single holder's share. AMM pool accounts are excluded from this count.
- **Honeypots / sell taxes**: before every buy, the bot gets a buy quote and then a sell quote for the same tokens. It rejects the trade if the round trip loses more than `max_roundtrip_loss_pct`.
- **Thin liquidity / slippage**: minimum liquidity, maximum price impact, and slippage set in bps.
- **Chasing pumps**: the bot rejects tokens up more than `max_price_change_1h_pct`.
- **Losing control of the account**: per-trade size, maximum open positions, a daily loss limit, a drawdown kill switch that sells all positions and stops, and a SOL reserve kept for fees.

### Architecture (agents)

```
           ┌────────────── Orchestrator (every poll_seconds) ──────────────┐
           │                                                               │
 1. Monitor ── exits: stop loss / TP ladder / trailing / time stop / kill switch ──► Executor
 2. Risk gate ── halted? daily loss? max positions? reserve?
 3. Scout ── DexScreener new profiles + boosts → SOL pairs → Candidates
 4. Safety ── RugCheck hard gates (any fail = reject)
 5. Analyst ── momentum/flow score 0–100 (volume, buy/sell ratio, 5m change, liq/FDV)
 6. Risk approve ── sizing → Order
 7. Executor precheck ── Jupiter price impact + honeypot round trip
 8. Executor ── paper fill or live swap (quote → tx → sign → send → confirm → measure balances)
           │                                                               │
           └──── Journal: every decision → data/journal-YYYY-MM-DD.jsonl ──┘
                 Portfolio state → data/state.json
```

Code layout: `meme_trader/agents/{scout,safety,analyst,risk,executor,monitor}.py`, `meme_trader/clients/`, `meme_trader/orchestrator.py`.
All parameters are in `config/params.example.yaml`. Local overrides go in `config/params.yaml`.

### Safety rails built in
- `mode: paper` is the default. Live mode needs `mode: live` **and** the env var `MEME_TRADER_CONFIRM_LIVE=yes`, a Jupiter key, and a keypair file. The keypair's pubkey must match `wallet.pubkey`, and the wallet balance must be at least `starting_sol`.
- The private key is only read from the file at `SOLANA_KEYPAIR_PATH`. It never appears in the config, logs or git. `.gitignore` blocks `*.keypair.json`, `id.json` and `.env`.
- **Use a dedicated hot wallet** holding only the trading budget. Never use the main wallet.

### Status
- [x] Scaffold, all six agents, orchestrator, journal, portfolio state
- [x] Unit tests: 13 passing (safety gates, analyst, monitor exits, risk/PnL, kill switch, config validation)
- [ ] **Live data not tested yet.** The cloud sandbox's network policy blocks `api.dexscreener.com` and `api.rugcheck.xyz` (403 at the proxy). The bot handled it correctly: it logged a `source_error` and finished the cycle. To test against live data, run it locally or allow those hosts in the environment's network settings.
- [ ] Not verified against live responses: the RugCheck field names (`score_normalised`, `topHolders[].owner`, `markets[].lp.lpLockedPct`) and Jupiter's `priceImpactPct` units (treated as a fraction).
- [ ] Live swap path written but never executed

### Next steps (proposed)
1. Owner supplies starting parameters (below). Write them into `config/params.yaml`.
2. Run paper mode locally for at least 1 week. Review the journal: hit rate, average win/loss, how many rejects each gate causes.
3. Tune the gates. Possibly add an LLM/social "narrative" scorer to the Analyst, a creator-wallet history check, and Pump.fun bonding-curve support.
4. Send live transactions through Jito / Helius Sender. Add a Telegram/Discord alert agent.
5. Go live with a small budget only after paper results are reviewed.

---

## Starting parameters — waiting on owner

Fill in or tell me these values. Defaults currently in `config/params.example.yaml` are shown.

| # | Parameter | Default | Owner value |
|---|---|---|---|
| 1 | Wallet public key (dedicated hot wallet) | — | set 2026-10-02 (address in the local `config/params.yaml`). Confirm whether this is the main wallet or a dedicated bot wallet. |
| 2 | Total budget `starting_sol` | 1.0 SOL | |
| 3 | Size per trade `per_trade_sol` | 0.05 SOL | |
| 4 | Max open positions | 3 | |
| 5 | Daily loss limit | 0.15 SOL | |
| 6 | Max drawdown kill switch | 30 % | |
| 7 | Stop loss | 20 % | |
| 8 | Take-profit ladder | +50 % sell ½, +150 % sell ¼ | |
| 9 | Trailing stop (after first TP) | 25 % from peak | |
| 10 | Max hold time | 240 min | |
| 11 | Min liquidity | $15k | |
| 12 | Token age window | 10 min – 72 h | |
| 13 | Max top-10 holders / single holder | 35 % / 10 % | |
| 14 | Min holders | 150 | |
| 15 | Min 5m volume / buy:sell ratio | $5k / 1.2 | |
| 16 | Slippage / max price impact | 3 % / 3 % | |
| 17 | Priority fee | 100k lamports | |
| 18 | Discovery sources (DexScreener, Pump.fun, Telegram calls, specific wallets to copy…) | DexScreener profiles + boosts | |
| 19 | RPC provider (Helius / Triton / QuickNode) | public RPC | |
| 20 | Strategy style: early snipes (<10 min) vs momentum on established tokens | momentum | **early snipes (pump.fun)** — 2026-10-02 |
| 21 | Sniper: SOL per entry / max positions / daily loss | 0.05 / 4 / 0.2 SOL | |
| 22 | Sniper: initials target / trailing tiers / stop loss | 2x / 30→15% / −30% | |
| 23 | Telegram channels & X accounts to watch | — | |
