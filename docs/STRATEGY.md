# Pump.fun Early-Entry Strategy — "Confirm, Then Ride"

Research notes and the reasoning behind `meme_trader/sniper/`. All parameters are in `config/params.example.yaml` under `sniper:`.

## 1. What the research says (why most people get screwed)

| Fact | Source | What it means for us |
|---|---|---|
| Only **~1% or less** of pump.fun tokens graduate. Some 2026 months were as low as **0.26%**. | [DEXTools](https://www.dextools.io/tutorials/what-is-pump-fun-solana-memecoin-launchpad-2026), [CoinLaw](https://coinlaw.io/memecoin-statistics/) | Base rate is brutal. Picking which tokens to buy matters more than anything else. |
| **>50% of tokens are sniped in their creation block.** In one month, 4,600 "sniper" wallets funded by 10,400 deployers took >15,000 SOL with an **87% hit rate**. | [Bitget / BlockBeats](https://www.bitget.com/news/detail/12560604803448), [ChainCatcher](https://www.chaincatcher.com/en/article/2185070) | Block-0 sniping is mostly **insiders sniping their own launches**. Retail block-0 snipers are their exit liquidity. **We can't win a race against the dev's own Jito bundle, so we don't enter it.** |
| Insiders in Telegram call channels bought **~100 s before the call** (median). One wallet front-ran 146 calls and sold after 72.6% of them. | [Money Leaves Clues](https://moneyleavesclues.substack.com/p/inside-the-economics-of-pumpfun-call) | Buying blindly when a call is posted makes you exit liquidity. Calls are a weak signal, and each caller's weight has to be learned from their results. |
| Median time to graduate ~2 min (75th pct 13 min). Price often falls **30–50% in the first 5 min after graduation**. | [J Tools](https://j.tools/en/blog/pump-fun-bonding-curve-mechanics-explained), [Altrady](https://www.altrady.com/blog/crypto-trading-strategies/pump-fun-solana-memecoin-trading) | Pump.fun moves in **minutes**, not hours. Exits must react in seconds. Default: sell *before* migration. |
| Only ~10% of sniper bots are consistently profitable, despite >70% chasing sub-50 ms latency. | [RPC Fast](https://rpcfast.com/blog/how-to-launches-snipe-pump) | Speed alone isn't an edge. The winners combine selection, discipline and infrastructure. |
| Tokens graduate at ~85 SOL in the curve (800M tokens sold). There is a 1.25% curve fee. LP is burned on migration to PumpSwap. | [Bitquery](https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/pump-fun-to-pump-swap/), [pump.fun fees](https://pump.fun/docs/fees) | We can compute exact fill prices from the curve reserves, which gives realistic paper trading and backtests. |

## 2. The strategy

### Entry: confirm, don't race
For every new launch (PumpPortal websocket, about 100s of ms behind the chain):

1. **Watch 15–240 s.** This lets bundlers and the dev show their hand. Being 15 s late costs some upside, and in exchange we avoid most rugs.
2. **Hard gates.** Any one of these rejects the token permanently:
   - the dev sold anything
   - the dev's initial buy is over 6% of supply
   - non-dev wallets bought over 15% of supply within 2 s of creation (a bundle)
   - early buyers have already sold over 35% of their bags
   - the creator launched more than 2 tokens in 24 h (serial deployer)
   - the same ticker was launched more than 3 times in 1 h (copycat)
   - the curve is already more than 40% sold
3. **Must hold at the moment of entry:**
   - curve progress of at least 4%
   - at least 15 unique buyers
   - top-10 holders at or under 35% of supply
   - net SOL inflow of at least 1 SOL over the last 20 s
4. **Score 0–100**, buying at 55 or above:
   - buyer velocity 25%
   - net inflow 25%
   - buy/sell balance 15%
   - distribution 15%
   - price near its high 10%
   - socials present 5%
   - learned caller weight 5%

### Exit: the classic, automated
1. **Initials out.** At **+100%**, sell exactly enough to get the cost back (fees included). The rest is a free ride.
2. **Trail the rest.** The trailing stop tightens as the run grows: 30% under +150%, 25% to +400%, 20% to +1000%, then 15%.
3. **Leave on red flags before the chart shows it:**
   - the dev sells
   - momentum decays (more than 1.5 SOL net outflow in 15 s while price is over 12% off the peak)
   - the move stalls (no new high for 90 s while in profit)
   - no follow-through (never reached +10% within 60 s)
   - the curve passes 92% (sell before the migration dump)
4. **Backstops:** −30% hard stop, 30 min maximum hold, and the kill switch.

### Risk
- Fixed **0.05 SOL** per entry and at most 4 open positions.
- Daily loss limit 0.2 SOL.
- A **30% drawdown** triggers the kill switch, which sells everything and halts.
- Paper by default. Live needs `--live`, `MEME_TRADER_CONFIRM_LIVE=yes`, and a dedicated hot wallet.

## 3. How we'd know it actually wins (no hype)

1. **Record.** `run` saves every live launch, trade and migration to `data/feed-*.jsonl`.
2. **Backtest on recorded data.** Replay it with `backtest --file`. Fills use exact curve math, our own price impact, 1.75% fees and a 3% latency haircut.
3. **Sweep parameters on one period, validate on another (walk-forward).** Tune only when results hold on data the tuning never saw.
4. **Paper trade live** for at least 1–2 weeks. Compare paper fills with what the curve did.
5. **Go live small.** Compare live fills with paper fills. The gap is our real slippage, and it feeds back into `paper_latency_slippage_pct`.

**The synthetic market is not evidence.** `SyntheticFeed` is a toy market I wrote to exercise the logic and the UI. In it, the strategy shows profit factors of 10–18 across seeds. That only proves the gates and exits do what they're designed to do: they rejected about 98% of synthetic bundle-rugs and made money on runners. Real markets have competing bots, adversarial devs who fake "organic" flow, and worse fills. Expect real numbers to be much lower and possibly negative until tuned on recorded data.

## 4. Roadmap (where a real edge can come from)

| Upgrade | Why |
|---|---|
| **Wallet-graph bundle detection.** Look up the funding source of early buyers through RPC/Helius. | Insiders fund snipers from the dev wallet. This is the strongest anti-rug signal in the research, and it beats our time-window proxy. |
| **Dev history.** Track each creator's past launches and outcomes. | Serial ruggers reuse wallets and funding paths. |
| **Smart-wallet follow.** Learn wallets with a consistently positive record of early buys (`subscribeAccountTrade`), and weight their buys. | "Who is buying" beats "how many are buying". |
| **Learned scorer.** Logistic regression / GBM on recorded launches (features at T+15–60 s → peak return). | The research shows graduation can be predicted from early-launch features. Replace hand weights once we have data. |
| **Faster rails.** Helius LaserStream/Yellowstone gRPC (50–120 ms detection) plus Helius Sender/Jito for landing. | Matters for exits during dumps more than for entries. |
| **LLM narrative check** with Claude: read name/ticker/metadata and the linked X account, and score narrative fit and bot-farm signs. | Cheap and fast enough at our 15 s+ timescale. It's also the AI angle worth showing off. |
| **Telegram/X ingestion** with per-caller learned weights (built, needs credentials). | CAs often appear socially at or before launch. |

## 5. Copy trading (added 2026-10-02, entry #3)

**Can we see other traders' pump.fun trades without a callout?** Yes. Every pump.fun buy and sell is a public Solana transaction, visible the moment it lands:
- on Solscan, on the wallet's pump.fun profile, and on trackers like [GMGN](https://gmgn.ai/blog/how-to-track-copy-solana-smart-money/) and [Kolscan](https://www.theblock.co/post/362119/pump-fun-makes-first-acquisition-purchases-solana-based-copy-trading-wallet-tracker-kolscan) (Kolscan was bought by pump.fun and is now free);
- for a bot, through [PumpPortal `subscribeAccountTrade`](https://medium.com/@pumpdevio/pump-fun-api-real-time-websocket-streaming-with-python-token-launches-trades-whale-tracking-2b3979fc26bb). It streams every trade by the wallets we list, costs 0.01 SOL per 10k events, and needs an API key.

**Why copy trading isn't free money:**
- **We always fill after the leader**, and after every faster copy bot. The `max_chase_pct` guard skips a trade when the price has already run past the leader's fill.
- **Known KOLs get farmed, and some farm their followers.** Some buy, wait for copiers, then sell into them. [ZachXBT has publicly accused large Solana influencers of exactly this kind of pattern](https://bitquery.io/investigations/ansem-black-bull-370x-investigation). So a wallet's own P&L is not the number that matters. **Our P&L copying it** is, and the bot tracks it per leader and auto-pauses a leader after 5 copied losses in a row. In the synthetic market, a "bait" wallet was profitable on its own trades while copying it lost money. The bot paused it.
- **Leaders run many wallets and rotate them.** The `leaders` command finds wallets in our own recorded data, ranked by realized + marked P&L. It flags insider-like wallets (buying inside the bundle window), deployers and bot-speed wallets.
- Research backs a careful approach: a 2026 multi-agent LLM study of meme-coin copy trading built to resist manipulative bots made about **+3% per copied investment** after realistic frictions. That's a real but thin edge, not a money printer ([ACM](https://dl.acm.org/doi/10.1145/3774904.3792635)).

**Modes:**
- `signal`: a leader's buy boosts the sniper's score.
- `mirror`: we buy when they buy and mirror their sells, plus our own stop loss, initials and trailing stop.

Start every new wallet on `signal`.

## 6. The agent team

| Agent | Kind | Job |
|---|---|---|
| Scout / feed | code | New launches, trades, migrations, leader-wallet trades |
| Gatekeeper | code | Hard anti-rug gates + 0–100 score |
| Copy agent | code | Leader buys/sells → chase, red-flag and rate-limit guards |
| Risk manager | code | Sizing, max positions, daily loss, drawdown kill switch |
| **AI desk** | Claude (4 personas) | Votes on every candidate entry, in parallel |
| ↳ Veteran trench trader | Claude | Order flow, holder spread, organic vs painted volume |
| ↳ Narrative trader | Claude | Culture/attention fit of the name/ticker/socials. This is the "Ansem-style" conviction lens. It's modelled on a public trading *style*, not impersonating anyone. |
| ↳ Risk officer / skeptic | Claude | Hunts red flags. A high-conviction pass **vetoes** the trade. |
| ↳ Quant | Claude | Base rates and expected value after fees |
| Executor | code | Paper fills on the curve / live PumpPortal tx, signed locally |
| Exit manager | code | Initials, tiered trailing stop, decay/stall, dev sold, leader sold |
| **Review agent** | Claude | "Head trader + senior engineer" post-mortem. It proposes parameter changes, and these are **backtested before anyone applies them**. |

How the desk is wired:
- **LLMs are kept off the exit path.** A red flag sells in milliseconds without waiting on an API call.
- Votes are aggregated deterministically (weighted conviction, quorum, skeptic veto), so every decision can be audited.
- Token names and metadata are written by the token's creator and could contain prompt injection. They are passed as data, and the model is told never to follow them.
- Default model: Claude Opus 5.5 at low effort. Server-side refusal fallback is enabled. The dashboard shows a cost meter.

## 7. Improvement roadmap (research, 2026-10-02)

1. **Funding-graph insider detection.** Use the [Helius "funded-by" Wallet API](https://www.helius.dev/docs/wallet-api/funded-by) on early buyers. Wallets that share a first funder with each other or with the dev are a cluster. Example: AVA AI had 40% of supply held by 23 deployer-linked wallets. Multi-hop funding is used to dodge this, so score the depth too.
2. **Learned scorer** trained on recorded launches (features at T+15–60 s → outcome). Replaces the hand-set weights. Train and validate walk-forward.
3. **LaserStream / Yellowstone gRPC** feed (50–120 ms) and **Helius Sender / Jito** for landing transactions. These matter most for exits in a dump, and for copy trades, where the chase cost is the whole game.
4. **Wallet-quality features for every buyer, not just leaders.** Count how many "smart" (historically profitable) wallets are among a token's first 50 buyers.
5. **A nightly scheduled review.** A routine runs `review --validate` and opens a PR with the changes that won on recorded data, so improvements ship only on evidence.

## 8. Intel from the owner's other bot (2026-10-03)

| Intel | What we did |
|---|---|
| Spikes round-trip within 2–5 min; speed decides everything | Already the design: websocket feed, decisions in seconds, fast exits. Trailing exits beat a fixed 5x target in backtests. |
| Pump.fun sells must use the AMM after graduation | Live trades use `pool=auto`; graduated trades are priced from market cap. |
| Fees paid ≥ 0.1–3 SOL; snipers < 20%; insiders < 30% | Added as entry filters (fees ≥ 0.1, snipers ≤ 20%, insiders ≤ 30%). |
| $5–$20 per buy from pre-check strength, hard cap $20, copies smaller | Sizing agent with a hard cap enforced in code; copies ×0.6; curve-depth cap. |
| 2x half / 5x rest / 0.3x stop, ratchet after 3x | `exit.profile: ladder`. Kept as an option; `trail` stays the default until real data says otherwise. |
| Re-quote failed transactions instead of blind retries | Buys are single-shot; sells re-quote with rising slippage; late landings are booked. |
| Close token accounts on exit | Done after every full live exit. |
| pumpdev.io websocket as a backup feed | Automatic failover; entries pause while on the backup. |
| Instagram "filter" settings | Treated as hypotheses. They're marketing funnels; every setting is a `--set` flag to backtest. |
| Callout bot | Built as the callout agent once the platform rules were clear ($1 minimum hold, rewards on volume): $1 bags, factual cards, tracked outcomes. See §9. |
| Insiders < 30% (deeper version) | Funding-graph cluster detection: Helius/RPC first-funder lookups, entry gate and hold-time exit. |

## 9. Ground rules
- **Callouts:** pump.fun requires holding ≥ $1 of a coin to call it out. We hold exactly that minimum, keep it at least an hour (unless the dev sells), never size up on a called coin to sell into the buyers a call brings, disclose the bag on every card, and track every call's outcome. Card text is measured data only, with no urgency and no promises.
- Read-only signals and our own trading only. No running call channels, shilling, bundling our own launches, or wash trading. Those are the behaviours this bot is built to avoid, and they carry legal risk.
- Every trade is journaled. Taxes apply to realised gains.
