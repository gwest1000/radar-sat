#!/bin/zsh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
LOCK_DIR="${STATE_ROOT}/run/msc-satellite-edge-cycle.lock"
LOCK_OWNER="${LOCK_DIR}/pid"
mkdir -p "${STATE_ROOT}/run" "${PROJECT_ROOT}/logs"

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
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/refresh_msc_satellite_edge.py" \
  --output-root "${OUTPUT_ROOT}" \
  --spool-root "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}"
"${PROJECT_ROOT}/scripts/ops/live_edge_publish.zsh"
