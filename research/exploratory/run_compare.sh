#!/bin/sh
# T8 (2026-10-06): graduation-play variants over the recorded days, rules only (no AI vote; the daily loss limit
# and drawdown stop off so a halted side doesn't skew the comparison). As run: commit d7f0429 plus a small patch
# (provenance/t8-compare-wt-cr.* in the research package; the devsell variants need it). The original run named
# 10-06's file as .jsonl and stopped when the recorder compressed it; this reads the sealed .gz.
cd "$(dirname "$0")/../.." || exit 1
PY=${PY:-.venv/bin/python}
PYTHONPATH=. nice -n 12 "$PY" -m meme_trader.sniper compare --jobs 3 --config "${MT_CONFIG:-config/params.yaml}" \
  --set entry.enabled=false --set desk.enabled=false --set xchain.enabled=false --set lab.enabled=false \
  --set capital.daily_loss_limit_sol=1000 --set capital.max_drawdown_pct=100 \
  --file data/feed-2026-10-03.jsonl.gz data/feed-2026-10-04.jsonl.gz data/feed-2026-10-05.jsonl.gz \
         data/feed-2026-10-06.jsonl.gz \
  --variant "cap3: execution.slippage_pct=3" --variant "cap5: execution.slippage_pct=5" \
  --variant "cap8: execution.slippage_pct=8" \
  --variant "fill1.5s: execution.paper_delay_s=1.5" --variant "fill1.0s: execution.paper_delay_s=1.0" \
  --variant "fill0.5s: execution.paper_delay_s=0.5" \
  --variant "nearhigh95: late.min_near_high=0.95" \
  --variant "devsell2pct: late.dev_sell_min_pct=2" --variant "devsell_never: late.dev_sell_min_pct=100"
