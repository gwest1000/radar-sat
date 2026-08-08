#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_DIR="${STATE_ROOT}/run/satellite-cycle.lock"
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
      print "Radar-Sat satellite cycle is already running as PID ${owner_pid}; exiting."
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

if ! try_acquire_heavy_satellite_lock; then
  print "A low-priority satellite render is already active; deferring this rapid cycle."
  exit 0
fi

if [[ "${RADARSAT_NOAA_STAR_GEOCOLOR_ENABLED:-${RADARSAT_WESTWX_SATELLITE_ENABLED:-0}}" == "1" ]]; then
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/backfill_noaa_star_geocolor.py" \
    --sector full-disk \
    --output-root "${OUTPUT_ROOT}" \
    --cache-root "${RADARSAT_NOAA_STAR_GEOCOLOR_CACHE_ROOT:-${PROJECT_ROOT}/var/cache/noaa-star-geocolor}" \
    --hours "${RADARSAT_NOAA_STAR_GEOCOLOR_HOURS:-3}" \
    --max-frames "${RADARSAT_NOAA_STAR_FULL_DISK_MAX_FRAMES:-1}" \
    --max-download-gb "${RADARSAT_NOAA_STAR_FULL_DISK_MAX_DOWNLOAD_GB:-0.1}" \
    --max-source-mb "${RADARSAT_NOAA_STAR_MAX_SOURCE_MB:-100}" \
    --defer-catalog \
    --apply || print -u2 "Warning: NOAA STAR full-disk GeoColor refresh failed; retaining raw NOAA fallback."
fi

if [[ "${RADARSAT_WESTWX_SATELLITE_ENABLED:-0}" == "1" ]]; then
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/backfill_westwx_satellite.py" \
    --output-root "${OUTPUT_ROOT}" \
    --cache-root "${RADARSAT_WESTWX_SATELLITE_CACHE_ROOT:-${PROJECT_ROOT}/var/cache/westwx-satellite}" \
    --hours "${RADARSAT_WESTWX_SATELLITE_HOURS:-3}" \
    --max-frames "${RADARSAT_WESTWX_SATELLITE_MAX_FRAMES:-1}" \
    --max-download-gb "${RADARSAT_WESTWX_SATELLITE_MAX_DOWNLOAD_GB:-0.8}" \
    --max-source-mb "${RADARSAT_WESTWX_SATELLITE_MAX_SOURCE_MB:-400}" \
    --defer-catalog \
    --apply || print -u2 "Warning: WestWX satellite refresh failed; continuing to publication."
fi

if [[ "${RADARSAT_VIDEO_ENABLED:-${RADARSAT_H264_PILOT_ENABLED:-0}}" == "1" ]]; then
  video_args=(
    --source-root "${OUTPUT_ROOT}"
    --output-root "${OUTPUT_ROOT}"
    --hours "${RADARSAT_VIDEO_LIVE_HOURS:-${RADARSAT_H264_PILOT_HOURS:-24}}"
    --archive-hours "${RADARSAT_VIDEO_ARCHIVE_HOURS:-168}"
  )
  if [[ -n "${RADARSAT_FFMPEG:-}" ]]; then
    video_args+=(--ffmpeg "${RADARSAT_FFMPEG}")
  fi
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/build_satellite_video.py" \
    "${video_args[@]}" \
    || print -u2 "Warning: H.264 loop refresh failed; retaining previous video generations and image fallbacks."
fi

"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}"
release_heavy_satellite_lock
"${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" --fast --whole-frame-only --recovery-hours 6
