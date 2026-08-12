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
    bc-southeast-overlay bc-northeast-overlay &
  bc_pid=$!
  run_video_group "${track}" "${north_america_layers}" north-america-overlay &
  north_america_pid=$!
  run_video_group "${track}" "${pacific_layers}" \
    pacific-wna-overlay north-pacific-overlay &
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

publish_video_catalog() {
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/write_catalog.py" --output-root "${OUTPUT_ROOT}"
  "${PROJECT_ROOT}/scripts/ops/publish_locked.zsh" \
    --fast --whole-frame-only --recovery-hours 6
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

# Publish the operational defaults first. The former all-layer live pass held
# this lock for hours, leaving the default H.264 loop stale and forcing clients
# onto the substantially heavier lossless-image fallback.
if ! run_video_phase live \
  "raw-visir,raw-visir-5min" "westwx-visir" "raw-visir"; then
  print -u2 "Default live H.264 refresh failed; retaining last-good profiles."
  exit 1
fi

# Archive video changes only on the hourly timeline. Refresh it at most once
# per hour. The next due time is measured from archive completion.
archive_stamp="${STATE_ROOT}/state/video-archive-completed-epoch"
current_epoch="$(date '+%s')"
previous_archive_epoch=0
[[ -r "${archive_stamp}" ]] && IFS= read -r previous_archive_epoch < "${archive_stamp}"
[[ "${previous_archive_epoch}" == <-> ]] || previous_archive_epoch=0
if (( current_epoch - previous_archive_epoch >= 3600 )); then
  if ! run_video_phase archive \
    "raw-visir,raw-visir-5min" "westwx-visir" "raw-visir"; then
    print -u2 "Warning: default archive H.264 refresh was partial; retaining last-good profiles."
  fi

  # Gate the next archive from completion, not start. An archive crossing an
  # UTC hour boundary must not immediately trigger another archive pass.
  archive_stamp_tmp="${archive_stamp}.tmp.$$"
  mkdir -p "${archive_stamp:h}"
  print -r -- "$(date '+%s')" > "${archive_stamp_tmp}"
  mv -f "${archive_stamp_tmp}" "${archive_stamp}"

  # Always finish archive maintenance with a current live snapshot. This is a
  # cheap no-op when source fingerprints did not advance, and essential when
  # archive encoding held the sole cycle lock across one or more ingests.
  run_video_phase live \
    "raw-visir,raw-visir-5min" "westwx-visir" "raw-visir" \
    || print -u2 "Warning: final default-live catch-up was partial."
fi
