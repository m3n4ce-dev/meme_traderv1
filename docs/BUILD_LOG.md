# Build Log — meme_traderv1

A running record of decisions, research, parameters and status. Newest entries at the top.

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
| 1 | Wallet public key (dedicated hot wallet) | — | |
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
