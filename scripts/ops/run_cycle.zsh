#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
LOCK_DIR="${STATE_ROOT}/run/cycle.lock"
LOCK_OWNER="${LOCK_DIR}/pid"

mkdir -p "${STATE_ROOT}/run" "${STATE_ROOT}/status" "${PROJECT_ROOT}/logs" \
  "${PROJECT_ROOT}/.cache/matplotlib"

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
      print "Radar-Sat cycle is already running as PID ${owner_pid}; exiting without overlap."
      return 1
    fi
    # A process that just won mkdir needs a brief window to publish its PID.
    if [[ -z "${owner_pid}" && "${attempts}" -eq 0 ]]; then
      sleep 1
      attempts=1
      continue
    fi
    stale_dir="${LOCK_DIR}.stale.$$"
    if mv "${LOCK_DIR}" "${stale_dir}" 2>/dev/null; then
      /bin/rm -f "${stale_dir}/pid"
      rmdir "${stale_dir}" 2>/dev/null || \
        print -u2 "Warning: isolated stale lock has unexpected contents: ${stale_dir}"
      attempts=0
    fi
  done
  print -r -- "$$" > "${LOCK_OWNER}"
  return 0
}

if ! acquire_lock; then
  exit 0
fi
trap release_lock EXIT
trap 'release_lock; exit 130' INT
trap 'release_lock; exit 143' TERM

ENV_FILE="${RADARSAT_ENV_FILE:-${PROJECT_ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

OUTPUT_ROOT="${RADARSAT_OUTPUT_ROOT:-${PROJECT_ROOT}/data/output}"
PYTHON_BIN="${RADARSAT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  print -u2 "Missing Radar-Sat Python runtime: ${PYTHON_BIN}"
  exit 1
fi

export PYTHONPATH="${PROJECT_ROOT}"
export MPLCONFIGDIR="${PROJECT_ROOT}/.cache/matplotlib"

# This high-bandwidth rapid GOES path has a dedicated cache and a small
# bounded catch-up allowance so a long radar/Pacific cycle cannot leave holes
# in the ten-minute satellite clock. A failure is isolated so Forecast
# Graphics ingest/publication can continue; the command writes its own
# detailed status file.
live_satellite_refresh=0
if [[ "${RADARSAT_WESTWX_SATELLITE_ENABLED:-0}" == "1" ]]; then
  live_satellite_refresh=1
  if ! "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/backfill_westwx_satellite.py" \
    --output-root "${OUTPUT_ROOT}" \
    --cache-root "${RADARSAT_WESTWX_SATELLITE_CACHE_ROOT:-${PROJECT_ROOT}/var/cache/westwx-satellite}" \
    --hours "${RADARSAT_WESTWX_SATELLITE_HOURS:-3}" \
    --max-frames "${RADARSAT_WESTWX_SATELLITE_MAX_FRAMES:-2}" \
    --max-download-gb "${RADARSAT_WESTWX_SATELLITE_MAX_DOWNLOAD_GB:-0.8}" \
    --max-source-mb "${RADARSAT_WESTWX_SATELLITE_MAX_SOURCE_MB:-400}" \
    --apply; then
    print -u2 "Warning: isolated WestWX ten-minute satellite catch-up failed; continuing normal cycle."
  fi
fi

# A daylight-only higher-resolution BC composite is isolated from the primary
# ten-minute feed. Each ~0.61 GB source set is deleted after one compact WebP is
# installed, and the final layer has a strict 24-hour retention policy.
if [[ "${RADARSAT_NATIVE_BC_SATELLITE_ENABLED:-${RADARSAT_WESTWX_SATELLITE_ENABLED:-0}}" == "1" ]]; then
  live_satellite_refresh=1
  if ! "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/backfill_native_bc_satellite.py" \
    --output-root "${OUTPUT_ROOT}" \
    --cache-root "${RADARSAT_NATIVE_BC_SATELLITE_CACHE_ROOT:-${PROJECT_ROOT}/var/cache/native-bc-satellite}" \
    --hours "${RADARSAT_NATIVE_BC_SATELLITE_HOURS:-3}" \
    --max-frames "${RADARSAT_NATIVE_BC_SATELLITE_MAX_FRAMES:-1}" \
    --max-download-gb "${RADARSAT_NATIVE_BC_SATELLITE_MAX_DOWNLOAD_GB:-0.7}" \
    --max-source-mb "${RADARSAT_NATIVE_BC_SATELLITE_MAX_SOURCE_MB:-700}" \
    --apply; then
    print -u2 "Warning: isolated native-resolution BC satellite catch-up failed; continuing normal cycle."
  fi
fi

# Publish the live satellite edge before the larger half-hour Pacific render.
# That legacy path can take several minutes under memory pressure and must not
# hold the default ten-minute BC view behind it.
if (( live_satellite_refresh == 1 )); then
  if ! "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/publish_r2.py" \
    --root "${OUTPUT_ROOT}" \
    --state-path "${STATE_ROOT}/state/r2-publish.sqlite3" \
    --status-path "${STATE_ROOT}/status/publish.json"; then
    print -u2 "Warning: early live-satellite R2 publication failed; continuing the primary cycle."
  fi
fi

primary_ingest_status=0
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/run_ingest.py" \
  --output-root "${OUTPUT_ROOT}" \
  --domain bc \
  --domain north-america \
  --domain north-pacific \
  --hours "${RADARSAT_INGEST_HOURS:-3}" \
  --spool-root "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
  --spool-mode "${RADARSAT_SPOOL_MODE:-auto}" \
  --spool-hours "${RADARSAT_SPOOL_INGEST_HOURS:-12}" || primary_ingest_status=$?

if (( primary_ingest_status != 0 )); then
  print -u2 "Warning: primary Radar-Sat ingest failed with status ${primary_ingest_status}; continuing isolated recovery and publication steps."
fi

# Raw staging objects may be discarded only when the primary renderer has
# successfully consumed and classified them. A failed ingest keeps the entire
# recovery window intact for the next cycle.
if (( primary_ingest_status == 0 )); then
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/prune_eccc_spool.py" \
    --spool "${RADARSAT_SPOOL_ROOT:-${HOME}/.local/share/radar-sat/spool/eccc}" \
    --older-than-hours "${RADARSAT_RAW_RETENTION_HOURS:-3}" \
    --ingest-status "${OUTPUT_ROOT}/status/ingest.json" \
    --apply
else
  print -u2 "Warning: skipping raw spool prune because primary ingest did not complete."
fi

"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/publish_r2.py" \
  --root "${OUTPUT_ROOT}" \
  --state-path "${STATE_ROOT}/state/r2-publish.sqlite3" \
  --status-path "${STATE_ROOT}/status/publish.json"

if (( primary_ingest_status != 0 )); then
  exit "${primary_ingest_status}"
fi
