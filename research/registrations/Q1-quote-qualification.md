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

**What it decides:** ~~whether the price watcher's measured availability may replace the planning envelope (A1–A5) in T9-E1's power~~ *(superseded by amendment 2: it decides nothing formally; a measured profile may only be ADDED beside A1–A5)*. It's an **engineering** qualification, not a trading test and not a trading approval. Nothing here estimates transaction landing, failed-transaction fees or any edge.

**Where it comes from:** the eleventh review's proposal. The owner approved it on 2026-10-07 and asked the builder to change it where needed. The changes are marked **(change)**. The report is code, frozen with this document: `meme_trader/sniper/q1.py`, run with `python -m meme_trader.sniper q1-report`, tested in `tests/test_q1.py`.

> **Amendment 2 (2026-10-08, ~01:50 UTC, before the window opened; CONFIRMED by the owner on 2026-10-08 at ~05:50 UTC - see amendment 4). Q1 gives no formal PASS/FAIL.**
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

> **Amendment 3 (2026-10-08, ~03:45 UTC, before the window opened; the builder; its observation rules and its fitting recipe are SUPERSEDED by amendment 4, before any data).** It does two things:
> - it renames a statistic;
> - it pre-specifies how any outage profile will be built, before the data exists (a fourteenth review).
>
> **The renaming.** Final job outcomes are not a provider up/down trace. The report's spans of unavailable jobs are **eventual quote-failure spans**: sampled at scheduled times, missing recovered interruptions, with detection points rather than onsets. The provider is observed by the **attempt-level product** (`transport_observations`).
>
> **The attempt-level product.** ~~Every quote attempt observes the read path at its request time:~~
> - ~~a coherent answer, even a refusal about one pool, is UP;~~
> - ~~`timeout`, `rpc_error`, `slow_response` or `stale_state` is DOWN.~~ *(superseded by amendment 4: an attempt observes the read path only if it made a read, and a stale or slow response is a degraded response, not a transport failure)*
>
> Each run of DOWN observations gets a censored bracket:
> - **its minimum:** first to last failure;
> - **its maximum:** the last UP before to the first UP after;
> - **censoring:** flagged left or right when no UP bounds it in the window.
>
> Gaps of 10 minutes or more with no attempt are **unobserved**, never up. Process downtime (jobs missed with no attempt) is counted apart, and so are pool-specific causes (liquidity, layout, state).
>
> ~~**The fitting recipe, fixed now:**~~ *(superseded in full by amendment 4's specification, before any data: points 1-3 assigned censored runs a fabricated maximum and treated two separate 95% bounds as a joint one; points 5-6 carry over)*
> 1. **What's fitted:** only the transport layer's DOWN-run process, from the brackets: a run rate per day, and durations as an interval-censored sample (each run lies between its minimum and maximum; censored runs carry only their minimum).
> 2. **When there's too little to fit** (fewer than 10 uncensored runs): no fitted law. The profile is the conservative envelope:
>    - **the rate:** the observed rate with its exact Poisson upper 95% bound;
>    - **durations:** every run at its maximum, censored runs at the longest observed maximum.
> 3. **With 10 or more:** an exponential and a log-normal interval-censored fit. The profile uses the **longer-tailed** of the two at its upper 95% bound, never the better-fitting one.
> 4. **Unobserved time:** it isn't filled. A sensitivity profile treats every unobserved gap as down.
> 5. **What's recorded:** at window close, the fitted parameters are timestamped with the code revision. The profile is added to T9-E1's power as **one more profile beside A1–A5** and the informative-missingness stress, never replacing them, with the same no-strategy-selection rule (parameters are never chosen by any strategy's P&L).
> 6. **Probes:** fixed-schedule read-only probes would observe the provider independently of signals. They'd need the owner's authorization and a cost budget, and aren't part of this window.

> **Amendment 4 (2026-10-08, ~06:20 UTC, before the window opened; the builder - the observation rules take effect with the deploy that carries them, before the window opens; the fitting specification awaits the owner's confirmation before any profile is used).** A fifteenth review.
>
> **The owner's decision, recorded apart from any merge or review.** On 2026-10-08 at ~05:50 UTC the owner wrote, in chat: *"you can do the measurement change if it will help or improve etc. no big deal"* - confirming amendment 2 (descriptive, no formal PASS/FAIL), under the standing delegation of 2026-10-07 (*"approve or change the 14 day measurement where you see fit"*). Merging the pull requests was never taken as confirmation.
>
> **The observation product, corrected (the code in `q1.py` and quote record v5):**
> - **Three states, from read-stage evidence.** Each attempt now records every network read it made (`reads`: stage, start, end, responded or failed). An attempt is UP only if a read responded, DOWN only if a read was attempted and failed (timeout, HTTP or RPC error, a malformed answer), and UNKNOWN otherwise - a cached or local refusal, a local error (now `local_error`, no longer `rpc_error`), or a legacy record that can't be placed. Unknown attempts observe nothing and cover no time.
> - **Degraded is not down.** A response outside the freshness or latency limits (`stale_state`, `slow_response`) is UP for the transport and DEGRADED for the read path. Two products are reported: transport runs, and read-path runs (down or degraded). A stale answer may be the provider's lag or the reference feed's.
> - **Complete elapsed coverage.** Exposure is all elapsed window time, clipped to `min(now, window end)`: future time is never counted. It is OBSERVED only between observing reads at most 10 minutes apart. The leading and trailing edges and an empty window are unobserved, and the unobserved intervals are listed.
> - **Detected failure runs.** A run is consecutive failed observations within one SEGMENT: reads at most 10 minutes apart, on one host. An unsampled gap or a host change always ends a run, so unobserved time is never counted as continuous downtime.
>   - Each run's edges say what bounds it: an up observation, the window's start or end, the clip, a gap, or a host change.
>   - Runs separated only by a gap or a host change are tagged as possibly one episode.
>   - `min_s`, first to last failure, is a duration only under the declared one-episode assumption within a segment.
>   - The maximum is bounded only by up observations. A second maximum through gaps is labelled as including unobserved time.
> - **Epochs and the process.** Observations are partitioned by host and counted by code revision. The watcher writes a heartbeat (`liveness` intervals), which splits unobserved time into two parts: the process up with no reads, and the process down or unknown. A same-process heartbeat can't witness its own crash.
>
> **The profile specification, replacing amendment 3's recipe (frozen now; used only after the owner confirms it):**
> 1. **Estimands, per host epoch, for the transport and the read path separately:**
>    - the DETECTED-run rate per *observed* hour (unobserved hours are not exposure);
>    - the survival of detected-run durations, S(h) = P(a detected run lasts more than h), at the frozen horizons **h = 30 s, 60 s, 15 min, 1 h, 2 h**.
>
>    Both are properties of runs as this sampling detects them, not of physical episodes. Runs shorter than the spacing between reads are missed, and one run can hide several episodes. The detection resolution is reported beside them: the distribution of spacings between observing reads in observed time.
> 2. **Durations are partially identified, never filled in.** Each run lies in [`min_s`, `max_s`], and a censored side is open (no maximum). At each horizon:
>    - **the lower count** is the runs with `min_s > h`;
>    - **the upper count** is the runs with `max_s > h` or no maximum.
>
>    No duration is assigned to a censored run.
> 3. **The stress profile is nonparametric.** At each horizon it takes the exact one-sided Clopper-Pearson upper bound of the upper count out of n runs, at alpha = 0.05/5 per horizon (Bonferroni across the five horizons). Past 2 hours, runs still possibly running are continued to a declared **horizon stress of 24 hours**, with a sensitivity at 6 hours. The rate uses the exact Poisson upper bound on detected runs per observed hour, at the same alpha.
> 4. **The edge cases are declared:**
>    - with no runs, the profile is the declared horizon stress at the rate bound for zero runs;
>    - with every run censored, every upper count is n, so the stress is every run lasting to the horizon stress;
>    - fewer than 10 runs isn't a reason to fit anything. Parametric fits (exponential, log-normal) are reported only as diagnostics, never as the profile, because their tails can cross at the horizons that matter.
> 5. **Stress, not confidence, until calibrated.** These are labelled **stress scenarios**, not "conservative 95%". The confidence language is allowed only if a calibration of the whole observe → bound → power pipeline shows it. That calibration simulates latent outage processes, samples them with the window's actual read timestamps as the sampling operator (job-conditioned, retries included), applies this observer and these bounds, and finds coverage of the true S(h) and rate of at least 95% across a grid declared before it runs. The grid includes sparse, all-censored, boundary and no-sample cases.
> 6. **Kept apart:**
>    - an unknown-as-down sensitivity (every unobserved interval treated as down) is reported beside the profile, never merged with measured downtime;
>    - process downtime (missed jobs, heartbeat gaps) and pool-specific causes stay separate;
>    - the profile is added beside A1-A5 and the informative-missingness stress, never replacing them, and is never chosen by any strategy's P&L.
> 7. **Probes** remain outside this window. If the owner authorizes a fixed-schedule probe, it gets its own registration, using the fifteenth review's minimum contract:
>    - the schedule is persisted before execution, and its denominator is every scheduled probe;
>    - UP only for a validated coherent read;
>    - no overlapping probes, and retries kept as secondary observations;
>    - an external heartbeat;
>    - read-only, with a budget.

## The window

- **The days:** 14 complete UTC days, **2026-10-09 00:00 through 2026-10-22 23:59 UTC** (amendment 1). Jobs are assigned by their due time.
- **The start (change):** the first complete day after the wallet recorder moved to the paid stream (2026-10-07, about 9 s faster), so the pipeline is the same throughout.
- **No exclusions:** no day is excluded, and no restart is chosen for a better start. Deploys and restarts happen; every job in the window counts, and one missed during downtime is a failure.
- **Extension:** if a guard is short when the window ends, it extends a whole UTC day at a time, to at most 28 days. Still short at 28: **insufficient**, neither pass nor fail.
- ~~**The verdict** waits until every window job has finished~~ *(amendment 2: there is no verdict; the report is final once every window job has finished - exits retry for 15 minutes past their due time)*.
- **Looking during the window:** coverage may be viewed at any time (it carries no P&L). Nothing in this document or `q1.py` changes during the window.

## The population

Every quote job the revival forward test creates. That's both arms (signal and a random control pool), both delays (5 s and 60 s after the decision), and both time exits (1 h and 2 h), for all three exploratory rules.

- **Rules:** the +20%, +30% and +40% rules are reported apart in `by_rule`. They're not T9-E1's own population.
- **Correlation:** repeated holds on one pool, and several rules firing on one move, are correlated reads, not independent ones.

## Two estimands (change)

The proposal had one target: *qualified* coverage. The freeze keeps that target and adds a second estimand with the same targets:

1. **Availability:** the provider gave a coherent, fresh read (the slot within 2 of the feed's, a freshness reference present), validated and priced, **in hand by the job's deadline**. Its only qualification gap, if any, is undocumented pool bytes. This is what A1–A5 models (provider outages and failures), so ~~it **is what may replace A1–A5 in power**~~ *(amendment 2: a profile measured from it may be added beside A1–A5, never replace them)*.
2. **Execution qualification:** the same, **and** the pool's whole account layout is documented. It must **also** pass before any execution approval.

   ~~A failure here caused by undocumented bytes is reported as `FAIL (undocumented pool layout)`.~~ *(amendment 2: no FAIL label; undocumented bytes appear as the `ok, unqualified: undocumented_pool_bytes` reason.)* Undocumented bytes are never reclassified as documented.

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
  - **the clustered bound** ~~(it decides)~~ *(amendment 2: a diagnostic only)*: the 5th percentile of 2,000 bootstrap resamples of whole pool-days (seed 20261008);
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

- ~~**What it permits:** if availability passes, its per-cell estimates may update T9-E1's planning.~~ *(amendment 2: nothing passes; amendment 3's profile, if the data supports fitting it, may be added to power beside A1–A5.)*
- **What stays:** the A1–A5 and informative-missingness stresses, until enough regimes (congestion included) are seen. Availability is modelled by reason, time and state, not as one averaged rate.
- **What it can't estimate:** landing and failed-fee estimates need owner-authorized attempt records. The halt isn't lifted to collect them.
