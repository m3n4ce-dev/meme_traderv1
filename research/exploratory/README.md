# Exploratory research scripts (T1–T9)

These are the scripts behind the findings in `docs/EDGE_BRIEF.md` and `research/hypotheses.csv`, as they ran on 2026-10-06 from a scratch directory. They're **development research, not confirmatory tests**: several looks at the same four days, with thresholds picked along the way.

Only three kinds of change were made for the repo:
1. Paths come from `_paths.py`. Recordings are read from `data/`; caches and outputs go to `$MT_EXPLORE_DIR` (default `data/research/exploratory/`, git-ignored).
2. 10-06's recordings are read from the sealed `.gz`. The original runs read the plain file **while it was still being written** (see the cut-offs below).
3. `run_compare.sh` is rewritten from the one-line command that was run.

The originals, byte for byte, with their sha256 and printed outputs, are in the research package (`review_export`) under `research_as_run/` and `research_outputs/`.

**Dependencies:** the repo's environment plus `lightgbm 4.7.0`, `numpy 2.5.3` and `scipy 1.18.1` for the model scripts. Several read `config/params.yaml` (the owner's settings) through `meme_trader.config`.

**How to run:** from the repo root, `python research/exploratory/<script> [args]`. Each model script builds its snapshot cache (`*.pkl`) on first run and reuses it after. `MT_DATA_DIR` points them at another data directory (e.g. from a worktree).

## Map

| Test | Scripts | What they do | Split |
|---|---|---|---|
| T1 | `edge-gbm_vs_logistic.py` | trees vs logistic on the barrier label | train 10-03..05, grade 10-06 |
| T1/T2 | `edge-edge2.py`, `edge-edge3.py`, `edge-edge4.py` | picks scored by a bracket trade after fees; walk-forward | train 10-03..04 → 10-05 (`edge3`, `walkforward.json`); train 10-03..05 → 10-06 (`edge2`, `edge4`) |
| T2 | `edge-exits.py` | which exit makes the picks pay; exits chosen on the first half of 10-06, checked on the second | 10-06 |
| T2 | `edge-delay5.py`, `edge-delay6.py` | picks with fills 0–2.5 s late | grade 10-05 / 10-06 |
| T3 | `edge-latelabel.py` | label counted from the delayed fill price | walk-forward as above |
| T4 | `edge-hodl5.py`, `edge-hodl6.py` | longer holds, wide or no stops | grade 10-05 / 10-06 |
| T5 | `edge-slow.py` | coins 5–30 min old | walk-forward |
| T6 | `edge-style.py DAY`, `edge-style_sum.py`, `edge-manual.py` | the owner's style as 540 rules, run per day (`DAY` = `feed-2026-10-0N.jsonl.gz`); the owner's manual entries vs the bots' | each day |
| T7 | `edge2-grad_collect.py DAY`, then `edge2-grad_eval.py` | which half-full coins graduate | train earlier days, grade a later one |
| T8 | `run_compare.sh` | graduation variants: slippage caps, fill delays, near-high, dev-sell size | 10-03..05 (10-06 not reached) |
| T9 | **`t9_replay_v2.py`** (2026-10-07) | the corrected replay: one series per pool across days, fills and censoring by `meme_trader/sniper/replay_exec.py` (the forward test's rules), statuses for every unmeasured trade, episodes, bounds. Its signal is the shared, versioned `meme_trader/sniper/t9_signal.py` (version 2: the price AT OR BEFORE t-300, as registered; version 1 took the price strictly before it, and its outputs are kept as `signal-v1/`) | 10-04..06 |
| T9 (superseded) | `edge2-swap_study.py [COST]`, `edge2-swap_robust.py COST DELAY`, `edge2-swap_top.py` | the first version. A sixth review found it filling a missing delayed exit at the trigger print, and selling positions that outran a day's series at their last print. `run` is corrected; its old numbers are invalid for executable-return inference | 10-04, 10-05, 10-06 |

## When they ran (and so how much of 10-06 they saw)

Each output's time is in UTC. A script saw 10-06 data **up to at most** that time; 10-03..05 were complete.

- **T1–T5, 18:55–20:23 UTC:** the "unseen 10-06" was its first ~19–20 hours. A rerun on the sealed file sees the whole day and won't match the original numbers exactly.
- **T6, 20:21–20:25 UTC.**
- **T7 collection, 23:29 UTC.**
- **T9, 23:31 UTC:** nearly the whole day.
- **T8, 00:54 UTC on 10-07:** 10-03..05 complete.

To reproduce an original run exactly, cut 10-06 at that time (events with `ts` ≤ the time).

The day's T8 config wasn't saved; today's effective settings are in the package's `config_effective.json`.
