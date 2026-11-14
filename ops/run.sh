#!/usr/bin/env bash
# One-command deploy of the serving loop on any Linux/macOS box that stays awake.
#
#   ops/run.sh                    # primary, live RawTree (needs .env with TEAM_ID and RAWTREE_API_KEY)
#   ops/run.sh --standby          # second machine: writes only if the primary's heartbeat is lost
#   ops/run.sh --fake             # local rehearsal against the simulated shop
#   ops/run.sh --smoke            # just check the live API answers our queries, then exit
#
# Creates .venv, installs the package, then runs serve.py under the supervisor (restart on crash,
# kill-and-restart on hang). Logs go to logs/serve-<role>.log and to the terminal.
# Policy and live hooks: RTREC_POLICY=module:factory  RTREC_HOOKS=module:factory[,module:factory]
set -euo pipefail
cd "$(dirname "$0")/.."

ROLE=primary STORE="${RTREC_STORE:-rawtree}" SMOKE=0
for arg in "$@"; do
  case "$arg" in
    --standby) ROLE=standby ;;
    --fake) STORE=fake ;;
    --smoke) SMOKE=1 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip >/dev/null
.venv/bin/python -m pip install -q -e . >/dev/null

if [ "$STORE" = rawtree ]; then
  if [ -f .env ]; then set -a; . ./.env; set +a; fi
  : "${TEAM_ID:?set TEAM_ID in .env (e.g. team_07)}"
  : "${RAWTREE_API_KEY:?set RAWTREE_API_KEY in .env}"
fi

if [ "$SMOKE" = 1 ]; then
  exec .venv/bin/python ops/smoke_rawtree.py
fi

export RTREC_STORE="$STORE" RTREC_ROLE="$ROLE"
export RTREC_STATE_DIR="${RTREC_STATE_DIR:-artifacts/runtime-$ROLE}"
mkdir -p logs "$RTREC_STATE_DIR"
# keep the machine awake while serving, where the tool exists (macOS caffeinate / systemd-inhibit)
WAKE=()
if command -v caffeinate >/dev/null; then WAKE=(caffeinate -dims);
elif command -v systemd-inhibit >/dev/null; then WAKE=(systemd-inhibit --what=sleep:idle --why=rtrec); fi

${WAKE[@]+"${WAKE[@]}"} .venv/bin/python -m rtrec.runtime.supervisor --status-file "$RTREC_STATE_DIR/status.json" -- \
  .venv/bin/python -m rtrec.runtime.serve --store "$STORE" --role "$ROLE" --state-dir "$RTREC_STATE_DIR" \
  2>&1 | tee -a "logs/serve-$ROLE.log"
