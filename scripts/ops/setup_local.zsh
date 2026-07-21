#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  typeset -a python_candidates
  [[ -n "${RADARSAT_BOOTSTRAP_PYTHON:-}" ]] && \
    python_candidates+=("${RADARSAT_BOOTSTRAP_PYTHON}")
  python_candidates+=(
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    python3.12
    python3.11
    python3
  )
  bootstrap_python=""
  for candidate in "${python_candidates[@]}"; do
    resolved=""
    if [[ "${candidate}" == /* ]]; then
      [[ -x "${candidate}" ]] && resolved="${candidate}"
    else
      resolved="$(command -v "${candidate}" 2>/dev/null || true)"
    fi
    if [[ -n "${resolved}" ]] && \
      "${resolved}" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      bootstrap_python="${resolved}"
      break
    fi
  done
  if [[ -z "${bootstrap_python}" ]]; then
    print -u2 "Radar-Sat requires Python 3.11 or newer; install Homebrew Python 3.12 or set RADARSAT_BOOTSTRAP_PYTHON."
    exit 1
  fi
  print "Creating Radar-Sat virtual environment with ${bootstrap_python}"
  "${bootstrap_python}" -m venv "${PROJECT_ROOT}/.venv"
fi
if ! "${PROJECT_ROOT}/.venv/bin/python" -c \
  'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  print -u2 "Existing .venv uses Python older than 3.11; recreate it with a supported interpreter."
  exit 1
fi
"${PROJECT_ROOT}/.venv/bin/python" -m pip install --upgrade pip
"${PROJECT_ROOT}/.venv/bin/python" -m pip install \
  -r "${PROJECT_ROOT}/requirements.txt" \
  -r "${PROJECT_ROOT}/requirements-feeds.txt"
mkdir -p "${PROJECT_ROOT}/data/output" "${PROJECT_ROOT}/logs" \
  "${PROJECT_ROOT}/var/state" "${PROJECT_ROOT}/var/status" \
  "${PROJECT_ROOT}/.cache/matplotlib"

print "Radar-Sat local runtime is ready. Configure RADARSAT_R2_* or Keychain credentials next."
