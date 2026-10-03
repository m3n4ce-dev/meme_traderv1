# meme_traderv1
First build 10/2/2026

Multi-agent Solana meme-token trading bot: Scout → Safety → Analyst → Risk → Executor, with a Monitor agent that handles exits.
It starts in **paper mode** by default. Design, research notes, status and the parameter checklist are in [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

## Start here
| Your computer | Guide | One-line install |
|---|---|---|
| Mac | **[docs/MAC_SETUP.md](docs/MAC_SETUP.md)** | `bash scripts/setup_mac.sh` |
| Ubuntu (home mini PC, 24/7) | **[docs/UBUNTU_SETUP.md](docs/UBUNTU_SETUP.md)** | `bash scripts/setup_ubuntu.sh --service` |

Then start the bot with `scripts/start.sh demo` (simulated) or `scripts/start.sh paper` (real market, fake money).
What each key/account is for and how to go live safely: [docs/SETUP.md](docs/SETUP.md). Task recipes: **[docs/HOWTO.md](docs/HOWTO.md)**.

## Pump.fun sniper + copy trader + AI desk (main strategy)
- **Strategies:**
  - early sniper ("confirm, then ride");
  - copy trading of leader wallets;
  - graduation plays (late-curve momentum, out before migration);
  - $1 callout bags.
- **Prediction:**
  - a calibrated P(2x before −30%) model trained walk-forward on your own recordings;
  - it feeds quarter-Kelly sizing and an optional entry gate.
- **Risk:**
  - a dollar hard cap per buy, a daily loss limit and a drawdown kill switch;
  - defense mode after a losing run;
  - insider-cluster detection from the funding graph.
- **Dashboard** (`http://127.0.0.1:8787`), in four views:
  - **Live**: positions, radar and toasts;
  - **Analytics**: highlights, projection, gate audit and model card;
  - **Controls**: live settings;
  - **Guide**.
  - Click any token for its gate checklist, P(2x), drivers, holders and tape.

```bash
scripts/start.sh demo        # simulated market -> dashboard
scripts/start.sh paper       # live pump.fun feed, PAPER trades, records the feed to data/
scripts/start.sh doctor      # what's set up / missing
scripts/start.sh train       # fit + grade the P(2x) model on your recordings (hot-reloads into a running bot)
scripts/start.sh sweep --grid exit.stop_loss_pct=20,30,40                 # walk-forward tuning, all CPU cores
scripts/start.sh compare --variant "late: late.enabled=true"            # A/B strategies across many days
scripts/start.sh report      # shareable HTML report + CSV of your trades
scripts/start.sh backtest    # replay the recordings with current settings
scripts/start.sh leaders     # wallets worth copying
scripts/start.sh review      # AI post-mortem; proposals are backtested before you apply them
```
The analysis commands use your recordings (`data/feed-*`) automatically, or the simulated market if there are none yet.
Strategy and research: [docs/STRATEGY.md](docs/STRATEGY.md). Optional env vars:
- `SOLANA_WS_URL`: Solana websocket for trade data (default: the free public RPC). Trades come from pump.fun's on-chain logs, so no paid data key is needed
- `PUMPPORTAL_API_KEY`: only for the metered PumpPortal trade stream (`sniper.feed.trades: pumpportal`)
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`: Telegram call ingestion
- `X_BEARER_TOKEN`: X call ingestion
- `ANTHROPIC_API_KEY`: AI trading desk (`run --desk`) and the review agent

![dashboard](docs/dashboard.png)

| Token detail | Analytics |
|---|---|
| ![token detail](docs/img/token-detail.png) | ![analytics](docs/img/analytics.png) |

## DexScreener momentum bot (original scaffold, paper trading)
```bash
pip install -r requirements.txt
cp config/params.example.yaml config/params.yaml   # edit values
export JUPITER_API_KEY=...        # optional in paper mode (gives realistic quotes + honeypot check)
python -m meme_trader --once      # one cycle
python -m meme_trader             # loop
pytest -q
```
Every decision is written to `data/journal-YYYY-MM-DD.jsonl`. Portfolio state is in `data/state.json`.

## Live trading (real funds)
Use a **dedicated hot wallet** holding only the bot's budget.
```bash
export SOLANA_KEYPAIR_PATH=~/.config/solana/bot.keypair.json   # never commit this
export SOLANA_RPC_URL=https://<paid-rpc>
export JUPITER_API_KEY=...
export MEME_TRADER_CONFIRM_LIVE=yes
# and set mode: live + wallet.pubkey in config/params.yaml
python -m meme_trader
```
Nothing here is financial advice. Most meme tokens go to zero, so only trade money you can afford to lose.
