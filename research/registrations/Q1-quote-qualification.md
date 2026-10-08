# Q1: qualifying the live price watcher (FROZEN 2026-10-07, before its window; the owner's approval)

> **Amendment 1 (2026-10-07, ~23:20 UTC, before the window opened; the owner had delegated changes to the builder).**
> The window now runs **2026-10-09 00:00 through 2026-10-22 23:59 UTC** (same length and rules). A twelfth review
> found the observer under test was wrong in two ways that decide what this window measures:
> - pump.fun has published a newer Pool layout (287 bytes: `protocol_fees`, `creator_fees` at 271 and 279), so most of
>   the "undocumented pool bytes" are documented;
> - sells were checked against the whole quote vault, when the spendable part is the vault minus those fee buckets.
>
> The window must run entirely on the fixed observer (record v3), which is deployed before the new start. If it isn't,
> the start moves mechanically to the first complete UTC day after that deploy. No data from the original window
> existed when this was decided: it hadn't opened.

**What it decides:** whether the price watcher's measured availability may replace the planning envelope (A1–A5) in T9-E1's power. It's an **engineering** qualification, not a trading test and not a trading approval. Nothing here estimates transaction landing, failed-transaction fees or any edge.

**Where it comes from:** the eleventh review's proposal. The owner approved it on 2026-10-07 and asked the builder to change it where needed. The changes are marked **(change)**. The report is code, frozen with this document: `meme_trader/sniper/q1.py`, run with `python -m meme_trader.sniper q1-report`, tested in `tests/test_q1.py`.

> **Amendment 2 (2026-10-08, ~03:00 UTC, before the window opened; awaiting the owner's confirmation). Q1 gives no formal PASS/FAIL.**
> A thirteenth review showed the confidence bound was wrong for this data:
> - the pool-day bootstrap treats pools as independent, but a provider outage hits every pool at once;
> - an all-success sample bootstraps to a bound of 1.0.
>
> `research/q1_calibration.py` then ran 400 whole 14-day windows for each of 54 common-outage scenarios (outages of 2 minutes to 24 hours, and whole-day shocks), through Q1's own bound functions (`data/research/q1_calibration.json`):
> - **the pool-day bootstrap** passed up to ~40% of windows whose true eventual availability was below 95%;
> - **6-hour and 24-hour block bounds** never passed, even at 100%;
> - **1-hour blocks** passed up to ~43% under multi-hour outages, and almost never with ordinary 3% transient errors.
>
> No bound in this family both controls false passes and has power within 14 to 28 days. A whole-day common shock can't be certified at 95% in under about two months: 14 clean days support only 80.7%.
>
> **So the window runs as planned, on the fixed observer, and reports descriptively:**
> - per exit cell, the availability and qualification estimates;
> - exact time-block bounds at 1, 3, 6 and 24 hours, **each labelled with the independence it assumes**;
> - the pool-day bootstrap, as a diagnostic only;
> - the outage episodes seen.
>
> The measured outage process (episode rate and durations) may be **added** to T9-E1's power as a profile beside A1–A5, never replacing them. Nothing here is a statistical qualification, and no live approval is implied.

## The window

- **The days:** 14 complete UTC days, **2026-10-09 00:00 through 2026-10-22 23:59 UTC** (amendment 1). Jobs are assigned by their due time.
- **The start (change):** the first complete day after the wallet recorder moved to the paid stream (2026-10-07, about 9 s faster), so the pipeline is the same throughout.
- **No exclusions:** no day is excluded, and no restart is chosen for a better start. Deploys and restarts happen; every job in the window counts, and one missed during downtime is a failure.
- **Extension:** if a guard is short when the window ends, it extends a whole UTC day at a time, to at most 28 days. Still short at 28: **insufficient**, neither pass nor fail.
- **The verdict** waits until every window job has finished (exits retry for 15 minutes past their due time).
- **Looking during the window:** coverage may be viewed at any time (it carries no P&L). Nothing in this document or `q1.py` changes during the window.

## The population

Every quote job the revival forward test creates. That's both arms (signal and a random control pool), both delays (5 s and 60 s after the decision), and both time exits (1 h and 2 h), for all three exploratory rules.

- **Rules:** the +20%, +30% and +40% rules are reported apart in `by_rule`. They're not T9-E1's own population.
- **Correlation:** repeated holds on one pool, and several rules firing on one move, are correlated reads, not independent ones.

## Two estimands (change)

The proposal had one target: *qualified* coverage. The freeze keeps that target and adds a second estimand with the same targets:

1. **Availability:** the provider gave a coherent, fresh read (the slot within 2 of the feed's, a freshness reference present), validated and priced, **in hand by the job's deadline**. Its only qualification gap, if any, is undocumented pool bytes. This is what A1–A5 models (provider outages and failures), so it **is what may replace A1–A5 in power**.
2. **Execution qualification:** the same, **and** the pool's whole account layout is documented. It must **also** pass before any execution approval.

   A failure here caused by undocumented bytes is reported as `FAIL (undocumented pool layout)`. Undocumented bytes are never reclassified as documented.

**Why the split:**
- In the first two hours of v2 records, 50 of 64 entry quotes were prefix-only, on established pools whose accounts carry bytes past the documented 271-byte layout. Those bytes track virtual-reserve state.
- With one qualified target, a documentation gap would have read as a provider outage, and the power analysis would have been fed a false availability.
- The two questions have different owners: the provider for availability, pump.fun's documentation for qualification.

**What counts as a failure** for both estimands:
- a record without qualification (from before v2);
- a missed job (zero attempts);
- a late response (in hand after the deadline);
- every refusal reason.

## Guards (coverage, not independence)

- **Matured exits:** at least **200 per arm × delay × hold cell** (8 cells). Entries are reported in their own 4 cells.
- **Spread:** at least **30 distinct pools** and **100 distinct pool-days**.

## Targets and bounds (reference only since amendment 2)

- **The targets,** per exit cell, for each estimand:
  - eventual success: the one-sided 95% lower bound **≥ 95%**;
  - first-try success: the lower bound **≥ 90%**.
- **The bounds,** both reported:
  - **the clustered bound** (it decides): the 5th percentile of 2,000 bootstrap resamples of whole pool-days (seed 20261008);
  - **the simple binomial** beside it: a one-sided Wilson bound.
- **Zero failures isn't zero probability:** even 0 failures in 200 independent jobs leaves an upper bound of about 1.5%, and correlation makes that optimistic.

## Reported (since amendment 2, the report itself)

- **Per cell:**
  - raw, available and qualified successes, each first-try and eventual;
  - every reason, and zero-attempt counts;
  - pending jobs.
- **Outages:** runs of consecutive unavailable jobs (the count, the longest in jobs and in minutes), and the recovery time of exits that succeeded on a retry.
- **The pipeline clock:**
  - chain time to the recorder's receipt;
  - receipt to the decision;
  - due time to in hand;
  - the response time;
  - slots behind the feed.
- **Pools:** those with undocumented bytes, and results by rule.

## After it

- **What it permits:** if availability passes, its per-cell estimates may update T9-E1's planning.
- **What stays:** the A1–A5 and informative-missingness stresses, until enough regimes (congestion included) are seen. Availability is modelled by reason, time and state, not as one averaged rate.
- **What it can't estimate:** landing and failed-fee estimates need owner-authorized attempt records. The halt isn't lifted to collect them.
