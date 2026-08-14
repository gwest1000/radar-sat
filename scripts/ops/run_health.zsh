#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"

mkdir -p "${PROJECT_ROOT}/var/status"
export PYTHONPATH="${PROJECT_ROOT}"

exec "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/check_health.py" \
  --root "${OUTPUT_ROOT}" \
  --publish-status "${PROJECT_ROOT}/var/status/publish.json" \
  --status-path "${PROJECT_ROOT}/var/status/health.json"
