#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_DIR="${STATE_ROOT}/run/archive-cycle.lock"
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
      print "Radar-Sat archive cycle is already running as PID ${owner_pid}; exiting."
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

source "${PROJECT_ROOT}/scripts/ops/heavy_satellite_lock.zsh"
release_all_locks() {
  release_heavy_satellite_lock
  release_lock
}
trap release_all_locks EXIT
trap 'release_all_locks; exit 130' INT
trap 'release_all_locks; exit 143' TERM

ENV_FILE="${RADARSAT_ENV_FILE:-${PROJECT_ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

export PYTHONPATH="${PROJECT_ROOT}"
export MPLCONFIGDIR="${PROJECT_ROOT}/.cache/matplotlib"
export RADARSAT_RAW_SAT_ENABLED=1
export RADARSAT_GOES_HAZARDS_ENABLED=0

# The workers normally launch together after login. Let the latency-sensitive
# rapid path claim the shared Satpy slot first, then wait for a quiet interval.
sleep "${RADARSAT_ARCHIVE_START_DELAY_SECONDS:-30}"
archive_wait_seconds=0
until try_acquire_heavy_satellite_lock; do
  if (( archive_wait_seconds >= 150 )); then
    print "Rapid satellite work stayed busy; deferring this archive cycle."
    exit 0
  fi
  sleep 5
  (( archive_wait_seconds += 5 ))
done

# The slow multi-satellite blend is needed only for the half-hour North Pacific
# background. North America and BC are served by the independent rapid paths.
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/run_ingest.py" \
  --output-root "${OUTPUT_ROOT}" \
  --domain north-pacific \
  --hours "${RADARSAT_INGEST_HOURS:-3}" \
  --latest-only \
  --spool-root "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
  --spool-mode off \
  --spool-hours "${RADARSAT_SPOOL_INGEST_HOURS:-12}"

"${PROJECT_ROOT}/scripts/ops/publish_locked.zsh"
