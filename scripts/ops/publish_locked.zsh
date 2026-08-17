#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
LOCK_FILE="${STATE_ROOT}/run/publish.lock"
LOCK_WAIT_SECONDS="${RADARSAT_PUBLISH_LOCK_WAIT_SECONDS:-300}"

mkdir -p "${STATE_ROOT}/run" "${STATE_ROOT}/state" "${STATE_ROOT}/status"

export PYTHONPATH="${PROJECT_ROOT}"

# A PID-directory lock can suffer an ABA race when several waiters replace a
# stale directory at the same instant: one waiter may remove a newly acquired
# lock belonging to another. macOS lockf holds an OS-level advisory lock for
# the complete child lifetime, recovers automatically on process exit, and
# preserves FIFO ordering with -k.
/usr/bin/lockf -k -t "${LOCK_WAIT_SECONDS}" "${LOCK_FILE}" \
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/publish_r2.py" \
  --root "${OUTPUT_ROOT}" \
  --state-path "${STATE_ROOT}/state/r2-publish.sqlite3" \
  --status-path "${STATE_ROOT}/status/publish.json" \
  "$@"
