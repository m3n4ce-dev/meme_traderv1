# Build Log — meme_traderv1

A running record of decisions, research, parameters and status. Newest entries at the top.

---

## 2026-10-05 — Entry #45: Feed outages: stop flapping, and a budgeted Helius backup

**Owner's request:** "We gotta figure out the latency… missed a few paper trades because the mainnet was down."

**What happened:**
- From 00:29 Solana's public RPC (`api.mainnet-beta`) hung up the trade-log websocket every 20 s–5 min. Each time, the watchdog moved to the next endpoint, PublicNode, which it already knew was ~10 s behind. It measured that and moved back.
- That was dozens of switches an hour, and each one paused entries while the new connection was checked.
- Later in the night the public RPC itself ran 4–10 s behind, with stalls.

**Fixes:**
1. **A hang-up on the fastest known endpoint is a reconnect to it** (1 s), not a move to a slower one, unless it hangs up 4 times in 3 minutes.
   - With your own endpoint configured (`SOLANA_WS_URL`), a hang-up on a fallback goes back to yours.
   - Quality problems (slow, missing trades) are handled as before: the bot never moves to an endpoint known to be slower.
2. **A metered backup:**
   - **When:** `feed.backup_ws_url`, else `SOLANA_WS_BACKUP_URL`, else the owner's `HELIUS_API_KEY` websocket. It's used only while the free endpoints hang up repeatedly or fall behind with no faster free one.
   - **Back to free:** it tries the free endpoints again every 5 minutes. It never starts on the backup, and is never picked just for being faster.
   - **Allowance:** up to `feed.backup_mb_per_day` (1200 MB) of uncompressed stream a day. Helius bills websockets at 2 credits per 0.1 MB, so that's ≈24k credits a day, ≈720k of the free plan's 1M a month. 0 turns it off.
3. **Measured, not guessed:**
   - The feed now reports reconnects and switches in the last hour, the stream's uncompressed MB per hour, and the backup's use today. The feed badge shows the real delay ("1.3s behind" instead of seconds since the last event), with ⚠ when it keeps dropping.
   - The stream measured 405–465 MB an hour over whole minutes (about 800 MB/h in a busy 10-s burst), roughly 10 GB a day.
   - A 10-second test of the owner's Helius websocket: 419 pump.fun trades, median 1.22 s behind the chain (block times are whole seconds, so ~0.5 s of that is rounding).

**The paid option:** for less delay all day, the Helius Developer plan ($49/month, 10M credits) covers the whole stream (~6M credits a month at this rate) as the primary endpoint (`SOLANA_WS_URL`). Lower still needs gRPC/LaserStream, which is a bigger plan.

---

## 2026-10-05 — Entry #44: KOL tracker and optional views on the Charts tab

**Owner's request:** "An optional KOL known wallet tracker on the charts page, plus other optional charts and visualizations."

**The tape:** the engine notes every trade by a known wallet (kolscan.io KOLs, and the wallet study's qualified wallets) on any coin it sees, in `kol_tape`. It records the hold time on sells, from their first buy.

**Endpoints:**
- `/api/kols`: the last hour's tape, and the coins they're in (who, buys/sells, net SOL, first-buy market cap → now, OG badge).
- `/api/hot`: copycat waves, meaning names launched 3+ times in an hour, with the OG and the biggest one now.

**The Charts tab's optional views** (chips, remembered per browser; refreshed every 3 s while the tab is open): 👑 KOL tracker, 🔥 Copycat waves, 🎓 Graduation watch (the scanner's coins), ⚡ Market pulse, 💼 Equity.

**Caught by the new test:** the first version reused the name `known` in the trade handler, which already meant "a copy-trade leader". Every KOL trade would have been routed to the leader handler and raised a KeyError. It was renamed before deploying.

---

## 2026-10-05 — Entry #43: Market cap first; a lived-in house; give to the bots from Manual trade

**Owner's request:** "Show what market cap we got into each coin at… market cap is a main metric to track."

**Market cap in and out of every trade:**
- **Recorded on every trade:** each closed trade records `entry_mcap_sol/usd` and `exit_mcap_sol/usd`, and positions carry their entry and current market cap.
- **Where it shows:**
  - position cards (`MC $7.3K → $21K`);
  - the trade panel ("in at $X MC, now $Y");
  - the Charts tab;
  - a `MC in → out` column in Closed trades;
  - Analytics → **By entry market cap** (All / You / Bots; buckets from <$5K to $160K+).
- **The live charts' scale** is now market cap in dollars, not a tiny per-token price. The entry line reads "in at $X", and markers say the market cap they happened at.
- **Older trades:** `python -m meme_trader.sniper mcap-backfill` rebuilt the market caps of trades from before this was logged, from the recorded feed (each trade line carries the curve's reserves). 294 of 328 were filled; the rest had no feed coverage. They're stored in `data/mcap_backfill.json`, applied at startup, and their dollars use today's SOL price.

**The house is lived in** ("make some tasks in other parts of the buildings so it's not stale"):
- **Multi-step tasks:**
  - **Lab:** run a backtest (bench → rack → edge board), read the wallet study, service the rack, compare exits.
  - **Lounge:** ping-pong for two (with a ball), the arcade, a fridge run then the TV, a nap, watching the pulse.
- **More visits:** trips are 2–3x more frequent, and a quiet house gets a visitor now and then. A character set to "Stays put" never goes.
- **The room tabs** say who's doing what.

**Give to bots from Manual trade:** "🤖 Give to bots" (or "✋ Take back") on a coin you hold. On one you don't, "🤖 Buy & give to bots" buys the amount picked and the bots take it over once it fills; `hand_after` remembers it until then.

**The chat and how-to questions:** a question that reads like "how do I / where is / turn off / hide…" now gets the matching manual sections attached automatically. It once answered "I don't have a tool" about a button.

---

## 2026-10-05 — Entry #42: The chat knows the console; characters, corkboard and pinned charts

**Owner's requests:**
- "Make sure I can ask the main chat bot how to do anything and everything on the console." (It couldn't answer "change the theme to white".)
- "Turn their dialog on and off, move the characters around and change how they act."
- "Delete or hide the notes on the bulletin board, or add one there."

**The console for the chat:**
- **The manual:** `docs/CONSOLE.md`, a task-by-task manual.
- **`console_help(question)`:** searches the manual and the Guide tab's own sections (`meme_trader/ui/manual.py`, keyword match with synonyms such as white → light theme, dialog → speech, bulletin → corkboard).
- **`ui_action`:** changes only what the owner's open dashboards show: theme, tab, $/SOL, opening a coin, pinning a chart, the room's speech, pop-ups, and a character's behaviour or spot.
  - The bot broadcasts it to every open page (`type: "ui"`), and the page applies it and says what it did.
  - It can't trade or touch a setting, so the chat may use both tools without an approval prompt.
- **Chat instructions:** call `console_help` before any "how do I…" answer and reply with the exact clicks or keys; do screen changes directly.
- **Tests** check that the manual names every tab, key and terminal command, so it can't silently fall behind the page.

**Characters** (Desk → 👕 Characters, formerly Wardrobe):
- **Speech:** 💬 Speech on/off for the bubbles (per browser; the Room chat panel keeps the record).
- **Placing:** drag any character to a free spot. It stays there and stops wandering; only real hand-offs move it.
- **Behaviour per character:** Talks (silent, quiet, normal, chatty), Wanders (stays put, calm, normal, restless) and Walks (slow, normal, fast). Saved on the bot with their looks.

**Corkboard:** clicking it opens a view where notes can be pinned there, or hidden from the board (the desk still reads them; `off_board` on the memory item) or deleted (click twice).

**Charts tab:** any coin can be pinned to watch it live, from its details or by right-clicking it, with Buy buttons and the change since pinning.

**OG vs copies, measured** on three recorded days with history carried across them:
- **Setup:** the graduation setup, bought 2.5 s late, trailing exit.
- **Results:** OGs −5.6% a trade [−10.9, +0.9] (306); copies +3.4% [−1.7, +9.1] (456); crowded names (4th copy on) +6.1%.
- **Conclusion:** being first isn't an edge, so the bot doesn't prefer OGs, and the buy tip says so. A crowded name may mean a hot theme; that's a lead for a future frozen test, not a tuning.

**An incident:** a JavaScript syntax error in the page (a `? :` left without its `:`) was served from the live checkout for about 10 minutes, because the page is read from disk on each load.
- **Fix:** page work now happens in a separate git worktree, with a browser syntax check before anything reaches the live checkout.

---

## 2026-10-05 — Entry #41: The OG among copies

**Owner's request:** "If there is a bunch of coins, would be good to mark the OG."

**Why it matters:** copying is the norm. Of 8,029 launches in 5.1 hours on the night of Oct 4, 71% shared a name or ticker with another launch. "PUMP" had 169 and "SI" 152 (one of the owner's +120% trades).

**How families are built:**
- The engine indexes every launch by ticker and by name. Both are lowercased with only letters and digits kept, in one namespace, since a copy often keeps the name and changes the ticker or the other way round.
- Coins sharing either key within 6 hours form a family.
- The first one launched is the **OG**. It is only known if it launched after the bot started watching; if not, the dashboard says an older original may exist.

**Where it shows:**
- **Badges** on Pulse rows, position cards and the Charts tab: `OG · n` or `copy k/n`.
- **The coin popup** lists the family: the OG first, then the biggest by market cap, with ages and graduations.
- **A buy tip** when you buy a copy: which coin is the OG and which is biggest now. It's a tip only, never a block.
- **The AI desk** gets a `name_family` block (launch order, count, which is biggest) with each review.

**Not done:** the bot doesn't prefer OGs or avoid copies. That would be a strategy change, and it needs a measurement first.

---

## 2026-10-05 — Entry #40: Health check: why the bot stopped trading, and three other bugs

**Owner's request:** "Analyze and debug the project."

**Checked and fine:**
- **Services:** all three are up.
- **Disk:** 713 GB free. data/ is 0.9 GB; the wallet recorder keeps 75 MB.
- **The bot's books reconcile:** cash 6.1516 SOL = equity, with no positions open.
- **Main loop:** 591 probes over 60 s had a median of 1.4 ms and a worst case of 10 ms.
- **Dashboard:** the full browser pass (every tab, three widths, both themes, every trading flow) had no script errors.
- **Static analysis:** pyflakes found only unused imports and variables.
- **Helius:** the wallet recorder streams through PublicNode (43.7 GB on Oct 4), not Helius. The bot's funding lookups now go through the owner's Helius address at 1 credit a call, ~80 an hour, so about 6k credits a day against the free 1M a month.
- **Block-zero bundles** (raised in a trader interview): already counted. Any wallet buying within 2 s of create is in `bundle_pct`.

**Found and fixed:**

1. **The AI desk blocked nearly every entry from 2026-10-04 14:23** (the desk was woken then):
   - **The cause:** `aggregate` computed the buy share as weight × conviction / 100. Models report conviction around 55–65, so 3 of 4 personas voting buy came to 44% against the 45% quorum, and the trade was passed. Logged example: "cyber: veteran:buy62 narrative:buy58 skeptic:pass62 quant:buy58 | buy share 44% vs quorum 45%".
   - **The fix:** the share now counts votes (a buy under 50 conviction is half a vote), and conviction still sets the size. The skeptic's veto (pass at ≥ 75) is unchanged.
   - **Effect, recounted from the journal (14:00 to 23:13):** the reviews would have gone from 2 approved of 33 to 18 of 33.
   - **What the market offered meanwhile:** 70–120 coins an hour reached the graduation window, while graduation trades fell from 14–23 an hour (11:00–13:00) to 0 after 16:00.
   - **The exit lab's desk-pass shadows:** 9 so far, mean +68%. Most are one coin (Gizmo, passed six times; the rules would have made +234% and +241% on two of those passes). Thin, but in the same direction.
2. **Strategy buttons didn't save:**
   - **The problem:** the Live page's Sniper/Copy/Graduation/Callouts buttons only applied the change ("press Save to keep it"). The owner had turned the sniper off; the deploy restart at 23:13 reloaded `entry.enabled: true` from params.yaml, and the sniper traded from 23:14.
   - **The fix:** the buttons now save at once, like the risk dial. The sniper was set back to off (saved), matching the owner's last screen.
3. **The demo wrote into the real data and spent real AI credit:**
   - **The problem:** `run --synthetic` journaled its fake trades into data/trades-*.jsonl and journal-*.jsonl (labelled paper-synthetic, so the stats kept them apart, but mixed into the files). Its AI desk voted on fake coins with the real API key.
   - **The fix:** a synthetic run now writes no trade or journal files, and its desk stays asleep unless `--desk` is passed.
4. **Coin logos could stall the bot:**
   - **The problem:** a 0.23 MB PNG under the 4 MB download cap held 73 M pixels. Decoding it took 1.4 s and ~300 MB on the event loop, freezing trading, exits and the dashboard. It happened twice on Oct 4.
   - **The fix:** an image over 16 M pixels is refused from its header before decoding, JPEGs decode at reduced size, and thumbnailing runs in a worker thread.

---

## 2026-10-05 — Entry #39: Live charts, the owner's picks vs a volume rule, and what KOLs are worth

**Owner's request:** "I bought these and took profit and defense mode turned on still. Look at what I did and also figure out an algo for volume and transaction counts", then "show previous entries and important wallet entries on a chart, or one chart page for all held coins; it still seems super slow", then "check out KOL scans".

### Changes

- **Defense mode counted the owner's trades:**
  - **The bug:** it looked at the last 10 closed trades of any kind except callouts. The owner's manual losing run put the *bots* on half size, with a higher entry bar, while the owner was +27%.
  - **The fix:** it now counts only the bots' trades (`source` not manual or callout). The banner says the owner's trades aren't affected. Regression test added.
- **"Super slow" was measured, not guessed:**
  - The page redraws in ~19 ms with no long tasks.
  - The bot's endpoints answer in ~1 ms.
  - The feed runs ~1.3 s behind the chain.
  - **The slow part was the charts:** the trade panel polled every 2.5 s and the coin popup every 2 s, so a price could be ~4 s old.
  - **Now:**
    - The page sends `chart_sub` with the coins it shows: the trade panel's coin, the open popup's, and every held coin on the new Charts tab.
    - The server streams each of their trades within ~0.25 s (`ticks`; only new trades after the first full history).
    - The charts are drawn by time (`tickChart`).
- **Markers on the chart** (`Engine.chart_data`):
  - **Your and the bots' entries and exits:** this run's ledger, including earlier round trips on the same coin and partial sells.
  - **Dev trades.**
  - **KOLs, named.**
  - **Smart wallets:** the wallet study's qualified wallets, copy leaders and smart-money signals.
  - **Whales:** single trades of 2 SOL or more.
  - Hovering a marker shows who.
- **📈 Charts tab** (key 9): every open position's live chart, with P&L, a 1m/5m/15m/All window, and Sell 25/50/100% buttons.
- **Helius was checked as a faster feed and not used.** Its free plan has 1M credits a month, and standard websockets cost 2 credits per 0.1 MB. The bot's full pump.fun trade stream is ~20 GB a day, so the month's credits would last ~2½ days.

### Studies

**The owner's buys vs the market** (recorded feed replayed through the bot's tracker; 19 evening buys matched):
- **Where the buys sit:** in the busiest ~10% of coins at that moment (median percentile ~90 for buyers in 20 s, net inflow, 60-s volume and the 60-s price move). The winners were in the top ~5%.
- **Where the curve was:** mid-curve (median ~55%).

**A mechanical volume / transaction-count rule:**
- **Method:**
  - Thresholds on buyers in 20 s, net inflow in 20 s, volume in 60 s, buy/sell ratio, price change, near-high and a curve band, plus the usual rug caps (28,102 rules).
  - Each rule fires once per coin. It's filled 2.5 s later at the next real trade, with ~7% round-trip cost.
  - Searched on Oct 4 (24 h), then tested on Oct 3 and Oct 5 (days the search never saw).
- **Results:**
  - **Fixed targets** (scalp +30/−15, the owner's style +40/−25, +60/−30, +100/−35) all lost on the unseen days, typically −6% to −14% a trade.
  - **Trailing exits** came closest. The best rule (net inflow over 20 s ≥ 8 SOL, 60-s volume ≥ 40 SOL, buy/sell ≥ 2, within 20% of the high, curve 30–95%), with a trail armed at +50% and set 25% off the high:
    - Oct 4 (the search day): +9.9% a trade;
    - Oct 3: +1.8% [−8.1, +13.4];
    - Oct 5: −2.3% [−16.3, +9.6].
- **Conclusion:** not an edge. Whatever made the owner's picks work isn't captured by these counts alone. Candidates are timing within the move, and exits sold into spikes by hand.

**KOLScan** (kolscan.io: ~570 named KOL wallets and a leaderboard):
- **Data:** in our recordings, 241 of those wallets made 4,449 first buys on pump.fun bonding-curve coins.
- **They are fast sellers:** median hold 34 s; 66% sold within a minute and 95% within 5 minutes.
- **Copying them loses, mirroring their exits:**
  - at their own price: +1.6% a trade;
  - 1 s late: −12.6% [−14.1, −11.1];
  - 2.5 s late: −13.9%;
  - 5 s late: −15.0%;
  - holding 5 minutes: −17.6%.
- **Picking the "good" ones doesn't help:**
  - the monthly top 50 copied for −12.7%;
  - wallets that made over +5% a copy on Oct 3 lost −9.3% on Oct 4;
  - the best of Oct 3–4 lost −11.9% on Oct 5.
- **So KOLs are shown, not followed:**
  - The live charts name them.
  - Buying within 2 minutes of a KOL buy shows a tip with these numbers (tips only, never a block).
  - The list is fetched only on the owner's click or `python -m meme_trader.sniper kols`, into the git-ignored `data/kols.json`.

---

## 2026-10-05 — Entry #38: The bots talk to each other, as a team with a plan

- **Desk huddles** (owner: "do the bots talk to each other? It doesn't seem like it… they should be close to AGI… they should think how can we grow this account… there has to be a strategy or they are useless"):
  - **Before:** the room's exchanges were scripted line pairs with numbers filled in.
  - **Now:** every `desk.huddle_minutes` (30) while the desk is awake, or on 📣 Call a huddle, one model call writes a real meeting from the bot's state (`desk_brief`).
    - Each bot speaks from its role: the operator chairs; scanner, exits, risk, feed and recorder report; the four personas argue.
    - They answer each other by name and challenge with numbers.
    - The room plays it out at the AI table as speech bubbles.
- **A growth plan carried between meetings** (`data/desk_plan.json`): a goal with a date, a strategy, and experiments with pass tests and status (proposed, running, passed, failed, stopped). Each meeting reviews it against the results.
- **Changes need the owner's Apply:** up to 3 setting changes per meeting, kept only if the key is a real setting. Apply sets and saves it; the agent can't apply them.
- **Advice for the owner** about their own trading (never limits).
- **New in the briefing:**
  - **Exit what-ifs** on the graduation bot's past trades:
    - as traded +4.91 SOL;
    - selling all at +30% −2.45, at +50% −1.03, at +100% +2.79;
    - banking a third at +30% +2.46.

    So "stacking wins" would cost this bot its runners.
  - **The owner's manual pattern:** sells at +50% or better made money; losers below −40% (none ever above +24%) cost more.
  - **Plain names for settings and strategy switches.** The first meeting read `entry.enabled: false` as "all entries off" and suggested turning on the losing sniper. Another blamed the bot's stops for the owner's manual losses. The briefing and instructions now make both clear, and the next meetings got them right.
- **First real meeting** (Sonnet, ~2¢):
  - **Plan:** hold the graduation config unchanged for a week toward 100+ clean trades.
  - **Proposed change:** a daily loss limit of 1.5 SOL instead of 3, as the account is 6.3 SOL.
  - **Advice for the owner:** a −25% stop on manual positions, with the numbers.
  - **Not adopted:** banking a third at +30%, because it trails as-traded by ~2.45 SOL.
- **Desk side column:** the scanner card no longer sticks over the panels below it.

---

## 2026-10-05 — Entry #37: The bots get a house; the desk back on Claude

- **A house** (owner: "give the bots another room or a whole house if they want"): the trading floor plus a **lounge** and a **research lab**, with a switcher in the room showing how many bots are in each.
  - **Getting around:** every room has its door in the same spot, so a bot walks out of one room and in through the next room's door, using pathfinding per room.
  - **The lounge:**
    - a TV with the live Market Pulse, a sofa facing it, four bunk beds (blankets in the personas' colours), an arcade machine, a ping-pong table, bean bags, and a fridge and kitchen;
    - the AI personas rest there while the desk sleeps: in bed at night, on the sofa by day, swapping at bedtime and morning;
    - bots take breaks there.
  - **The lab:**
    - whiteboards with the live wallet study and edge check, the recorder's server rack with blinking lights, two workbenches, a research table and a bookshelf;
    - the recorder visits often and the others now and then, and they read the boards' real numbers aloud.
  - **Chat:** talk from another room goes to the room log, tagged with the room, not over the room you're looking at.
- **The desk was awake again** on Hugging Face Qwen with no credit left, so every vote failed and entries were blocked. It's now on **Claude Sonnet 5.5** (tested: 2.7 s, about 1.2¢ a review).
  - The bots said "Can't reach Claude" even when the model wasn't Claude; it's now "Can't reach the AI model".
  - Errors now say what failed: a provider out of credits (402), a rejected token, rate limits, or a local server that isn't running.

---

## 2026-10-05 — Entry #36: Why the bot stopped trading, the desk's goal, the wallet study in Analytics, Calls moved

- **No graduation entries since 16:03 on Oct 4** (owner: "they should be making more trades"). Before that it averaged 3–9 an hour, 14–23 around midday.
  - **The cause was the AI desk:** since 14:00 it approved 2 of 56 reviews, and none of the 33 after 16:00.
    - The skeptic vetoed most of them.
    - Many reviews were "desk unavailable", which fails closed: Claude 400/401 errors earlier, then HTTP 402 once the free Hugging Face credit ran out ("You have depleted your monthly included credits").
  - **Fix:** the desk was put to rest (one click to wake it), and the rules trade on their own again. The 162 paper trades behind the +6.5% were all made by the rules without the desk.
- **The desk's goal** is now in its instructions:
  - grow the account; both buying a loser and passing on a runner cost money;
  - vote on whether the setup beats the ~6% round trip;
  - pass only for concrete reasons in the data.
  - The skeptic's brief says "vetoing everything costs the desk as much as buying everything".
- **The desk's passes are scored:** every graduation coin it turns down is followed in the exit lab with the bot's own exits ("desk-pass"). Analytics shows what its passes would have averaged against the coins it bought.
- **Exit lab:** two take-profit-early variants, "bank half at +30%" and "all out at +50%" (Cupsy's "take your profit, stop hunting home runs" from a Bez Trades video), so the data can say whether banking gains early beats the current exits.
- **Wallet study in Analytics** (`python -m meme_trader.wallets view`; `/api/wallets`, rebuilt in its own process at most every 30 min):
  - It shows period A's progress, the coins that ran, and the qualifying wallets.
  - **Groups:** wallets that bought the same coins within 5 s, three or more times, are grouped as one trader.
  - **First result:** the 7 listed wallets are about 3 traders. One group of four and one pair keep buying together: one operator, or bots copying one leader. The frozen rules count them separately.
- **Calls:** no longer a whole tab. It's a section at the bottom of Analytics (owner: "we don't need the whole page"); number keys now run 1–8.
- **Cost display:** "0.92¢ a review" read as 92 cents; it's now "$0.0092 (under 1¢)".
  - Hugging Face's note says what a free account gets: a few cents of credit a month, then nothing until it resets.
  - Saving a Hugging Face model on a free account warns that a desk that can't vote blocks entries.

---

## 2026-10-05 — Entry #35: SOL or dollars, and does the wallet recorder slow the bot?

- **◎ SOL / $ USD switch** (owner: "toggle between SOL format and current $ format"): a header button, or the `u` key, remembered per browser.
  - **What converts** (at the live SOL price): equity, P&L, position values, costs and proceeds, the loss limit, the kill-switch level, market caps, money flows, per-token prices on the charts and their axes, the room's talk and the P&L board, and Analytics.
  - **What stays in SOL:** order sizes, presets and deposits, because orders are sized in SOL. In dollar mode the trade panel shows what the amount is worth.
  - **Not converted:** the Portfolio tab shows both, and posts to X keep SOL.
  - **How:** the two amount helpers (`sol`, `signed`) follow the switch, so every place using them converts.
- **The wallet recorder and latency** (the owner paused it hoping to cut lag):
  - **Load:** it uses ~0.6% of the machine's CPU and streams ~1.2 MB/s, but from PublicNode, a different server from the bot's feed (api.mainnet-beta). They share no rate limit. The bot's own feed is ~0.17 MB/s.
  - **When the lag happens:** the feed's "behind the chain" switches follow pump.fun's busy hours: 46–104 an hour from 11:00 to 16:00 CDT, 1–11 an hour otherwise.
  - **The pause test:** during the recorder's 14:57–15:57 pause there were 46, against 47 the hour before and 49 the hour after. No effect.
  - **Pause works:** it closes the stream. (I first misread a measurement taken just after the owner resumed it.)
  - **What would cut the lag:** a better trade-feed endpoint than the free public RPC in busy hours, not pausing the study.

---

## 2026-10-05 — Entry #34: GitHub refresh: README, keys, a one-row header, a correction

- **README rewritten for what the bot does now:**
  - the edge check's results table: graduation plays +6.5% per SOL over 162 paper trades, "promising, not proven", with why;
  - the AI desk's model choices;
  - manual trading (chart, tips, limit orders, hand-over riding, graduated coins on paper);
  - the terminal, Resume after a halt, Restart, and the full key list.
  - New screenshots: most from the real-market paper bot, the terminal and AI-model panel from the demo. No video yet.
- **`.env.example`:**
  - It said the real market needs a PumpPortal key; it hasn't since the free on-chain feed.
  - It now lists the AI-provider keys (GitHub Models, Hugging Face, OpenRouter), the Anthropic workspace, and the X posting keys.
- **`scripts/start.sh edge` and `start.sh calls`** pass through to the CLI.
- **One-row header** on screens up to 2240 px: the tab icons, the subtitle and the clock hide, and Pause and Restart shorten to icons. Measured at 1440 to 2560 px: one row. At 1280 it wraps; on phones it stacks; nothing scrolls sideways.
- **Correction:**
  - **The mistake:** entries #32 and #33 said live orders reach the bonding curve only. They don't: the live executor sends `pool=auto`, which routes to PumpSwap after graduation.
  - **The reason that holds:** live still refuses graduated coins, because their price only updates every 10 s from DexScreener, too stale for real money. The messages, the guide and the log now say so.
- **Terminal:** it no longer prints the trade panel's background coin lookups.
- **Three bugs the new screenshots showed:**
  - **Wrong exit description:** the exit manager described a manual position by the bots' time limit ("closest exit is time held, 12.7 of 30 min"). Your positions have no time exit. It now shows your own stop, take profit and trail, or the ride rules once handed over (regression test).
  - **"Paused: paused."** The recorder said this; it now says "Paused from the dashboard: the wallet study misses trades until I resume."
  - **Chart note:** the position card's "no trades since you bought" note sat on top of the chart; it's now under it.
- **Qwen on Hugging Face** (owner connected Hugging Face and asked for "a nice big new Qwen agentic model"):
  - **The catalogue:** the router serves 33 Qwen models.
  - **One real vote each:**
    - **Qwen3.8-2.4T-A95B:** about 4.5 s and $0.0034 a vote (about 1.4¢ a review vs ~2.3¢ on Opus), with sensible reasons. Chosen.
    - **Qwen3.5-397B-A17B and Qwen3.8-27B:** they "think" first. Even with 4,000 tokens of room they ran 34 s and 8 s without answering, so they're too slow for 12 s votes.
  - **Client fixes:**
    - The client crashed on these replies: it expected an answer field, and thinking models send `reasoning` instead (KeyError: 'content'). It now retries once with more room when a model ran out thinking, and otherwise says plainly "pick an Instruct model" (tested with a fake server).
    - Routing suffixes like `:fastest` work.
  - **Real prices:** the cost estimate for other providers was $0, so a paid model read "free". Picking a model now reads its price from the provider's list: Hugging Face per provider (the highest live price, so it never reads low), OpenRouter per token. It's saved with the model.
- **CLAUDE.md:** the operator's facts are updated: the edge-check numbers, manual trading is the owner's, hand-over riding, and lifting a halt is the owner's call only.

---

## 2026-10-05 — Entry #33: Handed-over positions ride for a runner, a chart in the trade panel, Market Pulse fixed

- **Hand-over rides instead of dumping** (owner: "whenever I hand over to the bots they pretty much sell instantly. I'm trying to catch 2xs"):
  - **Why it sold at once:** handed positions ran the bots' own scalp exits, which are tuned for coins they bought seconds earlier. On a coin you'd held for minutes, those time-based exits fired at once: a 45 s stall, momentum decay, "dev sold" and a short stop.
  - **New "ride" rules** (`evaluate_ride_exit`, config `manual.handover`):
    - 50% out at 2x the entry.
    - The rest trails 30% off its peak since the hand-over, once it has run 30% (or after the partial).
    - A stop 40% below the lower of the entry and the hand-over price.
    - No time, stall, momentum, dev-sold or cluster exits.
  - **Coverage:** away mode and older saved hand-overs use the same rules.
  - **Graduated coins** keep riding on paper (the pool price). Live sells them at graduation: its orders would reach PumpSwap (`pool=auto`), but the price after graduation only updates every 10 s.
  - **Manual positions on a graduated coin** are no longer auto-sold on paper; the sale at graduation applies to live only, now that paper prices them.
- **A chart in the trade panel** (owner: "whenever I click trade ... it should pop up the chart"): picking a coin opens its live price chart.
  - It refreshes every 2.5 s and marks your entry.
  - Coins the bot doesn't track show the pool's candles from the lookup.
- **Market Pulse:**
  - **What was wrong:** after a restart it had 2 minutes of data, drawn as two half-width bars, and the header averaged a partial minute.
  - **Now:** a fixed 60-minute axis with one slot per minute, and partial minutes are faint, left out of the trades line and the averages. The line breaks where the bot was off.
  - **Kept across restarts:** the last hour is saved with the state.
- **Tests:** 286 pass, with a new one for the ride rules: no exit on chop, half at 2x, a trail off the peak, a stop below the hand-over price, and graduated paper vs live. The hand-over test now expects no instant sell.
  - Browser: Trade → chart shown, buy → hand over → still held, Pulse rendering.

---

## 2026-10-05 — Entry #32: Trade tips, the edge check, smarter personas, graduated coins, tightening

- **Tips on your own trades, never limits** (owner: "manual trading to be manual... no restrictions"):
  - **When a tip shows:** after a manual buy or sell goes through, a small box may show:
    - the buy is 10%+ of the account;
    - the coin has red flags;
    - the coin is quiet;
    - you've had 3 losses in a row;
    - you sold a quick in-and-out (about 6% round-trip cost, measured on the owner's flips).
  - **Controls:** "Don't show this one again", or turn tips off (Ctrl+K or `tips off` in the terminal).
  - **The quiet-coin confirmation is now a tip:** nothing asks "are you sure" any more, except the REAL MONEY confirmation in live mode.
  - **The trade panel shows the bot's take** on the coin: its status and score.
- **Edge check** (Analytics panel, `python -m meme_trader.sniper edge`, `sniper/edge.py`):
  - **What it shows per strategy:** the last 14 days of trades (paper and live never mixed); return per SOL staked; the average trade with a 90% bootstrap range; the result without the best 3 trades; and days up.
  - **Verdict and caveats in plain English.** Caveats flag a short span, changing settings, profit resting on a few runners, and paper fills.
  - **"Copy for a group":** the text leaves your own trades out and is labelled paper.
  - **First run on the live paper data:**
    - **Graduation plays:** +6.5% per SOL over 162 trades (90% range of the average trade +0.3% to +14.3%), "promising". But its best three trades made 92% of the profit, it covers only 1.5 days, and the settings changed 8 times.
    - **Sniper:** −13.8%, "losing, not by bad luck".
    - The frozen holdout test (~Oct 17) is still the real verdict.
- **Personas answer questions** (owner: they replied "not for us, drop a CA" to "what's holding us back?"):
  - **A briefing for each reply:** the edge check, top rejection reasons, what blocks entries, the settings, recent trades, the exit lab, the feed and your recent notes.
  - **New instructions:** questions get answers from those numbers (stance "info", no tag), and notes like "that one" are resolved through recent notes.
  - **Tested on the demo with Claude:** all four personas named the same losing copy leader with its trades, the positions limit blocking entries, and the sample sizes, each from its own angle.
- **Graduated coins on paper** (owner hit "it has graduated off the bonding curve" from Pulse's Graduated column):
  - **Paper buys:** they fill at DexScreener's PumpSwap pool price, refreshed every 10 s while held, with curve fees modelled (a bit worse than the pool's ~0.3%). A pasted address the bot never saw trading is priced the same way.
  - **Live** refuses with a clear reason. Its orders would route to PumpSwap (`pool=auto`), but a price that updates every 10 s is too stale for real money. (Corrected 2026-10-05: an earlier message wrongly said live orders reach the curve only.)
- **Endpoint keys:**
  - **Test buttons** for the Solana RPC URL (one getSlot), the websocket URL (one slot update, then unsubscribe) and the Helius key. A result never echoes the URL, since it can hold a key.
  - **A Helius key fills in an empty RPC URL.**
  - **The help text** gives the exact Helius and QuickNode formats, and warns that the ~20 GB/day trade stream can use up a metered free plan in days.
- **Fixes:**
  - **Replies cut off:** note replies ran off the right edge of their column (a grid track sized to the reply box). They now wrap at every width, including phones.
  - **P&L board:** the "vs start" and "today" text overlapped the chart; each now has its own line.
  - **Coin post drafts:** "Post" on a coin made a bare `$73fL…pump / CA:` draft. It now has the symbol and name, MC, curve, buyers or holders, top 10, dev and red flags, filled in by a lookup for coins the bot isn't tracking.
- **Tests:** 285 pass. The full browser sweep, the terminal test, the note layout at 4 widths and the post draft are all clean, with no page errors.

---

## 2026-10-05 — Entry #31: Resume after the kill switch, and a terminal

- **The halt that a restart couldn't clear:**
  - **What happened:** 16 manual paper trades in 13 minutes took the account from 5 to 2.82 SOL (−43.6%), past the 40% drawdown kill switch.
    - 1 win in 16. Two 1 SOL buys that went to zero ($SLJK −0.997, BOOCATE −0.904) were 87% of the loss.
    - The quick in-and-outs each lost about 6–7%: fees plus the move against you on the way in and out.
  - **Why the banner advice failed:** it said "Restart the bot to reset". That was true before the paper account carried over restarts (#29), so the halt now survived the owner's restart.
  - **Fix:** a **Resume trading…** button in the red banner (`unhalt` in the terminal), the owner's call only (not the agent's).
    - If the account is still past the limit, the kill switch counts from the equity at that moment (`book.kill_base`, saved with the account), so it doesn't trip again at once and still protects what's left.
    - A paper top-up moves that line up with the cash.
    - The drawdown card shows the new line.
  - **Paper banner shortcuts:** Add paper SOL and Start over.
- **>_ Terminal** (owner: "a terminal popup to do commands"): the `>_` button or the backtick key.
  - **Commands:** `status`, `pos`, `buy <coin> <sol>`, `sell <coin|all> [pct]`, `hand`/`take`, `away`, `alert` and `limit` by market cap, `orders`/`cancel`, `pause`/`resume`, `unhalt`, `kill`, `deposit`, `reset`, `risk`, `desk on|off|test|model`, `set`/`save`/`settings`, `log`, `coin`, `ask` (Claude's answer prints in the terminal), `go <tab>`, `restart`.
  - **Coins** are found by symbol among what the bot can see, or by contract address.
  - **Typing:** risky commands ask for `y`. History is kept with the up arrow; Tab completes.
  - **Bot commands only:** it sends the same actions as the buttons, so the same checks apply. It is deliberately not a system shell: the page shows coin names strangers chose, and a shell in the browser would make any slip there a way into the machine.
- **Tests:**
  - 277 pass, including lifting the kill switch: the agent is refused, the new line is saved and reloaded, a top-up moves it, it still trips past it, and the dashboard action works.
  - Browser: every terminal command at 1440 px and 420 px; the banner button. No page errors and no sideways scroll.

---

## 2026-10-05 — Entry #30: The couch, no APE, weather, room editor, exit lab, ↻ Restart, cheaper desk models

- **Couch:** it sat on the front edge of the room facing out. It now stands against the back wall under the corkboard, facing into the room, and the napping personas face out from it.
- **APE removed** ("kinda corny"): the button, the coin panel's APE, the server action and `manual.ape_sol`. Buy covers it, and on a coin you hold it adds to the position.
- **Weather follows the day** (idea from Agent Virtual Office: the room reflects the team's state):
  - The window shows sun on a green day, clouds below zero, rain past −2% of the account, and a storm with lightning past −6% or in defense mode.
  - The scanner remarks on each change.
- **🛠 Edit room** (idea from Pixel Agents' layout editor):
  - Drag the desks, the AI table, the couch and the plants. Pieces snap to half tiles, can't overlap each other or the coffee counter and cooler, and must stay in the room.
  - Seats follow their furniture, and the rug follows the table. Saved on the bot (`room` in data/ui.json); Reset room undoes it.
- **Exit lab** (`sniper/exitlab.py`; idea from a public bot's notes: judge exits on the same paper entries):
  - **How it works:** every bot entry starts shadow copies that run the bot's own exit code with other settings (graduation plays: stop 10% / 25%, stall 90 s, hold to 98% of the curve, a 2x take profit, a 20% trail once up 30%; sniper: stop 20% / 45%, no stall exit, a 2x take profit, the trail). Fills are instant, fees count on both sides, and shadows run up to 30 minutes.
  - **Output:** Analytics → Exit lab compares each rule with "as now" on the same entries. History is in `data/exit_lab.jsonl`.
  - **Measurement only:** it never trades. A better exit would be a new strategy version with its own test.
- **↻ Restart** next to KILL: saves the state, then re-runs the same command in place (same PID, so systemd doesn't notice). It loads fresh code and settings, and positions and orders resume. Tested: the demo bot was back in about 9 s.
- **Cheaper AI desk models** (owner: "local models or something cheaper than 4 cents per vote… or run from GitHub or Hugging Face"):
  - **What it really cost:** the meter showed about 2¢ a review (½¢ a vote) on Claude Opus 5.5 ($4/$20 per million tokens). The "4 cents" on the cards was a stale estimate; the cards now quote the live estimate.
  - **Providers** (Desk → AI desk model, saved to the config): Claude Opus 5.5, Sonnet 5.5 ($2/$10) or Haiku 4.5 (est. $1/$5), or any server with the standard chat-completions API:
    - GitHub Models (free with a token, rate-limited)
    - Hugging Face's router
    - OpenRouter (`:free` models)
    - a local server such as Ollama, LM Studio or llama.cpp
  - **The panel** shows the cost per review (measured once there are votes), lists the models a local server has installed, and has a Test button.
  - **Claude requests adapt** to what the model supports (effort, structured outputs), checked through the Models API. Other providers use JSON mode, or the request asks for JSON in words and the first JSON object in the reply is used.
  - **Note replies** use the same model.
  - **Measured on this machine** (12 cores, Ollama `llama3.2`): one vote in 5.1 s, free, but weaker (it called a 4% bundle "high"). Four at once on the CPU risk the 12 s vote limit, so local models suit note replies more than trade votes.
- **Full test pass:**
  - 276 tests.
  - Every tab at 1440 px dark and light, and 420 px.
  - Flows: buy, add, hand over, away, a limit alert, a call, the composer, Pulse, the palette, the desk model panel and its Test, the wardrobe, a note, the exit lab, the guide and a restart.
  - Bugs it found and fixed: the Test button's missing import; the palette focusing its box late (fast typing fell through to shortcuts); the guide lacking the new features.

---

## 2026-10-04 — Entry #29: A full health check after deploying #23/#24, and Habbo-style pixel people

- **Deploy note:** #24 was stacked on #23 and merged into #23's branch (not main) seconds after #23 merged. #25 carries it to main; until then the bot runs from `habbo-room`.
- **Full check, about 17:30 local:**
  - **Machine:** load 1.2 on a mini PC; 21 GB of 27 GB RAM free; 713 GB disk free; journals 52 MB.
  - **Services:** meme-sniper, meme-wallets and edge-scout all active with no crashes. Memory: 98, 171 and 119 MB.
  - **Feed:** mainnet-beta, 1.5 s behind the chain, 0% missing, not degraded.
  - **AI desk:** awake after the restart. `desk.enabled` was written to the config first, because it had been woken before the fix that saves that.
  - **Paper account:** started fresh one last time (nothing had been saved before #23); it now carries over restarts.
  - **Dashboard:** every endpoint answers in under 13 ms. The real page loads every tab with no errors and uses 10 MB of browser memory.
  - **Wallet recorder:** streaming 2,700 pools at full coverage, about 40 GB of bandwidth today, no errors.
  - **Research tests:** graduation-v1 is on holdout day 1.1 of 14; wallets-v1 is in period A, day 0.6 of 14.
- **Fixed from the check:** every restart began on PublicNode, which is about 10 s behind, so entries paused until the watchdog moved on.
  - The feed now remembers each endpoint's measured lag across restarts in `data/feed_endpoints.json` (hostnames only, never a full URL that could carry a key), and starts on the fastest one measured in the last 6 hours.
  - The 30-minute "go back to the first endpoint" retry now applies only to endpoints you configured (`SOLANA_WS_URL`), not the free fallbacks.
- **Pixel people (owner: "I want the bots like these guys", with Habbo screenshots):** the round robots are replaced by original Habbo-style pixel characters. No Habbo assets are used: every character is drawn in code.
  - **Drawing:** 32×64 sprites built from layered parts (hair, hat, glasses, top, bottom, shoes, skin), each with a 1-pixel darker outline and a shaded side, cached as PNG frames.
  - **Poses:** standing, a 4-frame walk, sitting, typing (2 frames), sleeping; front and back views, flipped for direction. Bots show their backs when walking up the room or looking at the window or board.
  - **Each bot has its own look:**
    - Claude: a black bob and big round glasses.
    - Scanner: spiky hair, a headset and a hoodie.
    - Risk: a hard hat and a safety vest.
    - Narrative: pink hair with a bow.
    - Skeptic: shades and a suit.
    - Quant: an afro, glasses and a striped sweater.
  - **Chat bubbles and note threads** show each speaker's pixel head.
  - **Wardrobe → "Change your looks":** a bot row, categories (hair 9 styles and 14 colours, 8 hats, glasses, 7 tops, 4 bottoms, shoes, 6 skin tones), preview tiles of the selected bot, colour swatches, a turnable preview, Random, Reset and the name.
  - Saved on the bot as `{name, look}`; looks saved before this still load.

- **Owner's follow-ups the same evening:**
  - **Chat bubbles:** they cut text off, drifted off the edge of the room, and idle lines repeated.
    - Bubbles now wrap (up to 3 lines), stack by their measured height, stay inside the room and point at the speaker.
    - A **Room chat** panel on the Desk tab keeps everything said.
    - Idle chatter comes from big pools: places, plus a voice for each bot. A line is never used twice: used lines are remembered in this browser, and when a pool runs dry, lines are made from live numbers. No identical line from anyone within 5 minutes.
  - **Add to an open position:** buying a coin you hold adds to that position. You get one position with a token-weighted average entry and the added cost in its P&L. Works on bot and manual positions, through instant, delayed-paper and late-confirmed live fills. Position cards have ＋ buttons, and Buy says "＋ Add".
  - **Hand positions to the bots** ("in case I need to leave"):
    - **🤖 Hand to the bots** on each of your positions switches it to the bots' exit rules: graduation exits past half the curve, sniper exits below. **Take back** returns it.
    - **🚶 Away** hands over everything you hold, plus anything your limit orders open, until **I'm back**.
    - Claude can do it from Chat (`hand_over_positions`, with your Approve).
    - Fixed along the way: your own positions could be force-sold at the bots' max-hold time while their price was unknown.
  - **Empty charts:** a position in a coin nobody has traded since you bought it now shows a line from entry to the last price, with a note. The detail panel explains the missing chart. Buying a coin with no trades for 3+ minutes asks first.
  - Pulse no longer lists "graduations" under a $20K market cap (instant migrations of coins that never filled their curve). The trade panel starts with a default size, so Buy works in one click.

---

## 2026-10-04 — Entry #28: Pulse, limit orders, the call ledger, X and Telegram posting, notes the desk replies to, a real guide

- **Owner's ask:**
  - "Whatever will be most awesome and profitable"; arrange the layout; connect X and post from the terminal; a better guide; each AI reads every note and replies; more interactive UI.
  - Mid-build: prove an edge to groups, and eventually split into several repos (plan: [EDGE_PROOF.md](EDGE_PROOF.md)).
- **⚡ Pulse tab:** three live columns, Axiom-style, from the bot's own feed: New (under 50% of the curve), Final stretch (50–100%), Graduated.
  - **Each row:** market cap, curve bar, buyers, buys/sells, 30 s net inflow, top-10, dev buy, socials, a sparkline, red-flag pills, and ⚡ quick buy.
  - **Per-column filters:** age, market cap, buyers, inflow, top-10, dev, bundle, socials, dev sold, Mayhem, sort. They're saved in the UI store.
  - Lists freeze while the mouse is over them.
  - Engine: `pulse_view()`, plus a list of recent graduations. Endpoint: `GET /api/pulse`.
- **Limit orders and alerts (Manual trade):**
  - Buy when the market cap dips to a level or breaks out above one; sell part of a position at a target; or just alert. Quick buttons set the dip, breakout, 2x-alert and take-profit levels.
  - They fire on the live curve price through the same manual buy/sell path, so the same caps and checks apply. They expire (1 h to 7 days), survive restarts (saved with the paper/live state), and a position's sell orders are cancelled when it closes.
  - In live mode, placing one asks for confirmation.
  - Engine: `place_order`, `cancel_order`, `_orders_tick`; coins with orders stay priced.
- **📣 The call ledger and Calls tab** (`sniper/calls.py`, `data/calls.jsonl`):
  - **What's recorded:** every call (yours, plus the bot's own entries) with the market cap at the time, hash-chained so nothing can be edited, deleted or reordered unnoticed. A file lock lets the bot and the command line both append safely.
  - **Scoring:** from market data (the live curve, then DexScreener after graduation). The stats lead with fixed-hold returns at 5 m, 1 h, 6 h and 24 h after ~5% costs; the sampled 24 h peak comes second.
  - **Proof:** Publish posts the newest hash to X and/or Telegram and records the anchor in the chain. Export downloads the ledger.
  - **Command line:** `python -m meme_trader.sniper calls verify|stats|list [--file]`.
- **Posting to X and Telegram** (`ui/social.py`, `ui/cards.py`, `python -m meme_trader.social`):
  - **X API (pay-per-use since Feb 2026, no free tier):** ~$0.015 a post, $0.20 with a link (a bare `pump.fun/…` counts).
  - **Two ways to connect X:**
    - OAuth 1.0a keys for your own developer account (signing tested against X's published example).
    - OAuth 2.0 PKCE "Connect X", with tokens in `data/x_auth.json` (mode 600) refreshed automatically.
  - **Images:** v2 chunked media upload; if the image upload fails, the post goes out as text and says so.
  - **Telegram:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID`.
  - **Composer:** live 280-character count with X's weighting, cost, link warning, daily count, and a share-card preview.
  - **Share cards** (1200×675 PNG) for a trade, a call or the record. Paper trades say PAPER; call cards always show the 1 h and 24 h results next to the peak.
  - **Limits:** 25 X posts a day. Posts happen only on your click or command; the AI never posts.
  - **Where:** Controls → Connections walks through setup, and the X keys are in API keys.
- **The desk replies to every note:**
  - When you save a note, link or contract address, all four personas read it (plus live metrics if the bot tracks the coin) and reply in a thread under it, with a stance.
  - Reply back; `@skeptic …` asks one persona.
  - In the room, each persona walks to the corkboard and says its reply.
  - Settings: `desk.note_replies` and `desk.note_effort`.
  - Text from the web is treated as data in the prompt.
- **Arrange v2:**
  - Every page becomes a 12-column board. Drag panels anywhere, drag the right edge to resize (snaps to columns), pick a width (⅓ ½ ⅔ Full) or a height (Auto/S/M/L), hide panels and restore them from the arrange bar.
  - "Reset this page" and "Reset all".
  - **▾ on every panel** collapses it at any time; **?** opens that panel's help.
  - All of it is saved on the bot.
- **Guide v2:**
  - Searchable sections: start, live, trading, Pulse, the room, the AI desk (with the quorum maths), notes, calls and proof, posting, risk, customising, research, keyboard, a glossary and an FAQ.
  - A 10-step spotlight tour across the tabs, offered on the first visit.
  - Contextual help from each panel's **?**.
- **Interaction:**
  - **Ctrl+K command palette:** tabs, actions, risk levels, coins, every setting and guide section. Paste a contract address to trade it, call it, alert on it, save it or post about it.
  - **Right-click any coin:** Trade, Quick buy, Call, Alert, Save to memory, Post, Copy, pump.fun, DexScreener.
  - **Opt-in desktop notifications** for fills, alerts and closes while the tab is in the background.
  - **Shortcuts:** `n` new post; tabs are now 1–9.
- **Tests:** 12 new: ledger tampering, two writers, horizons; orders and alerts; restarts; Pulse; note replies and follow-ups; OAuth signature, costs and limits; cards; dashboard actions. 268 in total.
- **Not verified against the live services:** no X or Telegram credentials exist yet, so posting was tested with X's published signing example and mocked requests. Note replies use the same API call shape as the desk's votes, which work in production.

---

## 2026-10-04 — Entry #27: The trading room, click to zoom, night shift, arrange any page

- **Owner's ask:** the effects felt corny; make the Desk tab a Habbo-Hotel-style trading room; click a bot to see what it's looking at; a P&L board on the wall; night at night; full metrics when a contract address is pasted; arrange each page; change the bots easily. Plus research on the best builds like this: [BUILDS_RESEARCH.md](BUILDS_RESEARCH.md).
- **The room (Desk tab):** an isometric room (2:1 tiles, SVG) replaces the side-view office.
  - **Furniture:** six desks with monitors and name plates, the AI meeting table, a couch, a coffee counter, a water cooler and plants. On the walls: a window onto a skyline, a clock with real hands, a **P&L board** (equity, vs start, today, equity line, trades and win rate, open positions and the risk dial, the last closes) and a **corkboard** with the newest memory notes.
  - **The bots** walk tile by tile round the furniture (a path finder; no walking through desks), sit and type when working, and take coffee, water, window, board and plant breaks. Two will meet in the aisle and talk about real numbers (the feed's lag, the risk dial, a coin that's filling, today's P&L).
  - **Habbo-style chat:** each line appears above the speaker and older lines rise and fade.
  - **Hand-offs are real:** when the AI desk reviews a coin, the scanner carries its folder to the AI table, asks the desk, and each persona says its vote and its main reason, then the scanner reads out the verdict. When the bot buys, the scanner walks the folder to the exit manager. When a trade closes, the exit manager reports it and a small "+0.012" rises over it. A new memory note gets pinned to the corkboard by a bot.
- **Click to zoom:** click any bot, the P&L board or the corkboard and the camera zooms in, then shows that bot's screen: the scanner's checklists, the exit gauges, the risk dial (with its buttons), the feed's data and pulse, the operator's last actions, the recorder's controls, a persona's votes, the P&L detail with the last ten trades, or every memory note. Esc or "Back to the room" zooms out.
- **Night shift:** day 07:30–17:30, dusk until 19:30 and from 05:30, night otherwise, by the browser's clock. At night the room darkens, the desk lamps and monitors glow, the skyline lights up under a moon and stars, and the bots have night-shift lines.
- **Effects removed:** no more stamps, confetti, red shards or screen shake. Toasts and the position cards' flash stay.
- **Wardrobe (👕 in the room):** rename any bot, pick its colour, and pick its hat or accessory (new: top hat, crown, beanie, none). Saved on the bot (`data/ui.json`), so every browser sees the same team.
- **Arrange (✎ in the header, or `a`):** drag any panel to a new place on its page, or hide it. Saved on the bot too, per page. **Reset to default** puts everything back.
- **Manual trade metrics:** pasting a contract address shows price, market cap, liquidity, volume, buys vs sells, price change, top-10 holding, holders, curve progress, age, dev holding and risk flags, with links to pump.fun, DexScreener and Solscan. It refreshes every ~20 s while the coin is in the box.
- **Server:** `GET /api/ui` and the `ui_set` action (only the `layout` and `bots` keys, 64 KB cap, written atomically). Test: `test_page_layouts_and_bot_looks_are_saved_on_the_bot`.

### Found checking the live bot (2026-10-04, 15:00–16:35 local)

- **The AI desk forgot it was on.** The bot restarted at 16:00 and came back with the desk asleep: waking it was runtime-only, while `params.yaml` still said `desk.enabled: false`. In the 3½ minutes before it was woken again, the bot bought ARC and UNK at risk level Max, unreviewed; both devs dumped (−0.40 and −0.70 SOL).
  - **Fix:** waking or resting the desk from the dashboard is saved to `params.yaml`, like the risk dial. When the bot rests a failing desk itself (3 failed reviews), that lasts only until the next restart.
- **The paper account reset on every restart.** Only live mode saved its book, so each restart opened a fresh 5 SOL paper account. The dashboard showed "Today −1.09 SOL" and a 22% drawdown on a day whose trades were +3.17 SOL, and a restart also cleared the daily loss limit.
  - **Fix:** the real-feed paper bot now saves and restores its book like live (`data/sniper_state_paper.json`): cash, today's P&L, the peak, deposits, the equity chart, the closed-trade list and open positions. The synthetic demo still starts fresh.
  - **Start over:** Controls → Paper balance → **Start over…** (refused while positions are open). The trade files keep every trade either way.
  - The first restart after this deploy still starts fresh (nothing was saved before).
- **The feed flapped between endpoints.** With no `SOLANA_WS_URL`, PublicNode (≈10 s behind) is first in the list and mainnet-beta second. When mainnet-beta's lag briefly topped 5 s, the watchdog moved to PublicNode, found it slower and moved back: 40 switches in 35 minutes, each pausing entries while the new connection was measured.
  - **Fix:** the watchdog remembers each endpoint's measured lag for 30 minutes. It leaves a slow endpoint only for one that isn't known to be slower; otherwise it stays (entries stay paused until the lag recovers). The 30-minute retry of the first endpoint skips it while it's known to be slower.
- **A desk approval could vanish.** POLLM was approved at 15:52:56, but the buy sized to zero (the liquidity cap) and nothing said so. The graduation scanner then paid for a second review, which passed. Now an approval that can't be sized logs "desk approved … but skipped: the curve is too thin to size a buy".
- **What the desk passed (23 reviews, 15:24–16:25; a small sample, not for tuning):**
  - **Unanimous passes:** 6 of the 8 fell 60–75% within 3 minutes.
  - **Coins with 3 of 4 buy votes**, still passed because the quorum counts conviction ("buy share 44% vs 45%"), split: cyber +89%, fartfun +30% and Plumb +18% at 3 minutes; FREESIM, CHAI, BYTE and MINIPAD fell.
  - The gate audit (Analytics) now follows every coin the desk passes on a graduation play, grouped as "AI desk passed (graduation): N of 4 said buy", so the quorum can be judged on more data.

---

## 2026-10-04 — Entry #26: Manual trading, the office, the desk's memory, trade effects

- **Manual trading (Live → Manual trade, every position card, every coin's detail drawer):**
  - **Buying:** presets, a custom amount, and a one-click **🦍 APE**.
  - **Selling:** **25% / 50%**, **Initials** (sell just enough to get the initial cost back, net of fees; refused when that would mean the whole bag) and **Exit**, on any position, the bot's or yours.
  - **Settings:** `sniper.manual` (`max_sol`, presets, `ape_sol`, default stop / take profit / trail).
  - **Rules for your trades:** the same order path as the bot (paper delays and fees; the live executor in live mode, after a confirm). "Paused" and "max positions" don't apply, because those are the bot's limits. The kill switch, a degraded feed, the daily loss limit and cash do.
  - **Your positions** exit only on the rules you set per position (stop, take profit, trail), plus a sale when the coin graduates: the bot prices and sells on the bonding curve only.
  - **A coin with no live price yet** is bought at its first trade, within `queue_s`.
- **The office (Desk tab):** a side-view office replaces the desk row.
  - **The room:** a window with a skyline (day or night by local time) and a live SOL / P&L ticker, a clock with real hands, a HODL poster, a corkboard, a coffee machine with steam, a water cooler, plants, desks with monitors, the AI meeting table, and a couch.
  - **The bots** walk with swinging legs, sit and type at their desks, and take coffee, water, window and plant breaks. They chat in pairs about real data, and one walks over to read each new memory note on the corkboard. They rush back to their desk on an alert or a trade.
  - **The AI personas** nap on the couch while the desk is off, and sit at the meeting table when awake.
  - **Bubbles take turns** by neighbourhood (urgent news quiets the neighbours).
- **Trade effects** (removed in #27): a "BOUGHT", "INITIALS OUT" or "+0.123 SOL" stamp, with coins and confetti for buys and wins and red shards with a screen shake for losses. The bot that made the move reacts in the office.
- **The desk's memory (Desk → Teach the desk; `sniper/memory.py`, `data/memory.json`):** paste an X post or article link, a contract address, or a note, with an optional comment.
  - **Links:** X posts and articles come through FxTwitter, other pages through the same public-only, size-capped fetcher as token metadata.
  - **Contract addresses** get a metrics snapshot, and the bot starts watching the coin.
  - **Who reads it:**
    - Claude in the Chat tab reads memory with the new `get_memory` tool.
    - The AI desk sees your notes on a coin it's voting on (`owner_notes`, marked as data, never instructions).
    - The corkboard shows the newest six.

---

## 2026-10-04 — Entry #25: The AI desk's key problem, and the agents at their desks

- **Why the AI desk failed:** every persona got HTTP 400 "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header".
  - The key is a *user* key (`sk-ant-usr-…`). Unlike a workspace key (`sk-ant-api…`), every request must name a workspace.
  - Reproduced with a plain request, so it wasn't the desk's beta options.
- **Fixed:**
  - **Workspace ID:** `ANTHROPIC_WORKSPACE_ID` (Controls → API keys, validated as `wrkspc_…`) adds the header to every Anthropic client: desk, review agent, doctor.
  - **User-key warning:** the keys panel warns when a user key has no workspace ID.
  - **Test button:** a 5-token request to the smallest model shows Anthropic's verdict in plain words.
  - **Errors:** the full error is kept (400 characters, not 160).
- **Safety:** when no persona answers, the desk passes the trade. That silently blocked every entry while the key was broken. After 3 such reviews in a row, the desk now rests itself and says why, and entries continue on the rules alone. Waking it always builds a fresh client.
- **The floor (Desk tab):**
  - **Characters:** each agent is a small character at a wooden desk with a speech bubble showing what it's saying now.
  - **AI desk:** the four personas sit at a second, glass desk. Their bubbles show their last vote ("BUY 72: 14 fresh buyers in 30 s"); asleep, they doze with "z"s.
  - **Moods** drive the animation: working (typing, live monitor), thinking (eyes scanning, frown), acting (a hop, big smile), alert (red "!"), asleep (head down). Bubbles pop when their text changes.
  - **Details:** click anyone for their card, with its controls, in the side panel.
  - Routine blocks (max positions) no longer look like alarms.

---

## 2026-10-04 — Entry #24: Non-SOL coins skipped; social links survive ipfs.io's rate limit

- **Non-SOL coins:**
  - `feeds.trade_quote_mint` reads a TradeEvent's quote asset: it walks past `ix_name` and `shareholders` (variable length) to `quote_mint`.
  - The trade feed now skips coins quoted in anything but SOL (USDC, $PUMP and others seen live) and counts them (`SolanaTradeFeed.non_sol_skipped`, shown on the Desk's feed-watchdog card).
  - Before this they came through with 0 SOL and a price of 0.
  - Tested on captured live events. Older, shorter events still read as SOL.
- **Social links:** `_enrich` tries each launch's metadata through other IPFS gateways when ipfs.io refuses (`feeds.ipfs_urls`, now shared with the logo fetcher).
- Recordings change only by leaving out the non-SOL coins' trades, which no strategy could trade. Replays of older recordings and graduation-v1's signature are unchanged.

---

## 2026-10-04 — Entry #23: Open-source survey; USDC-quoted coins are invisible to the bot

- Surveyed GitHub trading bots read-only ([OPEN_SOURCE_NOTES.md](OPEN_SOURCE_NOTES.md)). Most pump.fun sniper and copy-trading repos are sales pages or wallet-drainer bait.
- Worth learning from:
  - Chainstack's pump.fun bot: listener race, `programSubscribe` for late curves, the current IDL;
  - the Rust streaming/landing SDKs;
  - Hummingbot's funding arbitrage (a template for edge-scout);
  - a small bot whose own notes show paper ~80% wins vs live 1 in 38.
- **Measured:** about 3% of pump.fun trades (11 of 369 in 20 s) are on USDC- or token-quoted coins. Their `sol_amount` and virtual SOL reserves are 0, so this bot reads them as price 0 and never trades them.
  - Proposed: decode `quote_mint` (after the event's variable-length fields) and skip non-SOL coins explicitly.
- **Measured:** ipfs.io and dweb.link return 429 to this machine, so per-launch social-link lookups likely fail often.
  - Proposed: the logo fetcher's gateway fallback for those lookups too.

---

## 2026-10-04 — Entry #22: The Desk, Portfolio, API keys, X feed, token logos

- **Desk tab:** a card for each agent with its current thought:
  - graduation scanner;
  - exit manager;
  - risk officer;
  - feed watchdog;
  - AI desk (four personas);
  - Claude operator;
  - wallet recorder;
  - the bench (strategies that are off).

  Below the cards:
  - **Scanner list:** each coin near the window with its live checklist. `strategy.late_checklist` mirrors `evaluate_late_entry` and is tested to agree with it on replayed markets.
  - **Exit gauges:** `strategy.exit_watch`.
  - **AI desk votes:** each persona's vote, conviction and reasons.
  - **Thought stream:** scanner verdict changes plus buys, sells and closes.
  - **𝕏 feed** and **research progress.**

  The scanner's thinking runs only on the live feed and never feeds a decision, so replays and frozen tests are unchanged.
- **AI desk:** can be woken and rested at runtime (`engine.set_desk`), once an Anthropic key is set.
- **API keys & connections (Controls):**
  - Covers the Anthropic key, Solana RPC/websocket URLs, Helius, PumpPortal, Telegram, X and Jupiter.
  - Keys are written to `.env` at mode 600 and applied to the running bot where possible.
  - The page only ever gets "set" plus a hint (last 4 characters, or the endpoint's host).
  - The live-trading confirmation flag and the keypair path can't be set from here.
  - The Claude agent has no access.
- **Wallet recorder control:**
  - Pause, pause for N hours, or daily quiet hours, via `data/wallets/control.json`.
  - The recorder closes the stream within about 5 s.
  - Each pause goes into `pauses.jsonl`.
- **Portfolio tab:**
  - Watch-only wallets: SOL, SPL and Token-2022 holdings priced via DexScreener's most liquid pair, and recent transactions.
  - The bot's paper book is shown alongside.
  - Found while testing: the PumpPortal-linked wallet holds 0.006 SOL (~$0.74), not the ~$15 it was funded with. The metered PumpPortal feed used before #10 would have spent it.
- **𝕏 feed:**
  - Reads FxTwitter's public API (profile timelines and search).
  - Polls one request at a time, each source every 2.5 min, and only while someone's looking.
  - Post text is shown as plain text; images only from twimg.com.
- **Token logos everywhere:**
  - `/api/logo/<mint>` uses the token's own metadata image, then DexScreener.
  - Fetching is public-address only with a 4 MB cap. Images are re-encoded by Pillow to a 96 px WebP (so the dashboard only serves images it made) and cached in `data/logos/`.
  - Falls back to coloured initials.
  - ipfs.io and dweb.link rate-limit this machine (HTTP 429, likely from the bot's per-launch metadata fetches), so IPFS links fall back to pump.fun's Pinata gateway, 4everland and public Pinata.
- **Fixed:** `fetch_metadata` (and the new image fetch) read only the first network chunk of a response (`content.read(n)` returns what has arrived so far), which could truncate a token's metadata and lose its social links. Both now read to the end with a cap (`feeds.read_capped`), with a regression test.
- Style: soft gradient backdrop, accented KPI cards, tab icons, a brand mark; the header fits on one row at laptop widths.
- Pillow added to requirements (logo thumbnails). 231 tests.

---

## 2026-10-04 — Entry #21: Wallet study (wallets-v1) registered, recorder running

- **Why:** an X article claims the edge is following wallets that were early on several different runners into
  coins at least 14 days old. It shows no record. That kind of hold takes days, so our execution delay doesn't
  matter, which makes it worth a proper test. See [WALLETS.md](WALLETS.md).
- **Rules registered first:** `research/policies/wallets-v1.yaml`, signature `904875fa93aa`, before any data.
  - Wallets are chosen in period A (≥ 14 days) and frozen.
  - Period B decides, against random-coin and random-wallet baselines, with the pass mark fixed in the file.
- **Recorder:** `python -m meme_trader.wallets record`, user service `meme-wallets`.
  - Discovery: 45 s samples of the PumpSwap stream every 15 min.
  - Metadata from DexScreener; trades from GeckoTerminal at 5 calls/min, for pump.fun coins' pools at least
    2 days old with at least $5K liquidity; daily candles for the dump gate.
  - `mode: stream` records every swap instead (complete, ~85 GB/day download, ~10% of a CPU core). The owner's
    machine runs it: no data cap. Its prices match GeckoTerminal's on the same trades.
- **Measured on the way:**
  - The full PumpSwap stream is 0.9 MB/s.
  - PublicNode drops most per-pool subscriptions and throttled this IP after a few dozen. The bot's feed failed
    over to api.mainnet-beta, which ran at 1.2 s lag with no gaps. The recorder therefore uses one PublicNode
    connection, never the bot's endpoint.
  - GeckoTerminal sustains ~5 calls/min.
  - Coins 14+ days old are ~3% of PumpSwap trades.
- Earliest verdict: around 2026-11-01.

---

## 2026-10-04 — Entry #20: All settings on the dashboard, and a chat buddy

- **Controls → All settings:** every other scalar setting (about 110: graduation plays, execution, sizing, exits, entry gates, copy, callouts, defense mode) can be changed live, with search, an "only changed" filter, its default, and the help text from `params.example.yaml`. **Save to config** writes the ones changed this run.
  - Lists, endpoints, keys and paths stay in the config file.
  - Owner only: the AI agent can still change just the curated settings, within its ceilings.
  - The frozen research test is unaffected: `research final` replays the policy's locked settings, not the live ones.
  - The settings that had no comment in `params.example.yaml` now have one.
- **Chat buddy:** a small animated character at the top of the Chat sidebar that shows what Claude is doing (thinking, working a tool, writing, waiting for your Approve, done, error), cheers or winces when a trade closes, sleeps if Claude Code isn't installed, and gives a tip when clicked.

---

## 2026-10-03 — Entry #19: Owner decision: leave it running

- graduation-v1 keeps collecting its holdout untouched; `research final graduation-v1` gives the verdict once there are 14 days and 150 trades (around 2026-10-17).
- The paper bot keeps trading graduation plays only, with the realistic 2.5 s order delay, the risk dial at Normal, copy trading off and agent buys off.
- **No version 2 for now.** The development data says graduation-v1 loses at realistic delays (#18).
- **No paid infrastructure yet.**
- If funds are ever needed (for example a capped live pilot to measure real landing times), they come from the wallet already funded for PumpPortal, and only with the owner's explicit go-ahead. Its address stays out of the repo.
- README screenshots refreshed (risk dial, market pulse, P&L by strategy and exit reason), captioned as the simulated demo.

---

## 2026-10-03 — Entry #18: Execution measurement: with a realistic delay, graduation-v1 loses

### Built
- **`execution.paper_delay_s`:**
  - a paper order now lands that many seconds after the decision, at the curve price then;
  - a buy fails past `slippage_pct` (15%) and still pays its fee;
  - a sell that lands below its floor fails (fee paid) and is re-sent through `sell_slippage_steps` / `sell_priority_fee_steps`, like the live executor;
  - this works the same in the live paper bot and in replays (an order queue settled on the engine clock);
  - 0 keeps the old instant fill.
- **Every closed trade records:**
  - `entry_vs_signal_pct` / `exit_vs_signal_pct` (all-in fill vs the price when we decided);
  - `entry_delay_s` / `exit_delay_s`;
  - `failed_fees_sol`.

  In live mode they're measured. Live fills also carry their timing: seconds to build / send / confirm, plus the landing slot and block time from `getTransaction`. The decision slot is journaled next to it, so a pilot can measure landing in slots.
- **`research latency`:** how far prices move within h seconds in the 55–85% window, overall and right after momentum, and how often a buy landing then would fail its slippage limit.
- **`research eval`:** adds a delay curve (0/1/2/3/5 s, no extra haircut) and an estimate at the expected real delay (measured feed lag + ~1 s to land).

### Measured (16.3 h before the freeze; Mayhem tokens left out)
| After | median move | p75 | p90 | buy would fail (>15%) | after momentum: p90 / fail |
|---|---|---|---|---|---|
| 1 s | 0.00% | +1.8% | +7.2% | 3.6% | +9.4% / 5.3% |
| 2 s | 0.00% | +3.4% | +10.3% | 6.0% | +12.5% / 7.8% |
| 3 s | +0.01% | +4.5% | +12.6% | 7.9% | +15.1% / 10.1% |

### graduation-v1 with orders landing later (same data, no extra slippage haircut)
| Delay | Trades | Net SOL | On cost | Failed buys / sell attempts |
|---|---|---|---|---|
| 0 s | 201 | **+2.70** | +13.0% | 0 / 0 |
| 1 s | 178 | **−0.40** | −2.4% | 23 / 25 |
| 2 s | 66 | −1.01 (daily loss limit) | −16.6% | 20 / 18 |
| 3 s | 88 | −0.95 | −12.7% | 31 / 20 |
| 5 s | 76 | −0.99 | −15.9% | 31 / 17 |

**Mechanism:** with a 1 s delay the strategy misses **7 of its 15 best instant-fill winners** (+1.68 SOL between them). Those buys fail because the price runs past the slippage limit first, while losses stay the same (−3.17 vs −2.91 SOL). The edge exists only with near-instant execution: the runners it needs are exactly the ones a delayed order can't catch.

**Realistic delay today:** the public RPC feed runs 1–2 s behind the chain, plus ~1 s for PumpPortal to build the transaction and for it to land, so 2–3 s in total.

### Consequences
- The live paper bot now uses `paper_delay_s: 2.5` with no extra haircut, so its paper P&L shows what live execution would get.
- graduation-v1 stays frozen and keeps collecting its holdout; its final report will include this delay curve.
- Development data points to the strategy failing at any delay this setup can achieve. A v2 would have to work with a 2–3 s delay, or execution would have to get well under a second (a paid low-latency feed, building transactions directly, Jito). Each needs its own cost-vs-benefit measurement.

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
