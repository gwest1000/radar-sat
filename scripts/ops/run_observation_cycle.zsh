#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_DIR="${STATE_ROOT}/run/observation-cycle.lock"
LOCK_OWNER="${LOCK_DIR}/pid"

mkdir -p "${STATE_ROOT}/run" "${STATE_ROOT}/status" "${PROJECT_ROOT}/logs" \
  "${PROJECT_ROOT}/.cache/matplotlib"

release_lock() {
  local owner_pid=""
  [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
  if [[ "${owner_pid}" == "$$" ]]; then
    /bin/rm -f "${LOCK_OWNER}"
    rmdir "${LOCK_DIR}" 2>/dev/null || true
  fi
}

acquire_lock() {
  local owner_pid="" attempts=0 stale_dir=""
  while ! mkdir "${LOCK_DIR}" 2>/dev/null; do
    owner_pid=""
    [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
    if [[ "${owner_pid}" =~ '^[0-9]+$' ]] && kill -0 "${owner_pid}" 2>/dev/null; then
      print "Radar-Sat observation cycle is already running as PID ${owner_pid}; exiting."
      return 1
    fi
    if [[ -z "${owner_pid}" && "${attempts}" -eq 0 ]]; then
      sleep 1
      attempts=1
      continue
    fi
    stale_dir="${LOCK_DIR}.stale.$$"
    if mv "${LOCK_DIR}" "${stale_dir}" 2>/dev/null; then
      /bin/rm -f "${stale_dir}/pid"
      rmdir "${stale_dir}" 2>/dev/null || true
      attempts=0
    fi
  done
  print -r -- "$$" > "${LOCK_OWNER}"
}

if ! acquire_lock; then
  exit 0
fi
trap release_lock EXIT
trap 'release_lock; exit 130' INT
trap 'release_lock; exit 143' TERM

ENV_FILE="${RADARSAT_ENV_FILE:-${PROJECT_ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

export PYTHONPATH="${PROJECT_ROOT}"
export MPLCONFIGDIR="${PROJECT_ROOT}/.cache/matplotlib"
export RADARSAT_RAW_SAT_ENABLED=0

ingest_status=0
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/run_ingest.py" \
  --output-root "${OUTPUT_ROOT}" \
  --domain bc \
  --domain north-america \
  --domain north-pacific \
  --hours "${RADARSAT_INGEST_HOURS:-3}" \
  --spool-root "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
  --spool-mode only \
  --spool-hours "${RADARSAT_SPOOL_INGEST_HOURS:-12}" || ingest_status=$?

if (( ingest_status == 0 )); then
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/prune_eccc_spool.py" \
    --spool "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
    --older-than-hours "${RADARSAT_RAW_RETENTION_HOURS:-3}" \
    --ingest-status "${OUTPUT_ROOT}/status/ingest.json" \
    --apply
else
  print -u2 "Warning: observation ingest failed with status ${ingest_status}; raw spool retained."
fi

"${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" --fast
exit "${ingest_status}"
