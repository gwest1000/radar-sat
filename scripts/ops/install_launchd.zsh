#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AGENT_DIR="${HOME}/Library/LaunchAgents"

mkdir -p "${AGENT_DIR}" "${PROJECT_ROOT}/logs" "${PROJECT_ROOT}/var/status"

for name in ingest five-minute observations archive health; do
  label="com.greg.radar-sat.${name}"
  template="${PROJECT_ROOT}/ops/${label}.plist.template"
  target="${AGENT_DIR}/${label}.plist"
  sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" "${template}" > "${target}"
  plutil -lint "${target}" >/dev/null
  launchctl bootout "gui/${UID}/${label}" 2>/dev/null || true
  unload_attempts=0
  while launchctl print "gui/${UID}/${label}" >/dev/null 2>&1; do
    if (( unload_attempts >= 40 )); then
      print -u2 "Timed out waiting for ${label} to unload."
      exit 1
    fi
    sleep 0.25
    (( unload_attempts += 1 ))
  done
  launchctl bootstrap "gui/${UID}" "${target}"
  launchctl enable "gui/${UID}/${label}"
done

print "Installed Radar-Sat full-disk (3 min), five-minute BC (3 min), observations (5 min), archive (30 min), and health (15 min) launch agents."
launchctl print "gui/${UID}/com.greg.radar-sat.ingest" | head -30
