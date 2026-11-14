#!/usr/bin/env bash
# Sunday early-morning (05:00 CET) refit of the production model (runs unattended, does NOT touch the live server).
#   1. snapshot the practice stream (shop.* live tables + our feedback) into data/live/
#   2. append it to the history in history shape (data_final/)
#   3. refit v2 on the most recent window with the validated boosting rounds -> artifacts/ranker_final.txt
# The switch to the new model is a separate step (ops/go_final.py), so a failed refit changes nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
set -a; [ -f .env ] && . ./.env; set +a
PY=.venv/bin/python
{
  echo "== $(date -u +%FT%TZ) final refit start"
  rm -rf data/live   # re-fetch the whole practice stream (refresh_last only refreshes the latest day)
  $PY scripts/download_history.py --out data --live
  rm -f artifacts/ranker_final.ok
  $PY scripts/live_to_history.py --data data --out data_final --holdout-hours 2   # last 2 h set aside
  $PY -u scripts/train_ranker.py --parquet data_final --final --sample 0.3 --ipw-clip "${RTREC_IPW_CLIP:-0}" \
      --out artifacts/ranker_final.tmp.txt
  for f in artifacts/ranker_final.tmp.*; do mv "$f" "${f/ranker_final.tmp/ranker_final}"; done
  # gate: the refit enters the final only if it beats the current model on the untouched 2 h (writes .ok)
  $PY scripts/compare_models.py --data data_final --current "${RTREC_CURRENT_MODEL:-artifacts/ranker_v3.txt}" \
      --candidate artifacts/ranker_final.txt --ok artifacts/ranker_final.ok || echo "refit did NOT pass the gate"
  echo "== $(date -u +%FT%TZ) final refit OK"
} >> logs/final_refit.log 2>&1
