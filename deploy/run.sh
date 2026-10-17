#!/usr/bin/env bash
# Orchestrate the full BookStack deploy. Tees every stage to logs/.
#
# Usage:
#   bash deploy/run.sh              # run all stages
#   bash deploy/run.sh --from 05    # resume from 05-instance.sh onward
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env

STAGES=(
  01-preflight.sh
  02-bucket.sh
  03-iam.sh
  04-network.sh
  05-instance.sh
  06-install-bookstack.sh
  07-storage.sh
  08-tls-admin.sh
)

FROM=""
case "${1:-}" in
  --from)
    FROM="${2:-}"
    if [[ -z "$FROM" ]]; then
      echo "Usage: bash deploy/run.sh [--from STAGE]" >&2
      exit 1
    fi
    ;;
  "") ;;
  *)
    echo "Unknown argument: $1" >&2
    echo "Usage: bash deploy/run.sh [--from STAGE]" >&2
    exit 1
    ;;
esac

if [[ -n "$FROM" ]]; then
  matched=0
  for stage in "${STAGES[@]}"; do
    if [[ "$stage" == *"$FROM"* ]]; then
      matched=1
    fi
  done
  if [[ "$matched" -eq 0 ]]; then
    echo "No stage matches '${FROM}'. Available stages:" >&2
    printf '  %s\n' "${STAGES[@]}" >&2
    exit 1
  fi
fi

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"
: > "$LOG_FILE"

log() {
  printf '%s\n' "$*" | tee -a "$LOG_FILE"
}

log "BookStack deploy - $(date -Is)"
log "Log file: ${LOG_FILE}"
log ""

skipping=0
[[ -n "$FROM" ]] && skipping=1

for stage in "${STAGES[@]}"; do
  if [[ "$skipping" == 1 ]]; then
    if [[ "$stage" == *"$FROM"* ]]; then
      skipping=0
    else
      log "--- Skipping ${stage} ---"
      continue
    fi
  fi
  log "=== [$(date +%H:%M:%S)] ${stage} ==="
  # Tee each stage separately and fail fast; a failing stage must abort the run.
  if ! bash "$ROOT/deploy/$stage" 2>&1 | tee -a "$LOG_FILE"; then
    log "Stage ${stage} FAILED. Full log: ${LOG_FILE}"
    exit 1
  fi
  log ""
done

log "BookStack deploy finished - $(date -Is)"
