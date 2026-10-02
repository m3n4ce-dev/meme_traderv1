# Setup — connect and run

Go in this order. Each stage works before the next one is added. **Don't skip paper trading.**

## What you need

| # | Thing | Needed for | Cost | Where |
|---|---|---|---|---|
| 1 | A computer that stays on: your PC/Mac for testing, a VPS for 24/7 | everything | — / ~$10–40/mo | any VPS. For live trading, pick a US-East or Frankfurt/Amsterdam region, close to Solana validators |
| 2 | Python 3.11+ and git | everything | free | python.org |
| 3 | **PumpPortal API key**, with its linked wallet funded with ≥ 0.02 SOL | live market data (trades, wallet streams for copy trading) | 0.01 SOL per 10,000 trades streamed | pumpportal.fun |
| 4 | Anthropic API key | AI trading desk + review agent (optional) | pay per use; the desk dashboard shows a running $ meter | console.anthropic.com |
| 5 | **A dedicated bot wallet** (keypair file) | live trading only | — | `solana-keygen new` (step 5) |
| 6 | Paid Solana RPC (Helius / Triton / QuickNode) | live trading only | free tier → ~$50/mo | helius.dev |
| 7 | Telegram API id/hash, X bearer token | social call signals (optional) | X is pay-per-use | my.telegram.org, developer.x.com |

## 1. Install
```bash
git clone https://github.com/m4n3ce/meme_traderv1.git
cd meme_traderv1
git checkout claude/kind-ritchie-8xooqh
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then fill in keys as you get them
python -m meme_trader.sniper doctor                      # tells you what's missing and how to fix it
```

## 2. See it work (no keys, no money)
```bash
python -m meme_trader.sniper run --synthetic --speed 5
```
Open http://127.0.0.1:8787. This is a **simulated** market with simulated wallets to copy. Use it to learn the dashboard. Its P&L means nothing.

## 3. Paper-trade the real market (needs the PumpPortal key)
```bash
python -m meme_trader.sniper run
```
- This uses real pump.fun launches and trades, with **fake money** filled at real bonding-curve prices.
- Every event is saved to `data/feed-YYYY-MM-DD.jsonl`, so the bot can be backtested on real data.
- **Leave it running for 3–7 days.** Then:
```bash
python -m meme_trader.sniper backtest --file data/feed-*.jsonl                # replay with current params
python -m meme_trader.sniper backtest --file data/feed-*.jsonl --set exit.stop_loss_pct=25   # try a change
python -m meme_trader.sniper leaders  --file data/feed-*.jsonl                # wallets worth copying
python -m meme_trader.sniper review --validate                                # AI post-mortem (needs Anthropic key)
```

## 4. Copy trading
1. Find wallets in either of these places:
   - **from the `leaders` command** on your recorded data. This is best, because it ranks wallets by real results and flags insider-like ones.
   - **from public trackers** such as kolscan.io or gmgn.ai, which show leaderboards of profitable wallets.
2. Add them to `config/params.yaml`:
   ```yaml
   sniper:
     copy:
       leaders:
         - {address: <wallet>, label: whale1, mode: signal}   # start as signal (score boost only)
         - {address: <wallet>, label: whale2, mode: mirror}   # mirror = buy when they buy, sell when they sell
   ```
3. Run in paper mode. The **Copy trading** panel shows *our* results copying each wallet. A wallet that loses 5 copied trades in a row is auto-paused.

## 5. Going live (real money)
1. **Create a separate bot wallet. Don't use your main wallet.** The bot needs the wallet's private key in a file on the machine. Never put your main wallet's key on a box running a bot.
   ```bash
   sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"   # Solana CLI
   solana-keygen new -o ~/.config/solana/meme-bot.json
   chmod 600 ~/.config/solana/meme-bot.json
   solana-keygen pubkey ~/.config/solana/meme-bot.json            # -> put this in wallet.pubkey
   ```
   Your current address `HTG4jCTAVB6H8QThytKtrApjNaQhMuUENFi9Whppag6Z` is set as `wallet.pubkey`. If that's your main wallet, keep it as the wallet you **fund the bot wallet from**, and put the bot wallet's address in `config/params.yaml` instead.
2. Send the bot wallet **only** the budget: `sniper.capital.starting_sol` plus ~0.05 SOL for fees.
3. In `.env`, set `SOLANA_RPC_URL` (paid RPC), `SOLANA_KEYPAIR_PATH`, and `MEME_TRADER_CONFIRM_LIVE=yes`.
4. Run `python -m meme_trader.sniper doctor`. Every row should be OK.
5. Start small:
   ```bash
   python -m meme_trader.sniper run --live            # add --desk to turn on the AI desk
   ```
   Safety rails:
   - The bot refuses to start if the keypair doesn't match `wallet.pubkey` or the wallet holds less than the budget.
   - The KILL button sells everything.
   - The drawdown kill switch and the daily loss limit always apply.

## Running 24/7 on a VPS
```bash
tmux new -s bot
source .venv/bin/activate && python -m meme_trader.sniper run      # Ctrl-b d to detach
ssh -L 8787:127.0.0.1:8787 you@your-vps                            # view the dashboard from your laptop
```
The dashboard binds to 127.0.0.1 on purpose. Its buttons can sell, so reach it through an SSH tunnel and never expose it publicly.
