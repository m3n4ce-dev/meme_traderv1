# Build Log — meme_traderv1

A running record of decisions, research, parameters and status. Newest entries at the top.

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
- Wallet (public): `HTG4jCTAVB6H8QThytKtrApjNaQhMuUENFi9Whppag6Z`. It's a valid 32-byte Solana key and is now set as `wallet.pubkey` in `config/params.yaml`. The sandbox can't reach Solana RPC, so its balance is unverified.
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
| 1 | Wallet public key (dedicated hot wallet) | — | `HTG4jCTA…pag6Z` (2026-10-02). Confirm whether this is the main wallet or a dedicated bot wallet. |
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
