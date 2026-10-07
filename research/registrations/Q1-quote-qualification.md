# Q1: qualifying the live price watcher (FROZEN 2026-10-07, before its window; the owner's approval)

**What it decides:** whether the price watcher's measured availability may replace the planning envelope (A1–A5) in T9-E1's power. It's an **engineering** qualification, not a trading test and not a trading approval. Nothing here estimates transaction landing, failed-transaction fees or any edge.

**Where it comes from:** the eleventh review's proposal. The owner approved it on 2026-10-07 and asked the builder to change it where needed. The changes are marked **(change)**. The report is code, frozen with this document: `meme_trader/sniper/q1.py`, run with `python -m meme_trader.sniper q1-report`, tested in `tests/test_q1.py`.

## The window

- **The days:** 14 complete UTC days, **2026-10-08 00:00 through 2026-10-21 23:59 UTC**. Jobs are assigned by their due time.
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

## Targets and bounds

- **The targets,** per exit cell, for each estimand:
  - eventual success: the one-sided 95% lower bound **≥ 95%**;
  - first-try success: the lower bound **≥ 90%**.
- **The bounds,** both reported:
  - **the clustered bound** (it decides): the 5th percentile of 2,000 bootstrap resamples of whole pool-days (seed 20261008);
  - **the simple binomial** beside it: a one-sided Wilson bound.
- **Zero failures isn't zero probability:** even 0 failures in 200 independent jobs leaves an upper bound of about 1.5%, and correlation makes that optimistic.

## Reported beside the verdict

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
