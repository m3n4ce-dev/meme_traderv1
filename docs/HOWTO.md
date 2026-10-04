# How-tos

Short recipes for the things you'll actually do. Every command runs from the `meme_traderv1` folder.
Setup is in [MAC_SETUP.md](MAC_SETUP.md) and [UBUNTU_SETUP.md](UBUNTU_SETUP.md).

| I want to… | Jump to |
|---|---|
| See it work right now | [1](#1-try-it-in-two-minutes) |
| Understand the dashboard | [2](#2-read-the-dashboard) |
| Know why a token was bought or skipped | [3](#3-see-why-a-token-was-bought-or-skipped) |
| Start collecting real data | [4](#4-paper-trade-on-the-real-market) |
| Train the prediction model | [5](#5-train-the-p2x-model) |
| Change a setting while it runs | [6](#6-change-settings-while-it-runs) |
| Test a change before trusting it | [7](#7-prove-a-change-before-you-use-it) |
| Graduation plays | [8](#8-graduation-plays) |
| Use the model as an entry filter | [9](#9-use-the-model-as-an-entry-filter) |
| Make a report I can share | [10](#10-make-a-shareable-report) |
| Copy a profitable wallet | [11](#11-copy-a-wallet) |
| Run it 24/7 and check from my Mac | [12](#12-run-247-on-the-mini-pc-and-check-from-the-mac) |
| Go live with real money | [13](#13-go-live) |
| Chat with Claude, look up any token | [14](#14-let-claude-operate-the-bot) |
| Fix something that looks wrong | [15](#15-when-something-looks-wrong) |
| Find out if a strategy really works | [16](#16-research-a-strategy-properly) |

---

## 1. Try it in two minutes
```bash
scripts/start.sh demo
```
The dashboard opens at **http://127.0.0.1:8787** with a simulated market and fake money. No keys are needed. Press **Ctrl + C** in the terminal to stop.

## 2. Read the dashboard
Seven views. Switch with the tabs or the keys **1–7**:

- **Live**: money, open positions, the launch radar and every event.
  - Click any token, position, closed trade or log line to open its **detail panel**.
  - The coloured chips under the numbers turn whole strategies on and off.
- **Desk**: what each bot is thinking, as a row of agent cards.
  - **Graduation scanner:** every coin near its 55–85% window, checked rule by rule (the same check that decides a buy), with what it's still waiting for.
  - **Exit manager:** how close each open position is to each of its exits.
  - **Risk officer** and **feed watchdog:** the loss limit, drawdown and data health.
  - **AI desk:** four Claude personas who vote on each entry. Asleep until you add an Anthropic API key, then **Wake the desk**. It bills your Anthropic account, roughly $0.04 per vote set, and adds a few seconds before each buy.
    - A *user* key (`sk-ant-usr-…`) also needs your **Anthropic workspace ID** (`wrkspc_…`, from console.anthropic.com → Settings → Workspaces). A workspace key (`sk-ant-api…`) doesn't.
    - **Test** next to the key shows whether Anthropic accepts it.
    - If three reviews in a row get no answers, the desk rests itself and says why, so trading isn't silently blocked.
  - The agents sit at two desks with speech bubbles. Click any of them for its card and controls.
  - **Wallet recorder:** pause it, pause it for 1, 4 or 12 hours, or set daily quiet hours. Every pause is logged so the wallet study knows about the gap.
  - **Thought stream:** every decision as it happens.
  - **𝕏 feed:** public posts from accounts and searches you pick, plus each coin the bot holds. It reads public posts through FxTwitter, so no X login or paid API is needed. Contract addresses in posts are clickable.
  - **Research:** progress of the running tests.
  - **The office:** the agents live in a little office. They sit and type at their desks, take coffee and water breaks, chat about what's going on, and read new notes on the corkboard. Click any of them for their card and controls.
  - **Teach the desk:** paste an X post or article link, a contract address, or a note, with an optional comment. It's saved to the desk's memory: Claude in Chat can read it, and the AI desk sees your notes on any coin it votes on.
- **Chat**: talk to Claude about the bot (see [14](#14-let-claude-operate-the-bot)).
- **Manual trading** (Live tab, position cards, and every coin's details):
  - Paste a contract address (or press **Trade** in a coin's details), pick an amount or type one, and press **Buy**. **🦍 APE** buys the preset size in one click.
  - On any position, the bot's or yours: **25%**, **50%**, **Initials** (sell just enough to get your cost back; the rest rides free) and **Exit**.
  - **Your own positions** only exit on what you set on their card (stop, take profit, trail), plus when the coin graduates, because the bot can only trade the bonding curve. Defaults are in `sniper.manual`.
  - Pausing the bot doesn't block your trades. The kill switch, the daily loss limit and a broken feed do.
  - In live mode every manual trade asks you to confirm first.
- **Portfolio**: watch-only wallets, yours or anyone's.
  - Paste an address to see its SOL, its coins with logos and dollar values, and its latest transactions.
  - Addresses only: nothing there can move funds. The list stays on this machine (`data/portfolio.json`).
  - Balances use your `SOLANA_RPC_URL` if you set one. The public endpoint is slow for big wallets.
- **Analytics**: what is working, in plain English, plus charts:
  - equity and drawdown;
  - a projection of the next 100 trades;
  - breakdowns by strategy, exit, hour and model score;
  - the gate audit.
- **Controls**: live settings, plus **API keys & connections**. See [6](#6-change-settings-while-it-runs).
  - Paste a key and press Save: it goes into `.env` (readable only by you), is never shown again, and is never given to Claude.
  - Some keys work immediately (Anthropic, RPC URL); the rest after `systemctl --user restart meme-sniper`.
- **Guide**: the same tour inside the app, plus keyboard shortcuts.

Badges in the header:

| Badge | Means |
|---|---|
| `PAPER` / `LIVE` | Fake or real money |
| `model AUC 0.71` | A trained model is loaded. AUC is its score on launches it never saw: 0.5 is a coin flip, above 0.65 is useful. |
| `pumpportal.fun · 2s` | Seconds since the last market event. It turns red (`stale`) if data stops. |
| Amber banner | **Defense mode**: after a losing run, size is halved and the entry bar is raised for a while. |
| Red banner | **Halted**: the drawdown kill switch fired or KILL was pressed. Nothing new is bought. |

## 3. See why a token was bought or skipped
Click the token anywhere. The panel shows:
- **Gate checklist**: every filter, its live value, the limit, and pass/fail. "Red flag" gates reject for good. "Must hold" gates have to be true at the moment of entry.
- **P(2x)**: the model's chance that the token doubles before falling 30% within 10 minutes, and the **expected value** after fees.
- **Why the model says so**: the inputs pushing that probability up (green) or down (red).
- **Top holders** (the dev is flagged, along with who funded each wallet) and the **tape** of recent trades.
- Links to pump.fun, DexScreener and Solscan, plus the token's own socials.

## 4. Paper trade on the real market
```bash
scripts/start.sh paper
```
This uses the real pump.fun feed with fake money. It needs no keys: launches come from PumpPortal's free stream and trades from pump.fun's on-chain logs.

It **records the market** to `data/feed-YYYY-MM-DD.jsonl`. The model, backtests and tuning all learn from these files, so let it run. Finished days are compressed to `.jsonl.gz` automatically, which is about 8–10× smaller.

## 5. Train the P(2x) model
After **2–3 days** of recording (more is better):
```bash
scripts/start.sh train      # writes a CANDIDATE model (data/model-candidate.json); the bot doesn't use it yet
scripts/start.sh promote    # deploys it, only if trained on your recordings with held-out AUC >= 0.6
```
`train` fits on your oldest launches, calibrates on the next slice, and **grades on the newest launches it never saw**. The slices are purged, so no training outcome is known before the graded launches start. It prints a verdict:

| Verdict | Do this |
|---|---|
| little skill (AUC < 0.6) | Don't promote. Record more and retrain. (`promote` refuses it anyway.) |
| some skill (0.6–0.7) | Promote it to see P(2x) on the dashboard. Leave `predict.display_only: true`. |
| useful skill (> 0.7) | Promote it, then test whether it helps as a filter first. See [9](#9-use-the-model-as-an-entry-filter). |

A running bot picks up a promoted model within a minute; there's no need to restart it. While `sniper.predict.display_only` is `true` (the default), **the model is shown but never changes a trade**: no entry gate, no sizing. A model trained on the simulated market can't be promoted at all. **Retrain weekly**: pump.fun behaviour drifts, and research finds models trained on one period do worse on later ones.

## 6. Change settings while it runs
Open the **Controls** tab. Toggles and numbers apply instantly. Press **Save to config** to keep them after a restart; this writes `config/params.yaml`. **All settings**, further down, has every other setting (search it, or tick "Only changed"), each with its default and a one-line explanation. Lists, endpoints and keys still live in the config file.

The dollar **hard cap** (`Max buy`) is enforced in code. Nothing can buy more, whatever it asks for.

## 7. Prove a change before you use it
Two tools. Both use every CPU core, and both use your recordings by default.

**Tune one setting** (walk-forward: tune on older days, test on newer ones):
```bash
scripts/start.sh sweep --grid exit.stop_loss_pct=20,30,40
```
It recommends a change only if it also wins on the data it never saw.

**Compare whole strategies** across many samples (each recorded day is one sample):
```bash
scripts/start.sh compare --variant "late: late.enabled=true" --variant "ladder: exit.profile=ladder"
```
Read the **beat base** column. "7/8" means the variant made more than your current settings on 7 of 8 days. A variant that is better on average but wins only half its days is luck, not edge.

## 8. Graduation plays
A second strategy. It buys tokens already 55–85% up their bonding curve that are still filling fast, and sells before migration (post-migration liquidity drops sharply).

It's **on by default in paper**, so it collects evidence. Before going live, check its row in **Analytics → By strategy**. Prove it on your own recordings:
```bash
scripts/start.sh compare --variant "no_late: late.enabled=false"
```
Turn it off any time with the **Graduation plays** chip, or `late.enabled: false` in the config.

## 9. Use the model as an entry filter
Only after `train` says *useful skill*. A model can't be graded on launches it was trained on, so backtests, sweeps and compares on those days automatically run **without** it. Train on older days and test on newer ones:
```bash
scripts/start.sh train --file data/feed-2026-10-0[1-4]*      # older days only
scripts/start.sh promote
scripts/start.sh sweep --file data/feed-2026-10-0[5-7]* --set predict.display_only=false \
    --grid predict.min_p=0,0.15,0.25,0.35                      # newer days the model never saw
```
If a value wins on the unseen data, set `predict.display_only: false` and that `predict.min_p` in `config/params.yaml`, then restart.

`predict.require_positive_ev: true` is a softer filter. It skips entries the model says lose money after fees.

## 10. Make a shareable report
```bash
scripts/start.sh report                   # your recorded trades (one mode: paper, live or demo)
scripts/start.sh report --mode paper      # needed when you've run more than one mode
scripts/start.sh report --session paper-20261003-053729    # one run only (session ids are in the CSV)
```
This writes `data/report.html`, one self-contained page that opens anywhere, and `data/report.csv`, every trade for a spreadsheet. The page says plainly where its numbers come from. Demo, paper and live trades are tagged and never mixed unless you ask for `--mode all`. Trades recorded before tagging existed show up as `--mode unknown`. To report a backtest instead:
```bash
scripts/start.sh report --backtest --file data/feed-*
```

## 11. Copy a wallet
```bash
scripts/start.sh leaders
```
This ranks wallets in your recordings by profit, and flags insider-like and bot-speed ones. Paste a candidate under `copy.leaders` in `config/params.yaml` with `mode: signal`. In signal mode it boosts a token's score instead of copying blindly. Watch its results in the **Copy trading** panel. A leader that keeps losing is paused automatically.

## 12. Run 24/7 on the mini PC and check from the Mac
On the mini PC:
```bash
bash scripts/setup_ubuntu.sh --service
```
On the Mac:
```bash
ssh -L 8787:127.0.0.1:8787 you@botbox.local
```
Then open http://127.0.0.1:8787 on the Mac. Details are in [UBUNTU_SETUP.md](UBUNTU_SETUP.md). The dashboard only answers on the mini PC itself, because its buttons can sell, so always use the tunnel.

## 13. Go live
Only after weeks of paper trading that you're happy with in **Analytics**. Follow [SETUP.md step 5](SETUP.md#5-going-live-real-money):
- a **dedicated** hot wallet holding only the bot's budget, never your main wallet;
- its key in a file that only the signing code reads;
- a paid RPC;
- `MEME_TRADER_CONFIRM_LIVE=yes`.

Start with the smallest sizes.

## 14. Let Claude operate the bot
**From the dashboard (easiest).** Open the **✦ Chat** tab (or press `c`) and type. Some things to try:
- "how are we doing?"
- "anything worth selling?"
- "add 10 paper SOL"
- a quick button such as **Desk check**
- a token's contract address on its own: you get its metrics card right away (price, market cap, liquidity, candles, buys vs sells, top holders, risk flags), and Claude's read under it.

Claude Code has to be installed and logged in on this machine (run `claude` once in a terminal). The chat uses your Claude plan, not an API key; the meter at the top shows how much of your 5-hour and weekly allowance is used. Pick a lighter model (Haiku, Sonnet) in the menu to make it go further. **New chat** starts over.

**More or less risk.** Use the **risk dial** on the Live tab (or Controls), or tell Claude "more risk" / "less risk" in Chat. Raising it always waits for your Approve, even with "Ask before actions" off. Each level shows exactly what changes: dollars per trade, open positions, and the daily loss limit.

**Paper balance.** Ask in Chat ("add 5 paper SOL"), or use **Controls → Paper balance**. A deposit counts as starting capital, not profit. It lasts until the bot restarts, unless you tick "keep", which saves the new starting balance to `config/params.yaml`.

**From a terminal.** Open Claude Code in the project folder:
```bash
cd ~/meme_traderv1 && claude
```
The first time, approve the `meme-trader` MCP server. Then either type `/desk-check` for a full review, or just ask.

Reading is automatic. Every action shows you the exact step and its reason, and waits for your OK: pause, setting change, buy, sell, watch, deposit, note. In the dashboard that's the Approve button. You can switch "Ask before actions" off there; the bot's limits still apply. The bot itself enforces the limits: Claude can make things safer but never riskier than your `config/params.yaml`. It can't go live or press KILL. Its actions show in the dashboard log as AGENT lines, and setting changes last until the next restart.

To stop agent buys entirely: `sniper.agent.can_buy: false`. To turn the tools off: `sniper.agent.enabled: false`. Restart after either change.

## 15. When something looks wrong
| You see | Likely cause → fix |
|---|---|
| Feed badge red / `stale` | No market data. Check the internet, then the `trade logs` row of `scripts/start.sh doctor`. If the public RPC is struggling, set `SOLANA_WS_URL` to another Solana websocket. |
| Amber **Defense mode** banner | A losing run. It lifts by itself after the set minutes. Turn it off in Controls if you disagree. |
| Red **Halted** banner | The drawdown kill switch fired or KILL was pressed. Restart the bot to reset. |
| "running · max positions" | All trading slots are full. Raise **Max open positions** or wait for exits. |
| No buys for a long time | Look at **Why we passed** and the radar's rejected tokens, then check the gate audit in Analytics. |
| `no model` badge | Normal until you run `scripts/start.sh train` and then `scripts/start.sh promote`. |
| Dashboard says disconnected | The bot stopped. Check the terminal, or `journalctl -u meme-sniper -n 50` on the mini PC. |

Nothing here is financial advice. Most pump.fun tokens go to zero.

## 16. Research a strategy properly
A strategy only counts once it passes on data it was never tuned on. The full process is in [RESEARCH.md](RESEARCH.md); in short:
```bash
scripts/start.sh research data                   # what's recorded and how clean it is
scripts/start.sh research eval graduation-v1     # development report: costs, uncertainty, baselines, ablations
scripts/start.sh research final graduation-v1    # the frozen verdict (shows nothing until 14 days of holdout)
```
To try a different idea, copy `research/policies/graduation-v1.yaml` to a new name, change it, evaluate it, and `research freeze` it before judging. Don't edit a frozen policy; the tool refuses, because a result after changing the rules means nothing.

