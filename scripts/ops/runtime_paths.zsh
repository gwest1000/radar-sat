#!/bin/zsh

# PROJECT_ROOT must be set by the calling operational script.
if [[ -z "${PROJECT_ROOT:-}" ]]; then
  print -u2 "runtime_paths.zsh requires PROJECT_ROOT."
  return 1
fi

MACHINE_DATA_CONFIG="${PROJECT_DATA_CONFIG:-${HOME}/.config/project-data.env}"
if [[ -z "${PROJECT_DATA_ROOT:-}" && -f "${MACHINE_DATA_CONFIG}" ]]; then
  set -a
  source "${MACHINE_DATA_CONFIG}"
  set +a
fi

ENV_FILE="${RADARSAT_ENV_FILE:-${PROJECT_ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

if [[ -n "${RADARSAT_DATA_ROOT:-}" ]]; then
  DATA_ROOT="${RADARSAT_DATA_ROOT:A}"
  DATA_ROOT_SOURCE="RADARSAT_DATA_ROOT"
elif [[ -n "${PROJECT_DATA_ROOT:-}" ]]; then
  SHARED_DATA_ROOT="${PROJECT_DATA_ROOT:A}"
  if [[ ! -d "${SHARED_DATA_ROOT}" ]]; then
    print -u2 "PROJECT_DATA_ROOT is configured but unavailable: ${SHARED_DATA_ROOT}"
    return 1
  fi
  DATA_ROOT="${SHARED_DATA_ROOT}/radar-sat/data"
  DATA_ROOT_SOURCE="PROJECT_DATA_ROOT"
else
  DATA_ROOT="${PROJECT_ROOT}/data"
  DATA_ROOT_SOURCE="local fallback"
fi

OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${DATA_ROOT}/output}"
if [[ "${RADARSAT_CREATE_DATA_ROOT:-0}" == "1" ]]; then
  mkdir -p "${OUTPUT_ROOT}"
elif [[ ! -d "${OUTPUT_ROOT}" ]]; then
  if [[ -n "${RADARSAT_OUTPUT_ROOT:-}" || "${DATA_ROOT_SOURCE}" == "local fallback" ]]; then
    mkdir -p "${OUTPUT_ROOT}"
  else
    print -u2 "${DATA_ROOT_SOURCE} resolved to an unavailable Radar-Sat output directory: ${OUTPUT_ROOT}"
    return 1
  fi
fi

PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [[ -n "${PROJECT_DATA_ROOT:-}" ]]; then
  FCSTGRAPHICS_DATA_ROOT="${FCSTGRAPHICS_DATA_ROOT:-${PROJECT_DATA_ROOT:A}/fcstGraphics/data}"
else
  FCSTGRAPHICS_DATA_ROOT="${FCSTGRAPHICS_DATA_ROOT:-${HOME}/projects/fcstGraphics/data}"
fi
export PROJECT_DATA_ROOT RADARSAT_DATA_ROOT="${DATA_ROOT}" FCSTGRAPHICS_DATA_ROOT OUTPUT_ROOT PYTHON_BIN
