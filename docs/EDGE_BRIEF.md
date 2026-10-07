# meme_traderv1: what's holding us back from an edge

*A brief for an external reviewer, 2026-10-07. Written by the builder (Claude) for the owner. Everything here is paper
trading on the real pump.fun market. All numbers come from the bot's own records, and each claim notes its sample
and its weakness. The repo's `docs/BUILD_LOG.md` (entries #55–#63) has the details.*

**What we're asking:** help find a tradable edge, or prove there isn't one where we're looking. Specifically: which
hypotheses are worth testing, how to test them without fooling ourselves, and whether faster infrastructure is worth
buying. Section 7 lists the questions.

---

## 1. Where things stand

| | |
|---|---|
| Mode | Paper only. No live money until a frozen strategy passes on unseen days. |
| Market | pump.fun bonding-curve coins (~45k launches/day), plus established coins on PumpSwap (2+ days old) |
| Account | 9 SOL paper start, now 3.47 SOL. Stopped by its 40% drawdown kill switch on 10-06. Research keeps running regardless. |
| Feed | Paid flat-rate Solana websocket (RPC Fast, since 10-06): 1.3–1.6 s behind the chain, 0.3–1.1% of trades missing. Before 10-06, free RPCs: up to 207 unreliable minutes a day. |
| Paper fills | Each order lands 2.5 s after the decision, at the curve price then. Costs: 1.25% curve fee + 0.5% platform fee each side (3.5% round trip) + priority fee. |
| Recordings | 4 full days of every pump.fun trade (10-03 → 10-06, ~2.5M trades/day, ~360 MB/day gz). PumpSwap trades on established coins since 10-04 (~400k/day). |
| Strategies on | Graduation plays only (late-curve momentum, out before migration). The sniper is off (losing, not by bad luck). The bots never buy Mayhem-mode coins (owner's rule, read on-chain). |

### Edge check (the bot's own trades, day-clustered bootstrap)

| Strategy | Trades | Per SOL | Average (90% range) | Median | Without best 3 | Verdict |
|---|---|---|---|---|---|---|
| Graduation plays | 225 / 3.2 days | +0.3% | +1.9% (−10.6% to +9.7%) | −9.0% | −4.16 SOL | Too few days (4 < 5) |
| Sniper (off) | 78 / 3.1 days | −13.4% | −13.8% (−18.0% to −9.6%) | −16.7% | −2.37 SOL | Losing, not by bad luck |

---

## 2. Why the bots' trades lose (303 paper trades)

1. **Every round trip costs ~9–10% before the coin moves.** The bots pay a median **2.5%** above the decision price,
   because the buy lands 2.5 s later. They sell **3.6%** below the trigger, because a falling coin keeps falling for
   2.5 s. Fees add **3.5%**. The median graduation trade is −9.0%, almost exactly that cost.
2. **Adverse selection.** **46%** of graduation buys and **53%** of sniper buys never rose after the order landed.
   Faster participants see the same move, buy first and sell to us.
3. **The exits aren't the problem.** Of the last 37 coins the bots sold, 24 are 20%+ lower now (median −35% after the
   sell).
4. **A lottery shape.** 13 graduation trades that rode to the curve's end averaged **+143%** and pay for everything;
   without the best 3 trades the strategy is −4.16 SOL.

---

## 3. What we've tested, and what it showed

Unless noted, replays use the recordings and fill buys and sells late (2.5 s, as the paper bot does), after fees.
Oct 3–6 overlaps the frozen `graduation-v1` holdout, so everything below is development data, not a verdict.

| # | Hypothesis | Result | Status |
|---|---|---|---|
| T1 | **A better model of "doubles before −30% in 10 min"** (LightGBM, 36 features, snapshots 15 s–3 min after launch) | Held-out AUC **0.86** (logistic: 0.83), calibrated up to ~30%. Its top picks double 24–27% of the time (base ~5%). | Real ranking skill, but see T2 |
| T2 | **Trade the model's picks** (≥25–30%, stop 40 / trail 25–30) | **Instant fills: +11% to +16%/trade** on two unseen days. **0.5–2.5 s fills: about break-even.** Live forward follow since 10-06 (217 picks, read at our real lag): **−9% to −22%/pick**, instant fills included. | Dead at our speed |
| T3 | **Retrain for the late fill** (label measured from the price 2.5 s later) | AUC 0.86 on that label; trades still lose at every threshold | Dead |
| T4 | **Hold high-conviction picks longer** (up to 1 h, wide or no stops) | Within noise of zero; the median pick is −66% after an hour | Dead |
| T5 | **Slower coins** (5–30 min old) | Lost on both test days (−4% to −22%/trade) | Dead |
| T6 | **The owner's manual style as a rule** (half-full curve, heavy buying, a 10–35% dip, sell fast): 540 variants × 4 days | **0 of 540** made money; all lost 3–7%/trade on every day | Dead (owner's 29 trades: +0.07 SOL = noise) |
| T7 | **Predict which half-full coins graduate** (late-fill label, bot-like exits) | Top picks graduate **26–36%** (base 3–8%), yet lose **2–14%/trade**: non-graduators dump harder than graduators pay | Dead |
| T8 | **Graduation-play settings** (rules only, 3 days) | 2.5 s fills **+0.16 SOL**; 1.5 s +1.29; 1.0 s +1.18; **0.5 s +3.37**. Price caps 3/5/8% worse. Oct 5 lost the same at every speed. | **Speed matters**, but see §4 |
| T9 | **"Second-life" momentum on established coins** (PumpSwap, 2+ days old, $5k+ liquidity): +40% in 5 min on 4× volume, hold 1–2 h | Positive **every day** (Oct 4/5/6: +10.5 / +15.5 / +8.0% per trade at 5 s fills, 1.2% costs; **better with 60 s fills**: +13.7 / +16.2 / +24.9%; still positive at 2× costs). **Carried by 3 real revivals** (+664% to +1318% on $0.6–1.8M of volume); without the best 3 trades, about flat. Buying dips there lost every day. | **The only live candidate.** Forward test v2 running since 10-07 (exploratory) |
| T10 | **The AI team** (4 personas on Claude vote on each graduation candidate) | Over 155 scored calls its buys did **10 points worse than its passes** at the median (buy −15.4%, pass −5.3%) | No value yet. It practises while the bot can't buy and reads its own record. Since 10-06 each persona must name who's selling to us and why they're wrong. |
| T11 | **Copying KOL wallets** (10-05) | −13% a trade at 1–2.5 s latency | Dead at our speed |
| T12 | **"Smart wallets" (wallets-v1)**: follow wallets that were early on several runners, on 14+-day-old coins | Rules registered before any data; period A ≥14 days, then period B | Running; verdict ~11-01 |

**Data being collected for the next tests:**
- **X links** (1,079 so far): each coin's linked post or account, with followers, reach and age, read once the coin
  is active.
- **Graduated coins' 1-minute pool candles** (6 h after graduation).

---

## 4. The bottlenecks, as we understand them

**B1. Latency on the bonding curve.**
- The measurable edge in T2 and T8 lives in the first 0.5–1 s after a move becomes visible.
- **Ours today:** ~1.3–1.6 s of feed lag behind the chain, plus send and landing, which we model as 2.5 s from
  receipt.
- **Unmeasured:** we haven't measured our own landing-time distribution, failed-fill rate, or the gap between
  receive time and chain time at sub-second resolution (chain timestamps are whole seconds).
- **What faster would take:** a streaming feed (Yellowstone gRPC), fast landing (Jito / SWQoS / RPC Fast Beam), and a
  server co-located near the RPC (Frankfurt), at roughly $100–500+/month more.

**B2. Costs.**
- 3.5% fees round trip plus ~6% in slippage from being late: a curve strategy needs >10% expected move per trade just to
  break even.
- PumpSwap fees are ~0.3%/side, which is why T9 is interesting.

**B3. Adverse selection.** What we can see in 1.5 s, faster bots saw in milliseconds. Momentum we buy is often their
exit.

**B4. Our fill model is simple.**
- Paper fills are a constant delay on the *received-event* timeline, at the curve price then. We don't model a faster
  feed revealing events earlier (as the round-4 review noted), latency jitter, own-order price impact on the curve,
  failed or expired orders, variable creator fees (some coins charge 3%), or retry costs.
- The 0.5 s replay in T8 is a lower-delay *on the same timeline*, not a simulation of a different feed.

**B5. Small, regime-dependent samples.**
- Four days of recordings.
- Returns are lottery-shaped (a few trades carry everything), and results swing by day: Oct 5 lost everywhere.
- The feed was degraded on parts of Oct 4–6.
- Any candidate needs many independent days before it means anything.

**B6. The model's target isn't the trade.**
- Models predict "+100% before −30% within 10 min", a barrier event.
- The strategies trade with trails, partial sells, time exits, failed fills and fees.
- **Ranking skill (AUC) didn't translate into P&L at our latency.**

**B7. Research hygiene limits.**
- Several looks at the same days.
- `graduation-v1`'s holdout overlaps the development data used today.
- Revival runs 18 correlated variants.
- We've been careful to label things exploratory, but we don't yet have one frozen, prospective primary test of a new
  idea.

**B8. Live readiness (blocks real money, not research).**
- **Order outbox:** the round-4 review found that a crash between signing and saving an order could lose it. A
  durable order outbox is now built and tested (build log #64).
- **Executor:** the DexScreener/Jupiter executor is blocked until it shares the pump.fun engine's order lifecycle.

---

## 5. What we think the options are

1. **Leave the bonding curve's first minutes to the fast bots.** At our latency the evidence (T2–T7, T11) says there's
   nothing there for us.
2. **Established coins (T9)**: a slower market, lower fees, minutes not seconds.
   - **Why it's promising:** it was the only positive result, and it got *better* with a later fill.
   - **Risk:** it's carried by rare, huge revivals. Three days, three revivals.
3. **Speed, for graduation plays (T8).** It's only worth paying for if a realistic latency model, on new days, shows
   incremental net P&L above the subscription plus higher fees and tips. Our plan, from the round-4 review:
   - timestamp every stage of a real order;
   - measure p50/p90/p99 distributions;
   - replay with separate observation and landing clocks, failed orders, own impact and real fees;
   - freeze one latency policy and test it on new days;
   - only then price the infrastructure.
4. **Information:**
   - X posts: who posted the coin's link, their reach, how fresh it is.
   - Graduated-coin behaviour in the first hours on PumpSwap.
   - Both are data we only started collecting on 10-06.
5. **Wallet-based signals (wallets-v1)**: already registered; waiting for its period B.

---

## 6. What we can give a reviewer

- **Public:** the repo (`m4n3ce/meme_traderv1`, public, GPL-3.0, paper-first), the build log, and the tests (424 pass).
  The research scripts for T1–T9 live in the builder's scratch space; we can add any of them to the repo.
- **Private, on the owner's machine (shareable on request, as summaries or samples):**
  - the recordings (4 days, ~1.4 GB gz) and the PumpSwap trades (3 days);
  - the trained models (`model-trees.json`, `model.json`, feature version 2);
  - the bot's trade records (paper);
  - the exit lab, the model-pick follow, the AI-team record, and the X-link log.

---

## 7. Questions for the reviewer

1. **T9 design.** How would you freeze and run a confirmatory test of "second-life momentum"? Specifically:
   - which single rule, delay, exit and order size to register;
   - how many days, or how many revivals, we need before it can mean anything given the lottery shape (one revival
     a day carried it);
   - how to model executable costs and impact for a 0.25 SOL order in pools with $5k–$500k of liquidity;
   - what the right baseline is (we follow one random active established coin per signal).
2. **Latency.** How do we build a realistic latency model from what we can measure? (Receive time, integer-second
   chain time, slot; we can instrument decision, build, sign and send, and landing slot for live orders once we
   trade.) Is 0.5 s total realistic for a home-run bot with a paid gRPC feed and a landing service, or does it need
   co-location?
3. **Model target.** What target and evaluation would you use instead of a barrier label? For example, net return
   under one frozen exit, conditional on a fill at our latency. How should the strong ranking skill be used:
   - as a *filter* that skips coins likely to dump (risk reduction),
   - or as a buy signal?
4. **Statistics for lottery-shaped returns.** With day-clustered bootstrap and 5+ days required, what else? For example:
   - trimmed means;
   - "without the best k" checks;
   - a minimum number of independent big winners.
5. **Where else would you look,** given our constraints?
   - Paper first, small bankroll.
   - Retail-grade latency unless proven worth paying for.
   - Python, one machine.
   - Solana, maybe other chains later.
6. **Anything in §3 we've concluded wrongly,** where a different setup would have found what we missed.

---

*Constraints from the owner: paper until an edge is proven; no Mayhem-mode coins for the bots; the owner's manual
trades are never restricted; keys and wallets never in the repo.*
