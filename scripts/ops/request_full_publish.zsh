#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"

STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
REQUEST_DIR="${STATE_ROOT}/state/full-publish-requests"
PROFILE="${1:-fast-existing}"
REASON="${2:-unspecified}"

case "${PROFILE}" in
  fast-existing|fast-video|reconcile|full) ;;
  *)
    print -u2 "Unknown full-publication profile: ${PROFILE}"
    exit 2
    ;;
esac

mkdir -p "${REQUEST_DIR}"
requested_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
request_id="$(date +%s).$$.${RANDOM}.${PROFILE}.request"
temporary="${REQUEST_DIR}/.${request_id}.tmp"
target="${REQUEST_DIR}/${request_id}"
{
  print -r -- "profile=${PROFILE}"
  print -r -- "requestedAt=${requested_at}"
  print -r -- "reason=${REASON}"
} > "${temporary}"
/bin/mv "${temporary}" "${target}"

# The interval is a retry backstop. Kick the dedicated launchd worker so the
# usual publication starts immediately without keeping the producer alive.
if [[ "${RADARSAT_FULL_PUBLISH_KICKSTART:-1}" == "1" ]]; then
  launchctl kickstart "gui/${UID}/com.greg.radar-sat.full-publisher" \
    >/dev/null 2>&1 || true
fi
