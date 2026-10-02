# meme_traderv1
First build 10/2/2026

Multi-agent Solana meme-token trading bot: Scout → Safety → Analyst → Risk → Executor, with a Monitor agent that handles exits.
It starts in **paper mode** by default. Design, research notes, status and the parameter checklist are in [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

## Pump.fun sniper + dashboard (main strategy)
```bash
pip install -r requirements.txt
python -m meme_trader.sniper run --synthetic --speed 5   # demo market -> open http://127.0.0.1:8787
python -m meme_trader.sniper run                         # live pump.fun feed, PAPER trades, records feed to data/
python -m meme_trader.sniper backtest --file data/feed-*.jsonl --set exit.stop_loss_pct=25
```
Strategy and research: [docs/STRATEGY.md](docs/STRATEGY.md). Optional env vars:
- `PUMPPORTAL_API_KEY`: token-trade stream
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`: Telegram call ingestion
- `X_BEARER_TOKEN`: X call ingestion

![dashboard](docs/dashboard.png)

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
