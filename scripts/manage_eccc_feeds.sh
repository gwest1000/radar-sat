#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_SOURCE="${PROJECT_ROOT}/config/sarracenia/subscribe"
if [[ -n "${SR3_CONFIG_DIR:-}" ]]; then
  SR3_CONFIG_ROOT="${SR3_CONFIG_DIR}"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  SR3_CONFIG_ROOT="${HOME}/Library/Application Support/sr3"
else
  SR3_CONFIG_ROOT="${XDG_CONFIG_HOME:-${HOME}/.config}/sr3"
fi
CONFIG_DEST="${SR3_CONFIG_ROOT}/subscribe"
SPOOL_ROOT="${HOME}/.local/share/radar-sat/spool/eccc"
CONFIGS=(radarsat_goes_west radarsat_lightning radarsat_bc_site_radar)

find_sr3() {
  if [[ -n "${SR3_BIN:-}" && -x "${SR3_BIN}" ]]; then
    SR3_COMMAND="${SR3_BIN}"
  elif [[ -x "${PROJECT_ROOT}/scripts/sr3-radarsat" ]]; then
    SR3_COMMAND="${PROJECT_ROOT}/scripts/sr3-radarsat"
  elif [[ -x "${PROJECT_ROOT}/.venv/bin/sr3" ]]; then
    SR3_COMMAND="${PROJECT_ROOT}/.venv/bin/sr3"
  elif command -v sr3 >/dev/null 2>&1; then
    SR3_COMMAND="$(command -v sr3)"
  else
    echo "sr3 was not found. Install requirements-feeds.txt into the project virtual environment." >&2
    exit 2
  fi
}

install_configs() {
  mkdir -p "${CONFIG_DEST}"
  mkdir -p "${SPOOL_ROOT}/satellite" "${SPOOL_ROOT}/lightning" "${SPOOL_ROOT}/radar"
  for config in "${CONFIGS[@]}"; do
    install -m 0644 "${CONFIG_SOURCE}/${config}.conf" "${CONFIG_DEST}/${config}.conf"
  done

  # Anonymous ECCC credentials are public but still belong in Sarracenia's
  # credential store so passwords never appear in the committed feed configs.
  local credentials="${SR3_CONFIG_ROOT}/credentials.conf"
  if [[ ! -f "${credentials}" ]]; then
    install -m 0600 /dev/null "${credentials}"
  fi
  if ! grep -Fq 'amqps://anonymous:anonymous@dd.weather.gc.ca/' "${credentials}"; then
    printf '%s\n' 'amqps://anonymous:anonymous@dd.weather.gc.ca/' >>"${credentials}"
  fi
  chmod 0600 "${credentials}"
  echo "Installed configs in ${CONFIG_DEST}"
  echo "Raw feed spool: ${SPOOL_ROOT}"
}

run_for_each() {
  local action="$1"
  for config in "${CONFIGS[@]}"; do
    "${SR3_COMMAND}" "${action}" "subscribe/${config}"
  done
}

parse_configs_with_sr3() {
  local features
  features="$("${SR3_COMMAND}" features)"
  if ! grep -Eq '^Installed[[:space:]]+amqp[[:space:]]' <<<"${features}"; then
    printf '%s\n' "${features}" >&2
    echo "Sarracenia's required AMQP feature is not installed." >&2
    exit 2
  fi
  if ! grep -Eq '^Installed[[:space:]]+reassembly[[:space:]]' <<<"${features}"; then
    printf '%s\n' "${features}" >&2
    echo "Sarracenia's reassembly feature is not installed." >&2
    exit 2
  fi
  for config in "${CONFIGS[@]}"; do
    "${SR3_COMMAND}" show "subscribe/${config}" >/dev/null
    echo "Sarracenia parsed subscribe/${config}.conf"
  done
}

usage() {
  printf '%s\n' \
    'Usage: scripts/manage_eccc_feeds.sh ACTION [FEED]' \
    '' \
    'Actions:' \
    '  install       install/update configs and anonymous credentials' \
    '  check         statically validate configs and ask sr3 to parse each one' \
    '  start         install configs, then start all three subscribers' \
    '  stop          stop all three subscribers' \
    '  restart       install configs, then restart all three subscribers' \
    '  status        show Sarracenia process/lag status' \
    '  sanity        restart missing or hung Radar-Sat processes' \
    '  foreground    run one feed interactively (goes_west, lightning, bc_site_radar)' \
    '  supervise     keep the subscribers healthy (intended for launchd)' \
    '  install-agent install and load the per-user macOS launch agent' \
    '  cleanup       stop subscribers and remove their broker queues/bindings' \
    '' \
    'Set SR3_BIN=/absolute/path/to/sr3 to select a non-default installation.'
}

action="${1:-}"
case "${action}" in
  install)
    install_configs
    ;;
  check)
    install_configs
    find_sr3
    python3 "${PROJECT_ROOT}/scripts/check_eccc_feeds.py"
    parse_configs_with_sr3
    ;;
  start)
    install_configs
    find_sr3
    run_for_each start
    "${SR3_COMMAND}" status
    ;;
  stop)
    find_sr3
    run_for_each stop
    ;;
  restart)
    install_configs
    find_sr3
    run_for_each restart
    "${SR3_COMMAND}" status
    ;;
  status)
    find_sr3
    "${SR3_COMMAND}" status
    ;;
  sanity)
    find_sr3
    run_for_each sanity
    ;;
  foreground)
    feed="${2:-}"
    case "${feed}" in
      goes_west) config=radarsat_goes_west ;;
      lightning) config=radarsat_lightning ;;
      bc_site_radar) config=radarsat_bc_site_radar ;;
      *) echo "foreground requires: goes_west, lightning, or bc_site_radar" >&2; exit 2 ;;
    esac
    install_configs
    find_sr3
    exec "${SR3_COMMAND}" foreground "subscribe/${config}"
    ;;
  supervise)
    install_configs
    find_sr3
    run_for_each start
    shutdown_supervisor() {
      trap - EXIT INT TERM
      run_for_each stop >/dev/null 2>&1 || true
      exit 0
    }
    trap shutdown_supervisor INT TERM
    trap 'run_for_each stop >/dev/null 2>&1 || true' EXIT
    while true; do
      sleep 60
      run_for_each sanity || true
    done
    ;;
  install-agent)
    install_configs
    find_sr3
    launch_agents="${HOME}/Library/LaunchAgents"
    logs="${HOME}/Library/Logs/Radar-Sat"
    destination="${launch_agents}/ca.radarsat.eccc-feeds.plist"
    template="${PROJECT_ROOT}/config/launchd/ca.radarsat.eccc-feeds.plist.template"
    temporary="$(mktemp "${TMPDIR:-/tmp}/radarsat-eccc-feeds.XXXXXX")"
    trap 'rm -f "${temporary}"' EXIT
    mkdir -p "${launch_agents}" "${logs}"
    sed \
      -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" \
      -e "s|__LOG_DIRECTORY__|${logs}|g" \
      -e "s|__SR3_BIN__|${SR3_COMMAND}|g" \
      "${template}" >"${temporary}"
    plutil -lint "${temporary}"
    install -m 0644 "${temporary}" "${destination}"
    launchctl bootout "gui/$(id -u)/ca.radarsat.eccc-feeds" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "${destination}"
    launchctl kickstart -k "gui/$(id -u)/ca.radarsat.eccc-feeds"
    echo "Installed and started ${destination}"
    ;;
  cleanup)
    find_sr3
    run_for_each stop || true
    run_for_each cleanup
    ;;
  *)
    usage
    exit 2
    ;;
esac
