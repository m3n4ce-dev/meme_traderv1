# The console: how to do things

The dashboard at http://127.0.0.1:8787, task by task. The Chat tab's assistant reads this file (with the Guide
tab) through its `console_help` tool, and can do the screen actions marked **(chat can do it)** for you.

## The top bar

- **Badges:** PAPER or LIVE, the risk level, running/paused, the prediction model, and the feed (which server it's on and how far behind).
- **Tabs:** Live 1 · Pulse 2 · Desk 3 · Chat 4 · Portfolio 5 · Analytics 6 · Controls 7 · Guide 8 · Charts 9.
- **Buttons, right side:**
  - ⌘K: the command palette.
  - ✎ Arrange: move and hide panels.
  - ◐: theme.
  - ◎ SOL / $ USD: units.
  - ⏸ Pause: the bots stop opening new trades.
  - >_ : terminal.
  - ↻: restart the bot.
  - KILL: sell everything and halt.

## Switch the theme: light (white), dark, or automatic

- Press the ◐ button in the top bar, or the `t` key. Each press cycles automatic (follows your computer) → light → dark.
- The icon shows ☀ for light, ☾ for dark and ◐ for automatic. It's remembered in this browser.
- **(chat can do it):** "make it white", "dark mode".

## Show amounts in dollars or SOL

- Press ◎ SOL / $ USD in the top bar, or the `u` key. Every amount on the page switches.
- Orders are still sized in SOL.
- **(chat can do it).**

## Keyboard shortcuts (keys)

- `1`–`9` open the tabs: Live, Pulse, Desk, Chat, Portfolio, Analytics, Controls, Guide, Charts.
- `Ctrl`+`K`: command palette (jump to any tab, coin, setting or guide section; paste an address to act on it).
- `` ` `` (backtick): the terminal.
- `c`: Chat.
- `/`: filter the launch radar.
- `p`: pause or resume new entries.
- `a`: arrange this page.
- `n`: new post.
- `u`: SOL or $.
- `t`: theme.
- `m`: mute pop-up notifications.
- `?`: Guide.
- `Esc`: close whatever is open.
- KILL has no shortcut on purpose.

## Buy a coin by hand

- **On Live → Manual trade:**
  1. Paste the contract address. Its metrics, chart and red flags appear.
  2. Pick an amount (0.05 / 0.1 / 0.25 / 0.5 / 1 SOL or custom).
  3. Press **Buy**.
- **Other ways to buy:**
  - A Pulse row's ⚡ quick-buy button.
  - The Buy row in any coin's details.
  - Right-click any coin → ⚡ Quick buy.
  - In the terminal: `buy <coin> <sol>`.
- **Adding to a coin you hold:** Buy adds to the position, as do the ＋ buttons on its position card.
- **What applies to your trades:** they skip "paused" and the seat limits, but not the kill switch, the daily loss limit or a broken feed.
- **Tips:** a pop-up appears after your trades; turn them off with `tips off` in the terminal.

## Sell, take initials, or set your own exits

- **On a position card (Live → Open positions) or in the coin's details:**
  - Sell 25% / 50% / 100%.
  - **Initials:** sells just enough to get your cost back.
- **Your own stop, take-profit or trail on a position:** in its card, set them in its exit rules and save. Your trades only exit by your rules.
- **Other ways to sell:**
  - The Charts tab's Sell buttons.
  - In the terminal: `sell <coin|all> [pct]`.

## Market cap: where you got in

- **Where it shows:**
  - every position card (`MC $7.3K → $21K`: when you got in, and now);
  - the trade panel ("in at $X MC");
  - each Charts card;
  - Live → Closed trades (`MC in → out`).
- **The live charts' scale** is market cap in dollars, with "in at $X" on the entry line.
- **Analytics → By entry market cap:** results by the market cap trades got in at, for All, You or Bots.
- **Older trades** were filled in from the recorded market data.

## Hand a position to the bots, or go away

- **In Live → Manual trade:**
  - **🤖 Give to bots** hands over the coin you hold; **✋ Take back** undoes it.
  - On a coin you don't hold, **🤖 Buy & give to bots** buys the amount picked, and the bots take over once it fills.

- **🤖 Hand to the bots** (on a position card): the bots ride it for a runner. Half comes out at 2x, the rest trails 30% off its peak once up 30%, and the stop is 40% down.
- **✋ Take back:** your own exits apply again.
- **🚶 Away** (Live): the bots manage all your positions, and any limit orders you have open. Press **I'm back** to end it.
- **In the terminal:** `hand`, `take`, `away on|off`.

## Limit orders and market-cap alerts

On Live → Manual trade → Limit / alert:
1. Choose Limit buy, Limit sell or Alert.
2. Choose ≥ or ≤ and a market cap (50k, 1m…), and how long it lasts.
3. Press **Place**.

Orders & alerts lists them, with ✕ to cancel. In the terminal: `limit …`, `alert <coin> >= 1m`, `orders`, `cancel <id>`.

## Charts tab: live charts of every coin you hold, and pinned coins

- **The tab:** key `9`. Every open position, yours and the bots', gets a live chart, updated with every trade within about a quarter second.
- **On each chart:**
  - **P&L.**
  - **Time windows:** 1m / 5m / 15m / All.
  - **Sell buttons:** 25% / 50% / 100%.
  - **Markers:** ▲ is a buy and ▼ a sell. Hover a marker to see who it was. The colours are you (cyan), the bots (violet), the dev (red), KOLs by name (pink), smart wallets from the wallet study or copy leaders (amber), and whales of 2+ SOL (grey). The dashed line is your entry.
- **Pin any coin to watch it** without holding it:
  - Press 📈 Pin in its details, or right-click it anywhere → 📈 Pin to Charts. **(chat can do it)**
  - Pinned cards show the change since you pinned, market cap and curve, plus Buy buttons and Unpin.
  - Up to 8 pins, remembered in this browser.

## Charts tab: optional views (KOL tracker and more)

- **Turn them on** with the chips under the Charts header; each is remembered in this browser. ✕ hides a view.
- **👑 KOL tracker:**
  - Live trades by known wallets (kolscan.io's KOLs and the wallet study's wallets) on any coin the bot sees.
  - The coins they're in: who, buys/sells, net SOL, and market cap at their first buy → now. 📈 pins a coin.
  - A tape of their latest trades, with how long they held when they sell.
  - Load the KOL list with 👑 Refresh KOL list.
- **🔥 Copycat waves:** names launched 3+ times in the last hour, with the OG and the biggest one now.
- **🎓 Graduation watch:** the graduation scanner's coins (curve %, market cap, age, a sparkline, and what it's waiting on).
- **⚡ Market pulse:** launches, trades and graduations per minute.
- **💼 Equity:** your account over time.

## OG and copies: coins sharing a name or ticker

- **The badges:** most launches copy another coin's name or ticker. A coin marked **OG · n** was the first of n coins with that name in the last 6 hours; **copy k/n** came later.
- **Where they show:** Pulse rows, position cards and the Charts tab.
- **The coin's details:** lists the whole family, the OG first and then the biggest.
- **Being the OG isn't an edge by itself.** In the recordings, copies that reached the graduation setup did as well as or better than OGs.

## KOLs on the charts

- **Load the list:** on the Charts tab, 👑 Refresh KOL list fetches kolscan.io's list of about 570 known traders once. The charts then name them when they trade.
- **Copying them loses money.** They hold a median 34 s, and copying 1–2.5 s late lost about 13% a trade. So a tip appears if you buy within 2 minutes of a KOL buy.

## Pulse: find coins

- **The tab:** key `2`. Three columns:
  - 🌱 **New:** under 50% of the curve.
  - 🔥 **Final stretch:** 50–100%.
  - 🎓 **Graduated:** view-only.
- **Each row:** market cap, curve, buyers, buys/sells, 30-s inflow, top-10 share, dev buy, socials, sparkline, and flags (dev sold, bundle, top10, mayhem, OG/copy).
- **⚙ Filters per column:** saved.
- **Quick buy:** the amount at the top sets the ⚡ button.
- **Lists freeze under your mouse,** so a row doesn't move as you click it.
- **Right-click a row** for actions.

## A coin's details

- **Open it:** click any coin anywhere, or type `coin <coin>` in the terminal. **(chat can do it)**
- **What it shows:**
  - Buy and sell buttons, and 📈 Pin.
  - Links to pump.fun, DexScreener and Solscan.
  - Age, curve, market cap and score.
  - The same-name family.
  - The live chart with markers.
  - The model's P(2x).
  - The gate checklist (why the bot would or wouldn't buy).
  - Top holders and the live tape.

## The trading room (Desk tab)

- **The tab:** key `3`. The bots in their house:
  - **🏢 Trading floor:** desks, the AI table, the P&L board.
  - **🛋 Lounge:** where the AI personas rest, sofa by day and bunks at night.
  - **🔬 Lab:** the research boards.
- **Moving around:**
  - Switch rooms with the tabs above the room.
  - Click a bot to zoom into its screen; `Esc` goes back.
- **What they say** comes from the bot's real data. Everything they say is kept in the Room chat panel.
- **The room follows your clock** (dusk, night) and the day's P&L (sun, clouds, rain, storm).

## Characters: speech on or off, move them, change how they act

- **💬 Speech on / 🔇 Speech off** (above the room): turns the speech bubbles off or on. The Room chat panel still keeps what they say. **(chat can do it)**
- **Move a character:** drag it with the mouse to any free spot. It stays there and stops wandering; only real work, like carrying a coin to the AI table, moves it, and it comes back. To undo, use 👕 Characters → its Spot → **Send back**. **(chat can do it:** "send the skeptic back")
- **How each one acts:** 👕 Characters → pick the bot. These are saved as you click:
  - **Talks:** Silent, Quiet, Normal or Chatty.
  - **Wanders:** Stays put, Calm, Normal or Restless (how often it gets up for coffee or visits the lounge or lab).
  - **Walks:** Slow, Normal or Fast.

  Real hand-offs still happen, like the scanner carrying a coin to the AI table. **(chat can do it:** "make the quant quiet", "keep the scanner at its desk")
- **Looks:** 👕 Characters → hair, hat, glasses, clothes, skin and name → **Save changes**. Saved on the bot, so every browser sees the same team.
- **Furniture:** 🛠 Edit room. Drag desks, the AI table, the couch and plants, then press Done; Reset room restores the default.

## The corkboard: add, hide or delete notes

- **Open it:** on the trading floor, click the corkboard on the right wall. It zooms in.
- **Add a note:** type a note, an X or article link, or a contract address, plus a comment if you like, then **📌 Pin it** (or Enter). The desk reads it and replies.
- **🙈 Hide from board:** takes a note off the board; the desk still remembers it and reads it. **👁 Show on board** puts it back.
- **🗑 Delete:** removes it for good. Click it twice to confirm.
- **What the board shows:** the newest 6 notes that aren't hidden. Desk → Teach the desk shows all of them, with the personas' replies.

## The AI desk and its huddles

- **What it does:** four AI personas (veteran, narrative, skeptic, quant) vote on each entry the rules pick.
- **Wake it or let it rest:** Desk → AI desk model.
- **Choose its model:** Claude, GitHub Models, Hugging Face, OpenRouter or a local model, with Test buttons. It's billed per call; the panel shows the cost.
- **How the vote works:** a trade is approved when buy votes reach 45% of the desk's voting weight, so three of four is a buy. A buy under 50 conviction counts as half a vote. The skeptic can veto with a pass at 75+.
- **Huddles:** Desk → Desk huddles → 📣 Call a huddle. They also happen every 30 minutes while the desk is awake.
  - The bots discuss the account and keep a **Growth plan** (goal, strategy, experiments).
  - They propose up to 3 setting changes, each with an **Apply** button. Nothing changes until you press it.
- **Teach the desk:** Desk → Teach the desk. Paste a link, an address or a note; the personas reply under it.

## Strategies on and off, and the risk dial

- **Strategy buttons** (Live, under the top cards): Sniper, Copy, Graduation plays, Callouts. A click turns one on or off **and saves it**, so it stays that way after a restart.
- **Risk dial** (Live or Controls): Cautious, Normal, Bold, Aggressive, Max. It scales trade size, open positions and the daily loss limit together.
- **Pause:** ⏸ Pause or `p` stops new entries; open positions are still managed.

## Settings

- **Controls → Live settings:** the main ones, applied instantly. **Save** writes them to config/params.yaml so they survive a restart.
- **Controls → All settings:** every other setting.
- **In the terminal:** `settings [filter]`, `set <key> <value>`, `save`.
- **Defense mode:** after a losing run by the bots, they trade at half size with a higher entry bar for a while. Your own trades don't count toward it and aren't affected.

## Paper balance: add pretend SOL or start over

Controls → Paper balance:
- **Add paper SOL:** counts as starting money, not profit.
- **Start over…:** cash back to the starting balance; the trade files keep everything.

In the terminal: `deposit <sol>`, `reset`.

## Kill switch, halts and restart

- **KILL:** sells everything and halts.
- **What else halts the bot:** the daily loss limit or the drawdown kill switch.
- **Lift a halt:** press **Resume trading…** in the red banner. Only you can; a restart doesn't clear it.
- **↻ Restart** (next to KILL): saves everything, reloads the code and settings, and carries on.

## The terminal

- **Open it:** >_ in the top bar, or the `` ` `` key. Tab completes; ↑ and ↓ go through history.
- **Commands:** `help`, `status`, `pos`, `buy <coin> <sol>`, `sell <coin|all> [pct]`, `hand <coin|all>`, `take <coin|all>`, `away on|off`, `alert <coin> >=|<= <mcap>`, `limit buy|sell <coin> >=|<= <mcap>`, `orders`, `cancel <id>`, `pause`, `resume`, `unhalt`, `kill`, `deposit <sol>`, `reset`, `risk [1-5|name]`, `desk on|off|test|model`, `set <key> <value>`, `save`, `settings [filter]`, `log [n]`, `coin <coin>`, `ask <question>`, `go <tab>`, `restart`, `tips on|off`, `history`, `clear`.

## Chat with Claude

- **Open it:** key `4` or `c`.
- **What it can do:**
  - Answer questions about the bot and coins, using its tools.
  - Take actions (trade, settings, pause). You press Approve for each one.
  - Change what the screen shows right away, without approval: theme, tab, units, opening a coin, pinning charts, speech, and how the characters act.
- **Model:** pick it in the chat's model menu.
- **How-to questions:** "how do I …" is answered from this manual.

## Analytics

- **The tab:** key `6`.
- **Results:** highlights, equity and drawdown, the next 100 trades (projection), P&L by strategy, exit, score and hour.
- **Is it working:**
  - **Edge check:** is each strategy's edge real, with a 90% range.
  - **Exit lab:** other exits on the same entries, including the desk's passes.
  - **Gate audit:** what the rejected coins did.
- **Wallet study:** which early wallets were early again, with clusters.
- **Prediction model.**
- **📣 Calls & track record and the Ledger:** hash-chained calls, Publish proof.

## Calls, posts and connections

- **Make a call:** 📣 Call in Manual trade, or right-click a coin → Call it. Calls are recorded on a tamper-evident ledger (Analytics).
- **Post:** `n` or ✎ New post. Every post is your click, and paper results are labelled PAPER.
- **Connections:** Controls → Connections. X (pay per post) and Telegram (free).

## API keys and connections

- **Where:** Controls → API keys. Paste a key and Save, and use **Test** to check it.
- **What's there:** Anthropic (for the AI desk), Helius or QuickNode (faster data; on the free Helius plan the full trade stream would use the month's credits in about 2½ days), Telegram, X, Hugging Face.
- **Where they're kept:** in .env on this machine, never shown in full.

## Portfolio: watch a wallet

Portfolio (key `5`) → paste a wallet address and a label → **Add wallet**. It shows that wallet's holdings: addresses only, nothing here can move funds.

## Arrange the page and notifications

- **✎ Arrange** (`a`): drag panels by their bar, resize from the right edge, hide panels and bring them back from the bottom bar. Saved on the bot.
- **▾** on a panel's title collapses it.
- **Notifications:** `m` mutes pop-ups **(chat can do it)**. Desktop notifications can be turned on from the command palette (fills, alerts, closes while the tab is in the background).

## Live trading (real money)

- **Not switched from the dashboard.** It needs a deliberate start from a terminal, with `MEME_TRADER_CONFIRM_LIVE=yes` and a dedicated wallet (see docs/HOWTO.md).
- **Paper mode** uses real market data and pretend money, and is the default.
