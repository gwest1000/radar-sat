#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
  set -a
  source "${PROJECT_ROOT}/.env"
  set +a
fi

OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"

mkdir -p "${PROJECT_ROOT}/var/status"
export PYTHONPATH="${PROJECT_ROOT}"

exec "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/check_health.py" \
  --root "${OUTPUT_ROOT}" \
  --publish-status "${PROJECT_ROOT}/var/status/publish.json" \
  --status-path "${PROJECT_ROOT}/var/status/health.json"
