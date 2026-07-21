#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AGENT_DIR="${HOME}/Library/LaunchAgents"

mkdir -p "${AGENT_DIR}" "${PROJECT_ROOT}/logs" "${PROJECT_ROOT}/var/status"

for name in ingest health; do
  label="com.greg.radar-sat.${name}"
  template="${PROJECT_ROOT}/ops/${label}.plist.template"
  target="${AGENT_DIR}/${label}.plist"
  sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" "${template}" > "${target}"
  plutil -lint "${target}" >/dev/null
  launchctl bootout "gui/${UID}/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/${UID}" "${target}"
  launchctl enable "gui/${UID}/${label}"
done

print "Installed Radar-Sat ingest (3 min) and health (15 min) launch agents."
launchctl print "gui/${UID}/com.greg.radar-sat.ingest" | head -30
