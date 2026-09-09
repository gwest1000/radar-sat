#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"

STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
REQUEST_DIR="${STATE_ROOT}/state/full-publish-requests"
LOCK_FILE="${STATE_ROOT}/run/full-publisher.lock"
PUBLISHER="${RADARSAT_FULL_PUBLISH_DRIVER:-${PROJECT_ROOT}/scripts/ops/publish_locked.zsh}"
MAX_DRAIN_PASSES="${RADARSAT_FULL_PUBLISH_MAX_DRAIN_PASSES:-2}"
MAX_RUNTIME_SECONDS="${RADARSAT_FULL_PUBLISH_MAX_RUNTIME_SECONDS:-300}"
MAINTENANCE_BACKOFF_SECONDS="${RADARSAT_FULL_PUBLISH_MAINTENANCE_BACKOFF_SECONDS:-300}"
MAINTENANCE_RETRY_FILE="${STATE_ROOT}/state/full-publish-maintenance-retry"

mkdir -p "${REQUEST_DIR}" "${STATE_ROOT}/run" "${PROJECT_ROOT}/logs"

if [[ "${1:-}" != "--locked" ]]; then
  # launchd does not overlap one job label, while this advisory lock also
  # protects manual invocations and tests. A busy worker already owns every
  # pending request, so another invocation can return successfully.
  lock_result=0
  /usr/bin/lockf -t 0 "${LOCK_FILE}" "$0" --locked || lock_result=$?
  # macOS lockf uses EX_TEMPFAIL (75) when another worker owns the lock.
  (( lock_result == 75 )) && exit 0
  exit "${lock_result}"
fi

if [[ ! "${MAX_DRAIN_PASSES}" =~ '^[1-9][0-9]*$' ]] || (( MAX_DRAIN_PASSES < 2 )); then
  print -u2 "RADARSAT_FULL_PUBLISH_MAX_DRAIN_PASSES must be at least 2 (fresh publication, then maintenance)."
  exit 2
fi
if [[ ! "${MAX_RUNTIME_SECONDS}" =~ '^[1-9][0-9]*$' ]] \
  || [[ ! "${MAINTENANCE_BACKOFF_SECONDS}" =~ '^[0-9]+$' ]]; then
  print -u2 "Full publisher runtime/backoff must be whole seconds."
  exit 2
fi

bounded_publish() {
  "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/run_bounded.py" \
    --seconds "${MAX_RUNTIME_SECONDS}" --label "${selected_profile} publication" \
    -- "${PUBLISHER}" "$@"
}

publish_profile() {
  local profile="$1"
  case "${profile}" in
    fast-existing)
      bounded_publish \
        --fast --existing-video-only --whole-frame-only --recovery-hours 24
      ;;
    fast-video)
      bounded_publish --fast --whole-frame-only --recovery-hours 24
      ;;
    reconcile)
      bounded_publish --whole-frame-only --recovery-hours 24
      ;;
    full)
      bounded_publish
      ;;
  esac
}

pass=0
fresh_commit=0
while (( pass < MAX_DRAIN_PASSES )); do
  request_files=("${REQUEST_DIR}"/*.request(N))
  (( ${#request_files} )) || exit 0
  observed_requests=("${request_files[@]}")

  selected_profile="fast-existing"
  maintenance_profile=""
  fast_requests=()
  maintenance_requests=()
  for request_file in "${request_files[@]}"; do
    request_profile="${request_file:t:r:e}"
    case "${request_profile}" in
      full)
        maintenance_profile="full"
        maintenance_requests+=("${request_file}")
        ;;
      reconcile)
        [[ "${maintenance_profile}" == "full" ]] \
          || maintenance_profile="reconcile"
        maintenance_requests+=("${request_file}")
        ;;
      fast-video)
        selected_profile="fast-video"
        fast_requests+=("${request_file}")
        ;;
      fast-existing)
        fast_requests+=("${request_file}")
        ;;
    esac
  done

  maintenance_retry=0
  [[ -r "${MAINTENANCE_RETRY_FILE}" ]] \
    && IFS= read -r maintenance_retry < "${MAINTENANCE_RETRY_FILE}"
  [[ "${maintenance_retry}" =~ '^[0-9]+$' ]] || maintenance_retry=0
  if [[ -n "${maintenance_profile}" ]] && (( fresh_commit )); then
    # Every maintenance pass follows a current fast commit. New fast requests
    # are covered by successful reconciliation, so maintenance is not starved.
    (( $(date +%s) >= maintenance_retry )) || exit 0
    selected_profile="${maintenance_profile}"
  elif [[ -n "${maintenance_profile}" ]]; then
    if (( ${#fast_requests} == 0 && $(date +%s) < maintenance_retry )); then
      exit 0
    fi
    # Reconciliation must never absorb fresh video requests into a slow pass.
    # Even a maintenance-only request first puts the newest loops on the site.
    selected_profile="fast-video"
    request_files=("${fast_requests[@]}")
  fi

  print "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Publishing ${#request_files} coalesced request(s) as ${selected_profile}."
  publish_failed=0
  if ! publish_profile "${selected_profile}"; then
    if [[ "${selected_profile}" == fast-* && -n "${maintenance_profile}" ]] \
      && (( pass + 1 < MAX_DRAIN_PASSES && $(date +%s) >= maintenance_retry )); then
      # A fast storage guard or stale upload index may need reconciliation to
      # recover. Keep every observed request until this bounded repair succeeds.
      # Count the failed attempt too, so repair cannot extend the drain budget.
      (( pass += 1 ))
      selected_profile="${maintenance_profile}"
      request_files=("${observed_requests[@]}")
      print -u2 "Fast publication failed; attempting queued ${selected_profile} repair for ${#request_files} request(s)."
      publish_profile "${selected_profile}" || publish_failed=1
    else
      publish_failed=1
    fi
  fi
  if (( publish_failed )); then
    if [[ "${selected_profile}" == "reconcile" || "${selected_profile}" == "full" ]]; then
      print -r -- "$(( $(date +%s) + MAINTENANCE_BACKOFF_SECONDS ))" > "${MAINTENANCE_RETRY_FILE}"
    fi
    print -u2 "Full publication failed; ${#request_files} request(s) remain queued."
    exit 1
  fi
  (( ${#request_files} == 0 )) || /bin/rm -f -- "${request_files[@]}"
  if [[ "${selected_profile}" == "reconcile" || "${selected_profile}" == "full" ]]; then
    /bin/rm -f "${MAINTENANCE_RETRY_FILE}"
  else
    fresh_commit=1
  fi
  (( pass += 1 ))
done

remaining=("${REQUEST_DIR}"/*.request(N))
if (( ${#remaining} )); then
  print "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ${#remaining} newer request(s) remain for the next drain."
fi
