#!/usr/bin/env bash
# Arms the two unattended Sunday steps as detached sleepers (survive closing the terminal):
#   05:00 CET  ops/final_refit.sh  (practice stream until 04:00 UTC; last 2 h held out; gate vs current model)
#   06:30 CET  ops/go_final.py     (pre-registered A/B window: AB_SINCE -> 04:00 UTC; switch to one model)
# Settings live in ops/final.env (current model, IPW clip, A/B window). Times are this machine's local time.
# Check:  pgrep -fl schedule_sunday_job      Cancel:  pkill -f schedule_sunday_job
cd "$(dirname "$0")/.."
set -a; . ops/final.env; set +a
at() {  # at "YYYY-MM-DD HH:MM" cmd...
  local t; t=$(date -j -f "%Y-%m-%d %H:%M" "$1" +%s); shift
  nohup bash -c "exec -a schedule_sunday_job sleep $(( t - $(date +%s) )); set -a; . ops/final.env; set +a; $*" \
    >/dev/null 2>&1 &
  disown
}
at "2026-11-15 05:00" ops/final_refit.sh
at "2026-11-15 06:30" '.venv/bin/python ops/go_final.py --ab-since "$AB_SINCE" --ab-until "$AB_UNTIL" >> logs/go_final.log 2>&1'
echo "armed: $(pgrep -f schedule_sunday_job | wc -l | tr -d ' ') jobs (refit 05:00, switch 06:30 local)"
