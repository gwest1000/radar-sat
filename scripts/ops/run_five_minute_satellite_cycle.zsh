#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_DIR="${STATE_ROOT}/run/five-minute-satellite-cycle.lock"
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
      print "Radar-Sat five-minute satellite cycle is already running as PID ${owner_pid}; exiting."
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

if [[ "${RADARSAT_FIVE_MINUTE_BC_SATELLITE_ENABLED:-${RADARSAT_WESTWX_SATELLITE_ENABLED:-0}}" != "1" ]]; then
  exit 0
fi

export PYTHONPATH="${PROJECT_ROOT}"
export MPLCONFIGDIR="${PROJECT_ROOT}/.cache/matplotlib"

if [[ "${RADARSAT_NOAA_STAR_GEOCOLOR_ENABLED:-${RADARSAT_WESTWX_SATELLITE_ENABLED:-0}}" == "1" ]]; then
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/backfill_noaa_star_geocolor.py" \
    --sector pacus \
    --output-root "${OUTPUT_ROOT}" \
    --cache-root "${RADARSAT_NOAA_STAR_GEOCOLOR_CACHE_ROOT:-${PROJECT_ROOT}/var/cache/noaa-star-geocolor}" \
    --hours "${RADARSAT_NOAA_STAR_GEOCOLOR_HOURS:-3}" \
    --max-frames "${RADARSAT_NOAA_STAR_PACUS_MAX_FRAMES:-4}" \
    --max-download-gb "${RADARSAT_NOAA_STAR_PACUS_MAX_DOWNLOAD_GB:-0.15}" \
    --max-source-mb "${RADARSAT_NOAA_STAR_MAX_SOURCE_MB:-100}" \
    --defer-catalog \
    --apply || print -u2 "Warning: NOAA STAR PACUS GeoColor refresh failed; retaining raw NOAA fallback."
fi

"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}"
"${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" --fast --existing-video-only --whole-frame-only --recovery-hours 6
