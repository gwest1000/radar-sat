#!/bin/zsh

# Sourced by the rapid and archive satellite workers after STATE_ROOT is set.
# Satpy can use roughly 2 GB per full-disk render, so two simultaneous renders
# are substantially slower than running them sequentially and can make the
# workstation unresponsive.

HEAVY_SATELLITE_LOCK_DIR="${STATE_ROOT}/run/heavy-satellite.lock"
HEAVY_SATELLITE_LOCK_OWNER="${HEAVY_SATELLITE_LOCK_DIR}/pid"
HEAVY_SATELLITE_LOCK_ACQUIRED=0

release_heavy_satellite_lock() {
  local owner_pid=""
  [[ -r "${HEAVY_SATELLITE_LOCK_OWNER}" ]] \
    && IFS= read -r owner_pid < "${HEAVY_SATELLITE_LOCK_OWNER}"
  if [[ "${owner_pid}" == "$$" ]]; then
    /bin/rm -f "${HEAVY_SATELLITE_LOCK_OWNER}"
    rmdir "${HEAVY_SATELLITE_LOCK_DIR}" 2>/dev/null || true
  fi
  HEAVY_SATELLITE_LOCK_ACQUIRED=0
}

try_acquire_heavy_satellite_lock() {
  local owner_pid="" stale_dir=""
  if mkdir "${HEAVY_SATELLITE_LOCK_DIR}" 2>/dev/null; then
    print -r -- "$$" > "${HEAVY_SATELLITE_LOCK_OWNER}"
    HEAVY_SATELLITE_LOCK_ACQUIRED=1
    return 0
  fi

  [[ -r "${HEAVY_SATELLITE_LOCK_OWNER}" ]] \
    && IFS= read -r owner_pid < "${HEAVY_SATELLITE_LOCK_OWNER}"
  if [[ "${owner_pid}" =~ '^[0-9]+$' ]] && kill -0 "${owner_pid}" 2>/dev/null; then
    return 1
  fi

  # A missing owner file can be the brief interval after another worker won
  # mkdir. Do not steal that lock; only recover an explicit dead owner.
  if [[ -z "${owner_pid}" ]]; then
    return 1
  fi
  stale_dir="${HEAVY_SATELLITE_LOCK_DIR}.stale.$$"
  if mv "${HEAVY_SATELLITE_LOCK_DIR}" "${stale_dir}" 2>/dev/null; then
    /bin/rm -f "${stale_dir}/pid"
    rmdir "${stale_dir}" 2>/dev/null || true
  fi
  if mkdir "${HEAVY_SATELLITE_LOCK_DIR}" 2>/dev/null; then
    print -r -- "$$" > "${HEAVY_SATELLITE_LOCK_OWNER}"
    HEAVY_SATELLITE_LOCK_ACQUIRED=1
    return 0
  fi
  return 1
}
