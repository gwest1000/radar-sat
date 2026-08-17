#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
LOCK_DIR="${STATE_ROOT}/run/model-contour-cycle.lock"
LOCK_OWNER="${LOCK_DIR}/pid"

mkdir -p "${STATE_ROOT}/run" "${PROJECT_ROOT}/logs" \
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
      print "Radar-Sat model-contour cycle is already running as PID ${owner_pid}; exiting."
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

acquire_lock || exit 0
trap release_lock EXIT
trap 'release_lock; exit 130' INT
trap 'release_lock; exit 143' TERM

export PYTHONPATH="${PROJECT_ROOT}"
export MPLCONFIGDIR="${PROJECT_ROOT}/.cache/matplotlib"

refresh_status=0
if [[ "${RADARSAT_MODEL_CONTOURS_ENABLED:-${RADARSAT_HRDPS_CONTOURS_ENABLED:-1}}" == "1" ]]; then
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/backfill_model_contours.py" \
    --output-root "${OUTPUT_ROOT}" \
    --hrdps-root "${RADARSAT_HRDPS_DATA_ROOT:-${FCSTGRAPHICS_DATA_ROOT}/hrdps_continental}" \
    --ecmwf-root "${RADARSAT_ECMWF_DATA_ROOT:-/Volumes/Greg1_2tb/concrete_fcst_data/raw/ecmwf/realtime}" \
    --hours "${RADARSAT_MODEL_CONTOUR_RECOVERY_HOURS:-${RADARSAT_HRDPS_CONTOUR_RECOVERY_HOURS:-0}}" \
    || refresh_status=$?
fi

# Publish any successfully refreshed model even if the other source failed.
publish_status=0
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}" \
  || publish_status=$?
if (( publish_status == 0 )); then
  RADARSAT_PUBLISH_LOCK_WAIT_SECONDS="${RADARSAT_MODEL_PUBLISH_LOCK_WAIT_SECONDS:-900}" \
    "${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" \
    --fast --existing-video-only --whole-frame-only --recovery-hours 24 \
    || publish_status=$?
fi
if (( publish_status != 0 )); then
  exit "${publish_status}"
fi
exit "${refresh_status}"
