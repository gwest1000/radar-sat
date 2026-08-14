#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
LOCK_DIR="${STATE_ROOT}/run/live-edge-publish.lock"
LOCK_OWNER="${LOCK_DIR}/pid"
mkdir -p "${STATE_ROOT}/run"

release_lock() {
  local owner_pid=""
  [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
  if [[ "${owner_pid}" == "$$" ]]; then
    /bin/rm -f "${LOCK_OWNER}"
    rmdir "${LOCK_DIR}" 2>/dev/null || true
  fi
}

waited=0
while ! mkdir "${LOCK_DIR}" 2>/dev/null; do
  owner_pid=""
  [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
  stale_lock=0
  if [[ -n "${owner_pid}" && ! "${owner_pid}" =~ '^[0-9]+$' ]]; then
    stale_lock=1
  elif [[ "${owner_pid}" =~ '^[0-9]+$' ]] && ! kill -0 "${owner_pid}" 2>/dev/null; then
    stale_lock=1
  elif [[ -z "${owner_pid}" ]] && (( waited >= 1 )); then
    stale_lock=1
  fi
  if (( stale_lock )); then
    stale_dir="${LOCK_DIR}.stale.$$"
    if mv "${LOCK_DIR}" "${stale_dir}" 2>/dev/null; then
      /bin/rm -f "${stale_dir}/pid"
      rmdir "${stale_dir}" 2>/dev/null || true
      continue
    fi
  fi
  if (( waited >= 30 )); then
    print -u2 "Live-edge publisher remained busy; the next short cycle will retry."
    exit 0
  fi
  sleep 1
  (( waited += 1 ))
done
print -r -- "$$" > "${LOCK_OWNER}"
trap release_lock EXIT INT TERM
export PYTHONPATH="${PROJECT_ROOT}"
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/publish_live_edge.py" \
  --root "${OUTPUT_ROOT}" \
  --state-path "${STATE_ROOT}/state/live-edge-upload-state.json"
