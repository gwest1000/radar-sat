#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
LOCK_DIR="${STATE_ROOT}/run/publish.lock"
LOCK_OWNER="${LOCK_DIR}/pid"

mkdir -p "${STATE_ROOT}/run" "${STATE_ROOT}/state" "${STATE_ROOT}/status"

ENV_FILE="${RADARSAT_ENV_FILE:-${PROJECT_ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi
export PYTHONPATH="${PROJECT_ROOT}"

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
      if (( attempts >= 300 )); then
        print -u2 "Timed out waiting for R2 publication lock owned by PID ${owner_pid}."
        return 1
      fi
      sleep 1
      (( attempts += 1 ))
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

acquire_lock
trap release_lock EXIT
trap 'release_lock; exit 130' INT
trap 'release_lock; exit 143' TERM

"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/publish_r2.py" \
  --root "${OUTPUT_ROOT}" \
  --state-path "${STATE_ROOT}/state/r2-publish.sqlite3" \
  --status-path "${STATE_ROOT}/status/publish.json" \
  "$@"
