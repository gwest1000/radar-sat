#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
LOCK_DIR="${STATE_ROOT}/run/cycle.lock"
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
      print "Radar-Sat cycle is already running as PID ${owner_pid}; exiting without overlap."
      return 1
    fi
    # A process that just won mkdir needs a brief window to publish its PID.
    if [[ -z "${owner_pid}" && "${attempts}" -eq 0 ]]; then
      sleep 1
      attempts=1
      continue
    fi
    stale_dir="${LOCK_DIR}.stale.$$"
    if mv "${LOCK_DIR}" "${stale_dir}" 2>/dev/null; then
      /bin/rm -f "${stale_dir}/pid"
      rmdir "${stale_dir}" 2>/dev/null || \
        print -u2 "Warning: isolated stale lock has unexpected contents: ${stale_dir}"
      attempts=0
    fi
  done
  print -r -- "$$" > "${LOCK_OWNER}"
  return 0
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

OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  print -u2 "Missing Radar-Sat Python runtime: ${PYTHON_BIN}"
  exit 1
fi

export PYTHONPATH="${PROJECT_ROOT}"
export MPLCONFIGDIR="${PROJECT_ROOT}/.cache/matplotlib"

"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/run_ingest.py" \
  --output-root "${OUTPUT_ROOT}" \
  --domain bc \
  --domain north-america \
  --domain north-pacific \
  --hours "${RADARSAT_INGEST_HOURS:-3}" \
  --spool-root "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
  --spool-mode "${RADARSAT_SPOOL_MODE:-auto}" \
  --spool-hours "${RADARSAT_SPOOL_INGEST_HOURS:-12}"

# The renderer is read-only and has completed successfully at this point, so
# raw staging objects older than the recovery window can now be discarded.
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/prune_eccc_spool.py" \
  --spool "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
  --older-than-hours "${RADARSAT_RAW_RETENTION_HOURS:-3}" \
  --ingest-status "${OUTPUT_ROOT}/status/ingest.json" \
  --apply

"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/publish_r2.py" \
  --root "${OUTPUT_ROOT}" \
  --state-path "${STATE_ROOT}/state/r2-publish.sqlite3" \
  --status-path "${STATE_ROOT}/status/publish.json"
