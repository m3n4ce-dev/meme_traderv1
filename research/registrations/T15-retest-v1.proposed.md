# T15-retest-v1: breakout, then retest, then persistent demand (PROPOSED, draft 1)

**Status: proposed, not registered.** It's the ninth review's specification (answer 4), written down here unchanged except where marked *builder note*. The constants are **proposals to register, not tuned settings**. It needs the owner's approval, the qualified quote observer, and entity clusters frozen before the window.

- **Observer only:** nothing is bought.
- **T9-E1 is unchanged.**
- **No parameter grid is backtested here.**

## The question

On established PumpSwap pools, does demand that survives a pullback, entered only once that survival is observed, beat entering on the first breakout? Both are compared as policy accounts on the same opportunities, after the real delay and costs.

## Eligibility and measurement (shared with T9-E1)

- **Pools:** SOL PumpSwap pools at least 2 days old, with as-of recorder liquidity ≥ $5,000, confirmed non-Mayhem, decided at actual observation time.
- **The breakout:** the frozen `t9-signal-2`: ≥ +40% over 300 s and a volume surge ≥ 4, on its exact window boundaries.
- **Excluded, and recorded** (never removed from the opportunity count):
  - feed lag > 5 s;
  - an observed feed gap > 10 s between the breakout and the confirmation;
  - an unresolved content conflict, or unknown wallet or depth inputs.
- **Later unknowns** can invalidate the retest policy when they become observable. They never remove the opportunity or the baseline's earlier trade.
- **States are computed incrementally** from events already observed. A future peak or trough is never assigned back to a decision.
- **Trade measures use unique, chain-verified swap events,** with decision-time availability recorded.

## State machine (per pool; one episode per 2 h, filled, expired or invalid)

1. **BREAKOUT at t0:** the first eligible `t9-signal-2` firing. Saved:
   - the anchor price (the t − 300 price) and the current price;
   - the effective quote depth `D0`;
   - snapshot hashes;
   - the opportunity id.
2. **WAIT_RETEST:** track the running peak since t0. The first observed price ≤ 90% of that peak within 600 s enters RETEST, and the peak freezes. No retest in 600 s means **expired**.
3. **RETEST:** track the running low. It's **cancelled** if the price is below 80% of the frozen peak, or below the anchor, or if fresh effective depth is below 80% of `D0`. The episode must stay in this state at least 30 s.
4. **RECOVERY at tr:** the first price ≥ 105% of the running low, after those 30 s, freezes the low and starts 120 s of confirmation. A price below the frozen low cancels the episode; nothing resets.
5. **PERSISTENCE,** at tr + 120 or the first eligible observation after it. Each of the windows (tr, tr+60] and (tr+60, tr+120] needs:
   - strictly positive buy-minus-sell SOL, from unique events;
   - at least **5** economic-entity groups each net buying ≥ 0.02 SOL;
   - the largest group ≤ 40% of the window's gross qualifying buy SOL;
   - fresh effective depth ≥ 80% of `D0`.

   Across both windows together: at least **8** groups, and at least **3** buyers not seen buying in the 60 minutes before t0.
6. **DECIDE** only when the second window can be computed, the observation is fresh, and the price is still ≥ 105% of the frozen low. Never at a past window's end.
7. **EXECUTE** as T9-E1:
   - wait 60 s;
   - take a fresh executable 0.25 SOL buy quote and an immediate sell quote;
   - impact ≤ 1% per side;
   - re-check the portfolio and safety gates;
   - never fill at the confirmation's print.
8. **EXIT** with T9-E1's one-hour rule and its retry and impairment policy. The exit isn't optimized alongside the entry.

**Deadlines:** a recovery must start by t0 + 780 s, and the decision by t0 + 900 s. A late observation skips the episode; it's never backdated.

**Independence:**
- **Clusters:** a clustering version frozen before t0. Known common funder, controller or bundle relationships collapse into one group.
- **Unresolved relationships aren't independence.** Only the defensible lower bound of distinct groups counts. If it can't reach the threshold, the episode is `independence_unknown` and skipped.
- **Raw wallet counts** are a diagnostic only.

**Depth:** signed effective quote reserves (`meme_trader/sniper/pumpswap.py`), converted consistently to SOL from coherent qualified snapshots. Fillability against the **real** vault is also required.

## Comparator and estimand

- **Comparator:** first-breakout T9 momentum, on the same initially eligible opportunities, in its own account and clock.
- **Estimand:** policy-level net cash P&L, missingness and capital use across **all** opportunities. Comparing only successful retest fills against all breakouts would be selection bias.
- **Fixed-stake matched-episode diagnostics** are kept separately.

## Builder notes (not changes to the specification; for the reviewer and the owner)

1. **The anchor test is redundant as written.** Cancelling below 80% of a peak that's ≥ 1.4× the anchor always fires before the price can reach the anchor (0.8 × 1.4 = 1.12 > 1). It's harmless, but it can be dropped without changing anything.
2. **Feasibility first.** Five or more distinct net-buying groups in each of two consecutive minutes, eight across both, and three new ones may be rare on 2-day-old pools: the T9 replay saw ~35 breakouts a day across the universe. Before freezing, a **no-outcome feasibility pass** should count how many episodes reach each state per day: breakout, retest, recovery and persistence. If almost none reach DECIDE, the observer can't produce a verdict in any reasonable window, and the thresholds need a decision **before** any outcome is seen, not after.
3. **The clustering it needs is only partly built.** `funding.py` resolves first funders for a coin's early buyers, not a frozen entity map for arbitrary wallets on established pools. Building that, and freezing it, is part of the prerequisite work.

## Prerequisites

- the quote observer, qualified;
- T9-E1's runner and accounts;
- a frozen entity-cluster artifact;
- the feasibility pass above;
- the owner's approval.
