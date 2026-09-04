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

if [[ ! "${MAX_DRAIN_PASSES}" =~ '^[1-9][0-9]*$' ]]; then
  print -u2 "RADARSAT_FULL_PUBLISH_MAX_DRAIN_PASSES must be a positive integer."
  exit 2
fi

publish_profile() {
  local profile="$1"
  case "${profile}" in
    fast-existing)
      "${PUBLISHER}" \
        --fast --existing-video-only --whole-frame-only --recovery-hours 24
      ;;
    fast-video)
      "${PUBLISHER}" --fast --whole-frame-only --recovery-hours 24
      ;;
    reconcile)
      "${PUBLISHER}" --whole-frame-only --recovery-hours 24
      ;;
    full)
      "${PUBLISHER}"
      ;;
  esac
}

pass=0
while (( pass < MAX_DRAIN_PASSES )); do
  request_files=("${REQUEST_DIR}"/*.request(N))
  (( ${#request_files} )) || exit 0

  selected_profile="fast-existing"
  for request_file in "${request_files[@]}"; do
    request_profile="${request_file:t:r:e}"
    case "${request_profile}" in
      full)
        selected_profile="full"
        break
        ;;
      reconcile)
        [[ "${selected_profile}" == "full" ]] \
          || selected_profile="reconcile"
        ;;
      fast-video)
        [[ "${selected_profile}" == "full" || "${selected_profile}" == "reconcile" ]] \
          || selected_profile="fast-video"
        ;;
    esac
  done

  print "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Publishing ${#request_files} coalesced request(s) as ${selected_profile}."
  if ! publish_profile "${selected_profile}"; then
    print -u2 "Full publication failed; ${#request_files} request(s) remain queued."
    exit 1
  fi
  /bin/rm -f -- "${request_files[@]}"
  (( pass += 1 ))
done

remaining=("${REQUEST_DIR}"/*.request(N))
if (( ${#remaining} )); then
  print "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ${#remaining} newer request(s) remain for the next drain."
fi
