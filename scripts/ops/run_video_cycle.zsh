#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
VIDEO_TRACK="${RADARSAT_VIDEO_TRACK:-live}"
if [[ "${VIDEO_TRACK}" != "live" && "${VIDEO_TRACK}" != "archive" ]]; then
  print -u2 "RADARSAT_VIDEO_TRACK must be live or archive."
  exit 2
fi
LOCK_DIR="${STATE_ROOT}/run/video-${VIDEO_TRACK}-cycle.lock"
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

export PYTHONPATH="${PROJECT_ROOT}"

if [[ "${RADARSAT_VIDEO_ENABLED:-${RADARSAT_H264_PILOT_ENABLED:-0}}" != "1" ]]; then
  exit 0
fi

run_video_group() {
  local track="$1"
  local layer_csv="$2"
  shift 2
  local -a args=(
    --source-root "${OUTPUT_ROOT}"
    --output-root "${OUTPUT_ROOT}"
    --track "${track}"
    --hours "${RADARSAT_VIDEO_LIVE_HOURS:-${RADARSAT_H264_PILOT_HOURS:-24}}"
    --archive-hours "${RADARSAT_VIDEO_ARCHIVE_HOURS:-168}"
    --defer-shared-prune
  )
  local -a layers=("${(@s:,:)layer_csv}")
  local layer=""
  for layer in "${layers[@]}"; do
    args+=(--layer "${layer}")
  done
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
  local bc_layers="$2" north_america_layers="$3" pacific_layers="$4"
  local bc_pid=0 north_america_pid=0 pacific_pid=0 build_status=0
  run_video_group "${track}" "${bc_layers}" \
    bc-large-overlay bc-small-overlay bc-southwest-overlay \
    bc-southeast-overlay bc-northeast-overlay bc-south-coast-overlay &
  bc_pid=$!
  run_video_group "${track}" "${north_america_layers}" north-america-overlay &
  north_america_pid=$!
  run_video_group "${track}" "${pacific_layers}" \
    pacific-wna-overlay north-pacific-overlay &
  pacific_pid=$!
  wait "${bc_pid}" || build_status=1
  wait "${north_america_pid}" || build_status=1
  wait "${pacific_pid}" || build_status=1
  if [[ "${track}" == "live" ]]; then
    "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/build_satellite_video.py" \
      --source-root "${OUTPUT_ROOT}" \
      --output-root "${OUTPUT_ROOT}" \
      --prune-shared-only || build_status=1
  fi
  return "${build_status}"
}

publish_video_catalog() {
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}"
  "${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" \
    --fast --whole-frame-only --recovery-hours 24
}

run_video_phase() {
  local track="$1" bc_layers="$2" north_america_layers="$3" pacific_layers="$4"
  local phase_status=0
  run_parallel_video_groups \
    "${track}" "${bc_layers}" "${north_america_layers}" "${pacific_layers}" \
    || phase_status=1
  publish_video_catalog || phase_status=1
  return "${phase_status}"
}

# Live and archive are deliberately separate launchd jobs and locks. Archive
# maintenance can therefore never delay a newly ingested operational frame.
if ! run_video_phase "${VIDEO_TRACK}" \
  "raw-visir,raw-visir-5min" "westwx-visir" "raw-visir"; then
  print -u2 "Default ${VIDEO_TRACK} H.264 refresh failed; retaining last-good profiles."
  exit 1
fi
