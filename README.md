# meme_traderv1

Paper-first memecoin trading bots for **Solana**, in two parts:

- **pump.fun engine** (the main one). It watches every new pump.fun token as it launches and screens out rugs and insider launches. It trades late-curve "graduation plays" and early momentum, and can copy chosen wallets or act on calls from Telegram and X. A live dashboard shows every decision as it happens.
- **DexScreener momentum bot.** It scans trending Solana tokens on DexScreener, any token rather than only pump.fun launches, checks them with RugCheck and a sell-back test, and trades through Jupiter.

[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Tests: 144 passing](https://img.shields.io/badge/tests-144%20passing-brightgreen.svg)
![Mode: paper by default](https://img.shields.io/badge/mode-paper%20by%20default-orange.svg)

![Live dashboard during a real-market paper run](docs/dashboard.png)
<sub>Live view during a real-market paper run (2026-10-03): pretend money, real pump.fun data.</sub>

> **Status: paper trading.** The bot has only been tested with pretend money so far. Most pump.fun tokens go to zero. Nothing here is financial advice. Never trade money you can't afford to lose.

## Results so far

**No trading edge has been demonstrated yet.** Paper profits so far come from small samples and depend on a few big winners. The bot now has a research process built to confirm or reject one strategy at a time ([docs/RESEARCH.md](docs/RESEARCH.md)):
- the strategy is written down and frozen first;
- it's judged only on data recorded after freezing;
- the costs are net and measured;
- there are baselines and day-level uncertainty.

The first candidate, `graduation-v1`, is collecting its holdout. On 16 h of development data it made +1.55 SOL at $20 a trade with instant fills. With orders landing 1–3 s after the decision, at the price they find (today's realistic delay), it **lost** 0.4–1.0 SOL instead: it misses most of its biggest winners, which run away before a delayed buy lands (BUILD_LOG #16, #18). The paper bot now models that delay.

Earlier paper sessions are below for history. They used tiny samples, and partly a feed later measured to run 12 s behind the chain.

| Session | Data feed | Launches screened | Trades | Win rate | P&L |
|---|---|---|---|---|---|
| 2026-10-02, 48 min | PumpPortal (metered) | 1,959 | 7 | 29% | **+0.136 SOL** (+13.6%) |
| 2026-10-03, 1 h | Solana on-chain logs (free) | 1,465 | 13 | 38% | **+0.164 SOL** (+16.4%) |

By strategy, 2026-10-03:

| Strategy | Trades | Win rate | P&L |
|---|---|---|---|
| Graduation plays | 5 | 60% | +0.239 SOL |
| Early sniper | 6 | 17% | −0.074 SOL |
| $1 callouts | 2 | 50% | −0.001 SOL |

What's known so far:
- **Graduation plays made the paper profit in both sessions,** but that profit depends on a few winners and on entering fast; the frozen research run will decide. The early sniper lost (off).
- **About 8% of launches double within minutes.** The hard part is telling them apart from the ~92% that don't. The safety gates reject far more losers than winners: of 574 "serial deployer" rejects, 7% doubled before falling 30%, while 27% fell 30% first.
- **Completeness isn't enough: a feed must also be on time.** The free on-chain feed was complete, but PublicNode delivered it ~12 s behind the chain; the public RPC is 1–2 s behind. The watchdog now checks both.

## What it does

- **Sees every launch.** New tokens come from PumpPortal's free stream. Every trade is decoded from pump.fun's own on-chain logs over a Solana websocket. No paid data or API key is needed.
- **Screens for rugs.** It rejects a token if:
  - the creator bought a large share of their own token, or has sold;
  - the creator is a serial deployer, or the ticker is a copycat;
  - insiders bundled the first buys, or early buyers are dumping;
  - wallets in the funding graph form an insider cluster.
- **Trades two main strategies:**
  - **Graduation plays:** strong momentum late on the bonding curve, sold before the token migrates.
  - **Early sniper:** confirms momentum first, then rides it. It doesn't try to win block-0 races against insiders.
  - Optional: **copy trading** of chosen wallets (mirror their buys, or use them as a signal), **Telegram/X call signals** (contract addresses posted in channels you choose), and **$1 callout positions**.
- **Sells after graduation too.** Exits route to the bonding curve or, once a token has migrated, to its PumpSwap pool.
- **Manages risk:**
  - a dollar hard cap per buy, a daily loss limit and a drawdown kill switch;
  - a "defense mode" that halves size after a losing run;
  - position sizing at quarter-Kelly.
- **Measures itself:**
  - expectancy, profit factor and bootstrap confidence in the edge;
  - a gate audit that checks whether each rejection rule costs more than it saves;
  - a calibrated P(2×) prediction model, plus backtests, walk-forward sweeps and A/B comparisons on recorded data.

## How it works

```mermaid
flowchart LR
  PP["PumpPortal<br/>new launches + migrations<br/>(free)"] --> F[Feed]
  RPC["Solana RPC websocket<br/>pump.fun TradeEvent logs<br/>(free)"] --> F
  F --> T["Token tracker<br/>curve, holders, flow"]
  T --> G{"Safety gates"}
  G -- pass --> S["Strategies<br/>graduation · sniper · copy"]
  S --> R["Risk + sizing"]
  R --> X["Paper or live executor"]
  F --> REC[("data/feed-*.jsonl<br/>recordings")]
  REC --> BT["backtest · sweep · compare · train"]
  T --> UI["Dashboard<br/>127.0.0.1:8787"]
```

## Quick start

Paper trading on the real market needs **no keys and no wallet**.

```bash
git clone https://github.com/m4n3ce/meme_traderv1.git
cd meme_traderv1
bash scripts/setup_ubuntu.sh        # or: bash scripts/setup_mac.sh
scripts/start.sh doctor             # checks what's set up
scripts/start.sh paper              # real market, pretend money -> http://127.0.0.1:8787
```

Step-by-step guides with screenshots: [Mac](docs/MAC_SETUP.md) · [Ubuntu, including running 24/7 as a service](docs/UBUNTU_SETUP.md).

## Commands

| Command | What it does |
|---|---|
| `scripts/start.sh demo` | Simulated market. Good for learning the dashboard; its P&L means nothing. |
| `scripts/start.sh paper` | Real market, pretend money. Records the feed to `data/`. |
| `scripts/start.sh doctor` | Checks packages, data feeds, keys and wallet. |
| `scripts/start.sh report` | Shareable HTML report + CSV of your trades (`--mode paper` / `live`; demo, paper and live are never mixed). |
| `scripts/start.sh backtest` | Replays your recordings with the current settings. |
| `scripts/start.sh sweep --grid exit.stop_loss_pct=20,30,40` | Walk-forward parameter tuning with a noise guard. |
| `scripts/start.sh compare --variant "no_late: late.enabled=false"` | A/B test strategies across recorded days. |
| `scripts/start.sh train` | Fits and grades a candidate P(2×) model on your recordings. |
| `scripts/start.sh promote` | Deploys the candidate, only if trained on real recordings with skill on unseen launches. It stays display-only until `predict.display_only: false`. |
| `scripts/start.sh leaders` | Ranks wallets in your recordings worth copying. |
| `scripts/start.sh live` | **Real money.** Locked until you complete [SETUP.md step 5](docs/SETUP.md). |

Task recipes are in [docs/HOWTO.md](docs/HOWTO.md).

## AI operator: chat with Claude in the dashboard

The engine makes split-second decisions on its own. An AI operator works one level up, through tools exposed by the running bot: it reads status, positions, the radar, token detail and analytics, and can pause or resume entries, lower risk, turn strategies off, buy or sell, and top up the paper balance, always with a stated reason.

**In the dashboard:** open the **✦ Chat** tab (or press `c`) and type.
- Ask it anything: "how are we doing?", "anything to sell?". It reads the bot's data before it answers.
- Paste a token's **contract address** for an instant metrics card. It covers any Solana token, tracked or not: price, market cap, liquidity, candles, buys vs sells, top holders, mint/freeze authority, RugCheck flags, and Mayhem mode. Claude adds its read underneath.
- **Every action shows up as a card with Approve / Decline.** Reads run without asking.
- It runs Claude Code headless on your Claude plan, so no API key is needed. A meter shows your 5-hour and weekly usage.
- Claude gets only the bot's tools here: no shell, no files, no other connectors.

**Risk dial:** five levels on the Live and Controls tabs: Cautious, Normal (your settings), Bold, Aggressive and Max. Each scales trade size, open positions and the daily loss limit together. Say "more risk" in Chat and Claude asks you to approve a higher level. It can always turn the dial down, but only raises it with your click, and never above `risk.max_level`. The kill switch, stop losses and entry rules never move.

**In a terminal:** the same tools load in Claude Code from `.mcp.json`.

```bash
cd ~/meme_traderv1 && claude
> /desk-check                     # full review: feed, P&L, positions, radar, analytics -> act or not
> is the market hot enough for graduation plays right now?
```

The limits live in the bot, not in the prompt:
- the agent can lower any risk setting, but never raise sizing, positions, the loss limit or the stop loss above your config;
- it can't re-enable a strategy you turned off, switch to live, save risk settings or press the kill switch;
- paper deposits only, capped per deposit (`sniper.agent.max_deposit_sol`); they count as capital, not profit;
- its buys pass the bot's normal risk checks and are rate-limited (`sniper.agent` in the config);
- every action carries a reason, journaled and shown on the dashboard;
- read-only tools run freely; each action asks you first (Approve in the dashboard, or `.claude/settings.json` in a terminal).

## Screenshots

| Live: risk dial, positions, market pulse | Chat: ask, approve actions, paste a contract address |
|---|---|
| ![Live view](docs/img/live.png) | ![Chat with Claude](docs/img/chat.png) |
| **Token detail: every gate, live** | **Analytics: P&L by strategy and exit reason** |
| ![Token detail with gate checklist](docs/img/token-detail.png) | ![Analytics view](docs/img/analytics.png) |

Screenshots are from the built-in demo (`scripts/start.sh demo`, a simulated market), so the profits in them aren't results. Real results are under [Results so far](#results-so-far).

## Configuration

- **Settings:** defaults are in [`config/params.example.yaml`](config/params.example.yaml), with every option commented. Put your overrides in `config/params.yaml`, which is git-ignored. The dashboard's **Controls → Save** writes there too.
- **Keys:** go in `.env`; see [`.env.example`](.env.example). All are optional for paper trading:

| Variable | Used for |
|---|---|
| `SOLANA_WS_URL` | Trade data websocket (default: the free public RPC). Use a flat-rate endpoint if you change it: the stream is ~20 GB/day. |
| `ANTHROPIC_API_KEY` | Optional AI trading desk (`--desk`) and post-mortem review agent |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID` | Phone alerts for buys, closes and errors |
| `SOLANA_RPC_URL`, `SOLANA_KEYPAIR_PATH`, `MEME_TRADER_CONFIRM_LIVE` | Live trading only |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `X_BEARER_TOKEN` | Call signals from Telegram channels / X accounts |
| `JUPITER_API_KEY` | DexScreener bot: realistic paper quotes + sell-back check; required for its live mode |
| `PUMPPORTAL_API_KEY` | Only for the metered PumpPortal trade stream (`sniper.feed.trades: pumpportal`) |

`data/agent.token` is created fresh at every start for the AI operator tools. It isn't a setting, and it's readable only by your user.

## Going live

Don't, until paper results across several days justify it. When they do, follow [docs/SETUP.md step 5](docs/SETUP.md#5-going-live-real-money):
- use a **dedicated bot wallet** that holds only the budget, never your main wallet;
- use a paid RPC;
- start small.

Live mode is locked behind `--live`, `MEME_TRADER_CONFIRM_LIVE=yes` and a matching keypair. The dashboard's **KILL** button sells everything. Built-in safeguards:
- each transaction is simulated before signing and refused if it would move more SOL than the order allows;
- an order whose outcome is unclear is looked up on-chain, never blindly re-sent;
- buys reserve their cash before they're sent;
- the ledger is checked against the wallet's real balance;
- only one bot can trade a wallet at a time.

These have been tested against simulated failures, not yet with real money.

## Documentation

| Doc | Contents |
|---|---|
| [SETUP.md](docs/SETUP.md) | What to connect, costs, and going live safely |
| [MAC_SETUP.md](docs/MAC_SETUP.md) · [UBUNTU_SETUP.md](docs/UBUNTU_SETUP.md) | Illustrated install guides |
| [HOWTO.md](docs/HOWTO.md) | Task recipes and troubleshooting |
| [STRATEGY.md](docs/STRATEGY.md) | Market research, strategy reasoning and sources |
| [RESEARCH.md](docs/RESEARCH.md) | How a strategy is frozen, evaluated and judged (and what's been found) |
| [BUILD_LOG.md](docs/BUILD_LOG.md) | Every decision, measurement and result, newest first |
| [MULTICHAIN.md](docs/MULTICHAIN.md) | Can it trade other chains? Feasibility, plan and costs (on hold) |

## Project layout

```
meme_trader/sniper/   pump.fun engine: feeds, tracker, strategies, risk, analytics, CLI
meme_trader/ui/       dashboard server + single-page UI
meme_trader/agents/   DexScreener momentum bot: scout, safety, analyst, risk, executor, monitor
config/               params.example.yaml (all settings, commented)
scripts/              setup and start scripts for Mac / Ubuntu
deploy/               systemd service
tests/                pytest suite (144 tests)
```

### DexScreener momentum bot

A separate, simpler bot for **any Solana token**, not only pump.fun launches:
1. A scout pulls fresh and boosted tokens from DexScreener.
2. Safety checks run RugCheck, holder concentration, and a buy-then-sell quote round-trip to catch tokens that can't be sold.
3. An analyst scores momentum, risk sizes the position, and the executor trades through Jupiter.

```bash
cp config/params.example.yaml config/params.yaml   # edit values
python -m meme_trader --once                       # one cycle
python -m meme_trader                              # loop (paper by default)
```

Every decision is written to `data/journal-YYYY-MM-DD.jsonl`. Live mode needs `JUPITER_API_KEY`, a dedicated wallet keypair and `MEME_TRADER_CONFIRM_LIVE=yes`. It runs on live DexScreener and RugCheck data in paper mode, but it's less developed than the pump.fun engine and hasn't had an extended paper run yet.

## Development

```bash
.venv/bin/python -m pytest -q
```

## License

[GPL-3.0](LICENSE). You may use, study, change and share this code. Anything you distribute that's built on it must also be released under GPL-3.0. It comes with **no warranty**: you are responsible for any money you trade with it.
