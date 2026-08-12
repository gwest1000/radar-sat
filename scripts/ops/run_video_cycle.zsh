#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_DIR="${STATE_ROOT}/run/video-cycle.lock"
LOCK_OWNER="${LOCK_DIR}/pid"

mkdir -p "${STATE_ROOT}/run" "${STATE_ROOT}/status" "${PROJECT_ROOT}/logs"

release_lock() {
  local owner_pid=""
  [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
  if [[ "${owner_pid}" == "$$" ]]; then
    /bin/rm -f "${LOCK_OWNER}"
    rmdir "${LOCK_DIR}" 2>/dev/null || true
  fi
}

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  owner_pid=""
  [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
  if [[ "${owner_pid}" =~ '^[0-9]+$' ]] && kill -0 "${owner_pid}" 2>/dev/null; then
    print "Radar-Sat video cycle is already running as PID ${owner_pid}; exiting."
    exit 0
  fi
  /bin/rm -f "${LOCK_OWNER}"
  rmdir "${LOCK_DIR}" 2>/dev/null || true
  mkdir "${LOCK_DIR}"
fi
print -r -- "$$" > "${LOCK_OWNER}"
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

if [[ "${RADARSAT_VIDEO_ENABLED:-${RADARSAT_H264_PILOT_ENABLED:-0}}" != "1" ]]; then
  exit 0
fi

run_video_group() {
  local track="$1"
  shift
  local -a args=(
    --source-root "${OUTPUT_ROOT}"
    --output-root "${OUTPUT_ROOT}"
    --track "${track}"
    --hours "${RADARSAT_VIDEO_LIVE_HOURS:-${RADARSAT_H264_PILOT_HOURS:-24}}"
    --archive-hours "${RADARSAT_VIDEO_ARCHIVE_HOURS:-168}"
    --defer-shared-prune
  )
  local product=""
  for product in "$@"; do
    args+=(--product "${product}")
  done
  if [[ -n "${RADARSAT_FFMPEG:-}" ]]; then
    args+=(--ffmpeg "${RADARSAT_FFMPEG}")
  fi
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/build_satellite_video.py" "${args[@]}"
}

run_parallel_video_groups() {
  local track="$1"
  local bc_pid=0 north_america_pid=0 pacific_pid=0 build_status=0
  run_video_group "${track}" \
    bc-large-overlay bc-small-overlay bc-southwest-overlay \
    bc-southeast-overlay bc-northeast-overlay &
  bc_pid=$!
  run_video_group "${track}" north-america-overlay &
  north_america_pid=$!
  run_video_group "${track}" pacific-wna-overlay north-pacific-overlay &
  pacific_pid=$!
  wait "${bc_pid}" || build_status=1
  wait "${north_america_pid}" || build_status=1
  wait "${pacific_pid}" || build_status=1
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/build_satellite_video.py" \
    --source-root "${OUTPUT_ROOT}" \
    --output-root "${OUTPUT_ROOT}" \
    --prune-shared-only || build_status=1
  return "${build_status}"
}

run_parallel_video_groups live \
  || print -u2 "Warning: live H.264 refresh was partial; retaining last-good profiles."
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}"
"${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" --fast --whole-frame-only --recovery-hours 6

# Archive video changes only on the hourly timeline. Refresh it at most once
# per hour and publish the completed pointers in a separate atomic catalog.
archive_stamp="${STATE_ROOT}/state/video-archive-hour"
current_hour="$(date -u '+%Y%m%d%H')"
previous_hour=""
[[ -r "${archive_stamp}" ]] && IFS= read -r previous_hour < "${archive_stamp}"
if [[ "${current_hour}" != "${previous_hour}" ]]; then
  # Record the attempt before encoding. A partial profile set is expected when
  # an upstream product is absent and must not trigger another seven-day pass
  # on every ten-minute launch.
  mkdir -p "${archive_stamp:h}"
  print -r -- "${current_hour}" > "${archive_stamp}"
  if ! run_parallel_video_groups archive; then
    print -u2 "Warning: archive H.264 refresh was partial; retaining last-good profiles."
  fi
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}"
  "${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" --fast --whole-frame-only --recovery-hours 6
fi
