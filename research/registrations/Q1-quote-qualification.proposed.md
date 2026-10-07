# Q1: qualifying the live price watcher (PROPOSED: not frozen, needs the owner's approval)

**What it decides:** whether the watcher's measured availability may replace the planning envelope (A1–A5) in T9-E1's power. It's an **engineering** qualification, not a trading test and not a trading approval. The criteria are the eleventh review's proposal, unchanged. Freezing them before the window starts is the owner's call.

## The window

- **14 consecutive complete UTC days.** No day is excluded for being quiet or bad, and no restart is chosen for a better start. If a guard below isn't met by day 14, the window is extended. The guards are never relaxed after anything is seen.
- **The population:** every quote job the revival forward test creates (signal and control arms; 5 s and 60 s delays; 1 h and 2 h holds). The exploratory +20/+30/+40% rules aren't T9-E1's +40% rule, so the opportunity populations are reported apart and never treated as interchangeable.

## Guards (coverage, not independence)

- **Matured exits:** at least **200 per arm × delay × hold cell** (8 cells). Entries are counted in their own cells. Repeated holds on one pool are correlated, not independent.
- **Spread:** at least **30 distinct pools** and **100 distinct pool-days**.

## What is reported, per cell

- **Successes:** raw first-try, qualified first-try, raw eventual and qualified eventual.
- **Everything else:** missing and unqualified jobs by reason, including zero-attempt jobs (missed or past the deadline). Every attempt is kept.
- **Timing and outages:** outage and streak lengths, the recovery clock, permanent versus transient reasons, qualified hours, slot and response lag, timed-out (late) exits, unqualified layouts, and the full pipeline clock: chain time, recorder receipt, decision, due, start and in-hand.

## Targets (engineering targets, not market constants)

- **Qualified coverage:**
  - eventual: the one-sided 95% lower bound **≥ 95%**;
  - first try: the lower bound **≥ 90%**.
- **Uncertainty:** each bound is reported both ways:
  - a simple binomial;
  - clustered by pool-day, or by outage block where reads are correlated.
- **Zero failures isn't zero probability:** even 0 failures in 200 independent jobs has a one-sided 95% upper bound of about 1.5%, and correlation makes that optimistic.

## After it

- **What it permits:** preliminary availability estimates may update T9-E1's planning.
- **What stays:** the A1–A5 and informative-missingness stresses, until enough regimes (congestion included) have been observed. Availability is modelled by reason, time and state, not as one averaged rate.
- **What it doesn't cover:** the watcher can't estimate transaction landing or failed-transaction fees. Those need owner-authorized attempt records, and the halt isn't lifted to collect them.

## A known blocker (2026-10-07)

In the first 2 hours of v2 records, **50 of 64 entry quotes were prefix-only**: their pools carry bytes past the documented 271-byte layout. Until that layout is documented (`data/research/QUESTION_pumpfun_pool_layout.md`), qualified coverage **can't** reach 95% on these pools, whatever the provider does. The qualification would report that honestly, not reclassify the bytes.
