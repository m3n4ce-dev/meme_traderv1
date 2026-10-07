# T20-cohort-v1: several independent KOLs accumulating, then a meaningful distribution (PROPOSED, draft 1)

**Status: proposed, an exploratory observer first.** The owner's idea, sharpened by the ninth review (section 5). It's not T13-C1, and it doesn't change T13's future frozen wallet list. Constants are unoptimized candidates, for a coverage-only qualification before anything is frozen.

**The hypothesis isn't "three famous wallets bought, so buy", or "one tiny sell, so dump".** It's persistent accumulation by defensibly independent wallets, followed by a meaningful distribution. Several addresses aren't automatically several traders: wallets carry invisible inventory, receive transfers, coordinate, and sell into their followers.

## Selection and observation

- **Freeze the wallet list first.** The list, its selection dates and its entity clusters are frozen from an earlier period and hashed. No adding the day's big winner after seeing its coin.
- **Only what was observed before the decision counts.** Reconciliation delay is part of the strategy's latency.
- **Established, qualified SOL PumpSwap pools first.** This needs the quote observer and the fixed wallet ledger (`copytrade.LeaderBook`).
- **Window flow isn't inventory.** Observed net accumulation in a window is kept apart from total on-chain inventory. With unknown opening inventory or transfers, inventory-based readings are marked unknown.

## Entry candidate

1. A fixed 300 s window contains at least **3 defensibly independent** frozen-KOL groups. Each group's trades collapse into one vote.
2. Each group nets ≥ **0.20 SOL** of buys in that window. Transfers aren't buys.
3. The broad market's unique-event net SOL flow is positive in each of the last two non-overlapping 60 s windows.
4. Effective quote depth hasn't fallen more than 20% during the window. Fresh executable quotes pass the same ≤ 1% per-side impact and safety gates.
5. Decide once these facts exist, wait the frozen execution delay, and re-check. Unknowns and rejections are recorded.
6. One cohort opportunity per pool per 2 h, accepted or not.

## Exits: four on the same entries, with one primary chosen BEFORE any outcome is seen

At entry, each group's positive window-net acquired tokens `A_g` are saved. That's a window-flow baseline, not full inventory. `S_g(t)` is the group's gross observed sold tokens since entry; rebuys are reported separately.

1. **First qualifying cohort sell** (diagnostic): any frozen group sells ≥ 0.02 SOL of notional.
2. **Meaningful distribution** (diagnostic): at least 2 independent groups each sold ≥ 20% of `A_g`, and Σ min(S_g, A_g) / Σ A_g ≥ 25%.
3. **Broad net outflow** (diagnostic), all of:
   - buy-minus-sell < 0 in two consecutive non-overlapping 60 s windows;
   - net outflow ≥ 2% of fresh effective depth;
   - ≥ 5 distinct selling groups, with no group above 60% of gross sell SOL.
4. **Distribution and outflow together** (the proposed primary): 2 and 3 at the same time. The rest of the position is sold through the executable, delayed and retrying order lifecycle.

**Every arm keeps** the baseline stop, time, rug and liquidity exits.

**Order rule:** once an exit order is submitted or unknown, later cohort sells never submit another fresh full sell. A trim counts as done only after its partial fill.

## How it's judged

- **First, paired same-entry fixed-stake comparisons.** Then independent constrained policy accounts on the same future opportunity stream.
- **Measured:**
  - incremental net cash P&L, not the KOLs' win rate;
  - avoided drawdowns against lost upside;
  - exit delay, failed attempts, impact and costs;
  - false or dust triggers;
  - inventory, entity and history unknown rates;
  - concentration in the largest episodes.
- **The entry question is separate.** A better exit on cohort entries doesn't show that the KOL count picks good entries.

## Builder notes (for the owner and the reviewer)

1. **Our own history argues for a feasibility check first.** T11, copying KOLs at our 1–2.5 s delay, lost about 13% a trade: the KOLs we tracked held a median 34 s, mostly on fresh bonding-curve coins.
   - **The doubt:** whether 3 independent frozen KOL groups ever accumulate ≥ 0.2 SOL each within 300 s on **2-day-old PumpSwap pools** is an empirical question.
   - **The check:** a coverage-only pass, counting cohort windows per day with no outcomes, answers it before anything is frozen.
2. **A frozen KOL and entity artifact doesn't exist yet.**
   - **What exists:** `data/kols.json` is a scraped list, not a frozen selection with dates, and entity clustering is limited to first funders.
   - **What's needed:** the selection period, the freeze, and the cluster map, all done before the first evaluated day.
3. **The fixed wallet ledger** (eighth and ninth reviews) is what makes `A_g` and `S_g` trustworthy. Bags with unknown history or order are marked `unknown_bags` and must be shown as unknown here, not counted.

## Prerequisites

- the quote observer, qualified;
- frozen KOL and entity artifacts;
- the feasibility pass;
- the owner's approval;
- for the exits: the realistic delayed, partial and failing exit lifecycle (built in the exit lab, round 9).
