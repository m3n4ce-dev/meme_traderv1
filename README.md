# meme_traderv1

Paper-first memecoin trading bots for **Solana**, in two parts:

- **pump.fun engine** (the main one). It watches every new pump.fun token as it launches and screens out rugs and insider launches. It trades late-curve "graduation plays" and early momentum, and can copy chosen wallets or act on calls from Telegram and X. A live dashboard shows every rule, every setting and every decision as it happens, with AI personas that vote on entries and an edge check that says whether a result is real or luck.
- **DexScreener momentum bot.** It scans trending Solana tokens on DexScreener, any token rather than only pump.fun launches, checks them with RugCheck and a sell-back test, and trades through Jupiter.

[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Tests: 1,179 passing](https://img.shields.io/badge/tests-1%2C179%20passing-brightgreen.svg)
![Mode: paper by default](https://img.shields.io/badge/mode-paper%20by%20default-orange.svg)

![The trading room: the bots at their desks and the AI team voting on a coin](docs/img/desk.png)
<sub>The Desk tab during a real-market paper run (2026-10-06): the AI team votes out loud, with real reasons from the data.</sub>

> **Status: paper trading.** The bot has only been tested with pretend money so far. Most pump.fun tokens go to zero. Nothing here is financial advice. Never trade money you can't afford to lose.

## Results so far

**No trading edge has been proven yet, and four days of testing say why.** The paper bot's own **edge check** (Analytics tab, or `scripts/start.sh edge`) judges each strategy the way a skeptical group would:

| Strategy | Paper trades | Per SOL staked | Average trade (90% range) | Without its best 3 trades | Verdict |
|---|---|---|---|---|---|
| Graduation plays | 225 | +0.3% | +1.9% (−3.5% to +7.6%) | −4.16 SOL | can't tell yet: the range includes zero |
| Early sniper (now off) | 78 | −13.4% | −13.8% (−17.0% to −10.6%) | −2.37 SOL | losing, not by bad luck |

<sub>Real market, pretend money, 2026-10-03 to 10-06. Fills are modelled: a 2.5 s delay to land, curve fees, and failed orders. The paper account hit its drawdown kill switch on 10-06; testing goes on without it (below).</sub>

### Where things stand (2026-10-08, [build log #61–#82](docs/BUILD_LOG.md))

- **The paper account is still halted** by its drawdown kill switch (since 2026-10-06 11:46 UTC). Lifting it is the owner's call. Research and measurement go on without it.
- **A live price watcher measures what trading would really cost** (measurement only, nothing is sent):
  - it takes executable PumpSwap quotes from on-chain state, priced by the official SDK's math. An external reviewer re-derived 250 of 250 recorded quotes exactly from their raw accounts;
  - **trading costs more than the research assumed:** a 0.25 SOL buy-then-sell on 40 pools like the ones tested costs a **median 3.2%** in fees and impact, plus about 0.8% in network fees. The research had assumed 1.2% for the whole round trip.
- **A 14-day measurement of quote availability** ([Q1](research/registrations/Q1-quote-qualification.md)) runs 2026-10-09 to 10-22, registered before any data:
  - it's descriptive, with no pass/fail, because a calibration showed no available bound can certify 95% availability in 14–28 days;
  - its outage stress profile is fixed in code before the window opens.
- **A shadow ledger checks the paper account's books** against a transactional ledger prototype. It's non-authoritative and paper only, and it has been on since 10-08. A crash matrix at the bot's real write boundaries found and fixed three ways a crash or a full disk could leave the books and the account journal disagreeing.
- **Every change goes through external review.** Fifteen rounds so far. Each round's findings, the reviewer's tests and the fixes are in the build log.

### What four days of data say (2026-10-06, [build log #56–#60](docs/BUILD_LOG.md))

- **The edge on new coins is speed, and a 2.5 s bot doesn't have it.**
  - A gradient-boosted model (36 features, trained and graded on different days) sorts new coins well: AUC 0.86, against 0.83 for the first model. Its estimates match what happens.
  - Its picks made +11% to +16% a trade with instant fills on two unseen days. Filled 0.5–2.5 s late, they made about nothing.
  - Live since 10-06, 155 picks followed in the exit lab have lost 9–25% a pick, instant fills included. It's a small evening sample, and it agrees with the rest.
- **About 3,000 entry rules lost on every day tested.** That includes the owner's own manual style turned into rules (buy the dip on a half-full curve with heavy buying, sell fast), 540 variants of it. Coins 5–30 minutes old lost too.
  - At this speed the bonding curve looks efficient: buying loses roughly the fees plus a little.
- **The bots' exits aren't the problem; the entries are.** Of the last 37 coins the bots sold, 24 are 20%+ lower now (median −35% since the sell).
- **The AI team (four personas on Claude) hasn't picked better coins yet.** Over 153 scored calls, its buys did 10 points worse than its passes at the median.
  - It practices whenever the bot can't buy, and each persona reads its own record at every vote.
  - Since 10-06 each persona must name who is selling to the bot, and why they're wrong, before voting buy.
- **What's being collected for the next tests:**
  - who posted each coin's X link, their reach and how fresh it is;
  - the minute-by-minute price of every coin after it graduates, a slower market with lower fees where seconds matter less.

**The frozen test.** The research process ([docs/RESEARCH.md](docs/RESEARCH.md)) confirms or rejects one strategy at a time:
- the strategy is written down and frozen first;
- it's judged only on data recorded after freezing;
- the costs are net and measured;
- there are baselines and day-level uncertainty.

`graduation-v1` is collecting its holdout; its verdict is due around 2026-10-17. A second candidate, `wallets-v1`, tests "follow wallets that were early on several runners" on coins at least 14 days old, with its rules registered before any data ([docs/WALLETS.md](docs/WALLETS.md)).

Earlier findings:
- **About 8% of launches double within minutes.** The hard part is telling them apart from the ~92% that don't. The safety gates reject far more losers than winners: of 574 "serial deployer" rejects, 7% doubled before falling 30%, while 27% fell 30% first.
- **Completeness isn't enough: a feed must also be on time.**
  - The free on-chain feed is complete when it's up, but it drops and lags: up to 207 unreliable minutes a day by 10-06.
  - A flat-rate paid websocket (RPC Fast) has run 1.3–1.6 s behind the chain with no drops.
  - The watchdog checks completeness and lag, tolerates trades delivered out of order, and remembers which endpoint was fastest.

## What it does

- **Sees every launch.** New tokens come from PumpPortal's free stream. Every trade is decoded from pump.fun's own on-chain logs over a Solana websocket. No paid data or API key is needed; a flat-rate paid websocket makes it steadier (see Configuration).
- **Screens for rugs.** It rejects a token if:
  - the creator bought a large share of their own token, or has sold;
  - the creator is a serial deployer, or the ticker is a copycat;
  - insiders bundled the first buys, or early buyers are dumping;
  - wallets in the funding graph form an insider cluster.
- **Trades two main strategies:**
  - **Graduation plays:** strong momentum late on the bonding curve, sold before the token migrates.
  - **Early sniper:** confirms momentum first, then rides it. It doesn't try to win block-0 races against insiders.
  - Optional: **copy trading** of chosen wallets, **Telegram/X call signals** (contract addresses posted in channels you choose), and **$1 callout positions**.
- **Lets AI personas vote.** Four personas (veteran, skeptic, narrative, quant) can review every entry the rules pick before it's bought. They run on Claude, or on something cheaper or free:
  - GitHub Models, Hugging Face, OpenRouter;
  - a model on your own machine (Ollama, LM Studio).

  The dashboard shows what a review costs (about 2 cents on Claude Opus) and has a Test button. They also read every note you save, and answer questions like "what's holding us back?" from the bot's real numbers.
  - **Every vote is scored:** what the coin did next, on the bot's own exits, whether the team said buy or pass (Analytics → The AI team's record).
  - Each persona reads its own record at every vote.
  - When the bot can't buy (a strategy off, the kill switch, the daily limit), the team keeps **practicing** on paper.
- **Manages risk:**
  - one **risk dial** (Cautious to Max) scales trade size, open positions and the daily loss limit together;
  - a dollar hard cap per buy, a daily loss limit and a **drawdown kill switch**;
  - after a halt, **Resume trading** re-measures the kill switch from where you resumed;
  - a "defense mode" that halves size after a losing run;
  - position sizing at quarter-Kelly.
- **Measures itself:**
  - the **edge check**: per strategy, a 90% range, the result without its best 3 trades, and day by day;
  - the **exit lab**: every other exit rule, run in the shadows on the same entries with fees;
  - expectancy, profit factor, and a gate audit that checks whether each rejection rule costs more than it saves;
  - a calibrated P(2×) model, and a stronger gradient-boosted one whose picks are followed live in the exit lab. Each pick is filled twice, instantly and as late as the bot lands, to measure what speed is worth;
  - backtests, walk-forward sweeps and A/B comparisons on recorded data, and a lab that tests one setting at a time on the last 24 h;
  - **After you sold:** where each coin is now, against the price you sold at.
- **Keeps a record nobody can rewrite.** Every 📣 call goes into a hash-chained ledger and is scored after costs at fixed horizons. You can publish the ledger's head to X or Telegram ([EDGE_PROOF.md](docs/EDGE_PROOF.md)).

## Trading by hand

Manual trading is yours: the bot never blocks or resizes your trades.
- **Paste a contract address** (or press Trade on any coin) and the coin's **live price chart** opens with its metrics: price, market cap, liquidity, volume, buys vs sells, holders, dev share, RugCheck and the bot's own red flags.
- **Buy or add, then sell:** sell 25% or 50%, take **Initials**, or **Exit**. Your positions exit only on the stop, take profit or trail you set on each card.
- **Limit orders and alerts by market cap:** buy the dip, sell into strength, or just get pinged. They survive restarts.
- **💡 Tips after your trades.** A small box can appear for:
  - a big share of the account;
  - red flags;
  - a quiet coin;
  - a losing streak;
  - a quick flip that only paid fees (about 6% round trip).

  They never block anything, and you can turn them off.
- **🤖 Hand a position to the bots** (or 🚶 **Away** for all of them). They ride it for a runner:
  - half comes out at 2x;
  - the rest trails 30% off its peak;
  - a stop sits 40% down;
  - there are no time or stall exits.
- **Graduated coins** (Pulse's Graduated column, or a pasted address) trade on paper at their PumpSwap pool price.
- **⚡ Pulse** lists new launches, the final stretch and fresh graduates, with filters and one-click buys.

![Pulse: new launches, the final stretch and fresh graduates](docs/img/pulse.png)

## How it works

```mermaid
flowchart LR
  PP["PumpPortal<br/>new launches + migrations<br/>(free)"] --> F[Feed]
  RPC["Solana RPC websocket<br/>pump.fun TradeEvent logs<br/>(free, or flat-rate paid)"] --> F
  F --> T["Token tracker<br/>curve, holders, flow"]
  T --> G{"Safety gates"}
  G -- pass --> S["Strategies<br/>graduation · sniper · copy"]
  S --> D["AI desk<br/>(optional vote)"]
  D --> R["Risk + sizing"]
  R --> X["Paper or live executor"]
  F --> REC[("data/feed-*.jsonl<br/>recordings")]
  REC --> BT["backtest · sweep · compare · train"]
  T --> UI["Dashboard<br/>127.0.0.1:8787"]
```

## Quick start

Paper trading on the real market needs **no keys and no wallet**.

```bash
git clone https://github.com/m3n4ce-dev/meme_traderv1.git
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
| `scripts/start.sh edge` | The edge check in plain text, ready to paste into a group (`--days 14`, `--mode paper`). |
| `scripts/start.sh report` | Shareable HTML report + CSV of your trades (`--mode paper` / `live`; demo, paper and live are never mixed). |
| `scripts/start.sh calls verify` | Checks the call ledger's hash chain (`calls stats`, `calls list` too). |
| `scripts/start.sh backtest` | Replays your recordings with the current settings. |
| `scripts/start.sh sweep --grid exit.stop_loss_pct=20,30,40` | Walk-forward parameter tuning with a noise guard. |
| `scripts/start.sh compare --variant "no_late: late.enabled=false"` | A/B test strategies across recorded days. |
| `scripts/start.sh research eval graduation-v1` | Evaluates a frozen research policy (`data`, `freeze`, `final`, `log`). |
| `scripts/start.sh train` / `promote` | Fits a candidate P(2×) model; deploys it only with skill on unseen launches. |
| `scripts/start.sh leaders` | Ranks wallets in your recordings worth copying. |
| `scripts/start.sh live` | **Real money.** Locked until you complete [SETUP.md step 5](docs/SETUP.md). |

Task recipes are in [docs/HOWTO.md](docs/HOWTO.md).

## The dashboard

- **Tabs:** Live, Pulse, Desk, Chat, Portfolio, Analytics (with the 📣 calls track record), Controls, Guide.
  - **Ctrl+K** finds anything: tabs, coins, settings, guide pages.
  - **✎ Arrange** lets you drag, resize and hide any panel.
- **>_ Terminal** (the `` ` `` key): every command, typed.
  - `status`, `pos`, `buy BONK 0.1`, `sell all`, `alert BONK >= 1m`, `limit buy BONK <= 50k 0.1`;
  - `hand all`, `away on`, `risk 2`, `desk on`, `unhalt`, `log 20`, `ask …`.
  - It runs bot commands only, the same as the buttons, never shell commands.
- **↻ Restart** (next to KILL) saves everything, reloads the code and settings, and picks up where it left off.
- **Controls:** every setting, live, with Save. API keys set and tested from the page, with Test buttons for the Anthropic key, the RPC, the websocket and Helius.
- **Desk:** the bots as Habbo-style pixel people in an isometric house: the trading floor, a lounge (they sleep in bunks there while the AI desk rests) and a research lab with live study boards.
  - The scanner carries coins to the AI table, and the personas vote out loud.
  - The P&L board and the weather in the window follow the day.
  - Edit the room, dress the bots, or click one to see its screen.
- **✦ Chat:** ask Claude about the bot, or paste a contract address for a metrics card. Every action it proposes waits for your Approve. It runs Claude Code headless on your Claude plan, so no API key is needed.

The AI operator's limits live in the bot, not in the prompt:
- it can lower any risk setting, but never raise sizing, positions, the loss limit or the stop loss above your config;
- it can't re-enable a strategy you turned off, switch to live, save risk settings, or press or lift the kill switch;
- paper deposits only, capped per deposit; they count as capital, not profit;
- its buys pass the bot's normal risk checks and are rate-limited;
- every action carries a reason, journaled and shown on the dashboard, and asks you first.

The same tools load in Claude Code from `.mcp.json`:

```bash
cd ~/meme_traderv1 && claude
> /desk-check                     # full review: feed, P&L, positions, radar, analytics -> act or not
```

## Screenshots

| Live: the paper account, as it is (halted by its kill switch) | Charts: graduation watch and market pulse |
|---|---|
| ![Live tab](docs/dashboard.png) | ![Charts tab](docs/img/charts.png) |
| **Analytics: the AI team's record, every call scored** | **Exit lab: the stronger model's picks, instant vs 2.5 s late** |
| ![The AI team's record](docs/img/team.png) | ![Model picks in the exit lab](docs/img/picks.png) |
| **After you sold: where each coin is now** | **Analytics: the edge check** |
| ![After you sold](docs/img/after-exit.png) | ![Edge check](docs/img/analytics.png) |
| **AI desk model: Claude, or cheaper** | **Terminal** |
| ![AI desk model panel](docs/img/desk-model.png) | ![Terminal](docs/img/terminal.png) |
| **Chat: ask, approve actions, paste a contract address** | |
| ![Chat with Claude](docs/img/chat.png) | |

Most screenshots are from the real-market paper bot (pretend money), taken 2026-10-06. The terminal and AI-model panel are from the built-in demo (`scripts/start.sh demo`, a simulated market), whose numbers aren't results.

## Configuration

- **Settings:** defaults are in [`config/params.example.yaml`](config/params.example.yaml), with every option commented. Put your overrides in `config/params.yaml`, which is git-ignored. The dashboard's **Controls → Save** writes there too.
- **Keys:** go in `.env` (see [`.env.example`](.env.example)), or set them on the dashboard under **Controls → API keys**. All are optional for paper trading:

| Variable | Used for |
|---|---|
| `SOLANA_WS_URL` | Trade data websocket (default: free public endpoints, which drop and lag at busy hours). A flat-rate paid plan is steadier: RPC Fast's Focus plan ran 1.3–1.6 s behind with no drops (2026-10-06). The stream is ~40 GB/day (1.5–1.7 GB an hour), so avoid plans billed by data or credits. |
| `SOLANA_RPC_URL` | Lookups, balances, the Portfolio tab and live trading. Helius: `https://mainnet.helius-rpc.com/?api-key=…` (set for you when you save a Helius key). |
| `HELIUS_API_KEY` | Wallet-funding lookups with exchange labels (insider clusters) |
| `ANTHROPIC_API_KEY` | AI desk on Claude, and the post-mortem review agent |
| `GITHUB_MODELS_TOKEN`, `HF_TOKEN`, `OPENROUTER_API_KEY` | AI desk on cheaper or free models (a local Ollama needs no key) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`, `TELEGRAM_CHANNEL_ID` | Phone alerts, and posting calls and cards to a channel |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` (or `X_CLIENT_ID` / `X_CLIENT_SECRET`) | Posting to X from the dashboard (your own developer app) |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `X_BEARER_TOKEN` | Call signals from Telegram channels / X accounts |
| `SOLANA_KEYPAIR_PATH`, `MEME_TRADER_CONFIRM_LIVE` | Live trading only |
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
- only one bot can trade a wallet at a time;
- graduated coins trade on paper only: their price only updates every 10 s after graduation.

These have been tested against simulated failures, not yet with real money.

## Documentation

| Doc | Contents |
|---|---|
| [SETUP.md](docs/SETUP.md) | What to connect, costs, and going live safely |
| [MAC_SETUP.md](docs/MAC_SETUP.md) · [UBUNTU_SETUP.md](docs/UBUNTU_SETUP.md) | Illustrated install guides |
| [HOWTO.md](docs/HOWTO.md) | Task recipes and troubleshooting |
| [STRATEGY.md](docs/STRATEGY.md) | Market research, strategy reasoning and sources |
| [RESEARCH.md](docs/RESEARCH.md) | How a strategy is frozen, evaluated and judged (and what's been found) |
| [WALLETS.md](docs/WALLETS.md) | The wallet study: rules, data sources and their limits, timeline |
| [EDGE_PROOF.md](docs/EDGE_PROOF.md) | Proving an edge to a group: the call ledger, honest scoring, publishing proofs, and a plan for splitting into repos |
| [OPEN_SOURCE_NOTES.md](docs/OPEN_SOURCE_NOTES.md) | What open-source trading bots on GitHub do, which ones are bait, and what we took |
| [BUILDS_RESEARCH.md](docs/BUILDS_RESEARCH.md) | The best "agents in a room" builds (Pixel Agents, Habbo engines, Axiom) and what the trading room took from them |
| [BUILD_LOG.md](docs/BUILD_LOG.md) | Every decision, measurement and result, newest first |
| [MULTICHAIN.md](docs/MULTICHAIN.md) | Can it trade other chains? Feasibility, plan and costs (on hold) |

## Project layout

```
meme_trader/sniper/   pump.fun engine: feeds, tracker, strategies, risk, AI desk, edge check, exit lab, CLI
meme_trader/ui/       dashboard server + single-page UI, keys, posting to X / Telegram
meme_trader/agents/   DexScreener momentum bot: scout, safety, analyst, risk, executor, monitor
meme_trader/wallets/  the wallet study recorder and evaluation
research/policies/    frozen research policies (one file per idea)
config/               params.example.yaml (all settings, commented)
scripts/              setup and start scripts for Mac / Ubuntu
deploy/               systemd services
tests/                pytest suite (288 tests)
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
