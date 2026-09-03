#!/bin/zsh

set -euo pipefail
setopt NO_BG_NICE
zmodload zsh/zselect

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROJECT_ROOT}/scripts/ops/runtime_paths.zsh"

STATE_ROOT="${RADARSAT_STATE_ROOT:-${PROJECT_ROOT}/var}"
SCHEDULER_STATE="${STATE_ROOT}/state/video-scheduler"
LOCK_DIR="${STATE_ROOT}/run/video-worker.lock"
LOCK_OWNER="${LOCK_DIR}/pid"
DIRTY_FILE="${SCHEDULER_STATE}/publication-dirty"
CATALOG_PATH="${OUTPUT_ROOT}/catalog.json"
COMPOSITE_BUILDER="${RADARSAT_COMPOSITE_VIDEO_BUILDER:-${PROJECT_ROOT}/scripts/build_composite_video.py}"
LEGACY_BUILDER="${RADARSAT_LEGACY_VIDEO_BUILDER:-${PROJECT_ROOT}/scripts/build_satellite_video.py}"
CATALOG_WRITER="${RADARSAT_VIDEO_CATALOG_WRITER:-${PROJECT_ROOT}/scripts/write_catalog.py}"
PUBLISHER="${RADARSAT_VIDEO_PUBLISHER:-${PROJECT_ROOT}/scripts/ops/publish_locked.zsh}"
MAX_RUNTIME_SECONDS="${RADARSAT_VIDEO_SCHEDULER_MAX_RUNTIME_SECONDS:-3300}"
TERMINATE_GRACE_SECONDS="${RADARSAT_VIDEO_TERMINATE_GRACE_SECONDS:-15}"
KILL_REAP_SECONDS="${RADARSAT_VIDEO_KILL_REAP_SECONDS:-5}"
FAILURE_BACKOFF_SECONDS="${RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS:-120}"
PRUNE_INTERVAL_SECONDS="${RADARSAT_VIDEO_PRUNE_INTERVAL_SECONDS:-3600}"
MAX_EXACT_WORKERS="${RADARSAT_VIDEO_MAX_EXACT_WORKERS:-2}"
HYBRID_CORE_ENABLED="${RADARSAT_HYBRID_CORE_ENABLED:-1}"
HYBRID_CORE_PRESETS=(weather-core-v1 weather-smoke-core-v1)

if (( MAX_EXACT_WORKERS < 1 || MAX_EXACT_WORKERS > 2 )); then
  print -u2 "RADARSAT_VIDEO_MAX_EXACT_WORKERS must be 1 or 2."
  exit 2
fi
if [[ ! "${MAX_RUNTIME_SECONDS}" =~ '^[1-9][0-9]*$' ]] \
  || [[ ! "${TERMINATE_GRACE_SECONDS}" =~ '^[0-9]+$' ]] \
  || [[ ! "${KILL_REAP_SECONDS}" =~ '^[0-9]+$' ]]; then
  print -u2 "Video scheduler runtime and termination limits must be whole seconds."
  exit 2
fi

mkdir -p \
  "${STATE_ROOT}/run" \
  "${STATE_ROOT}/state" \
  "${STATE_ROOT}/status" \
  "${SCHEDULER_STATE}" \
  "${PROJECT_ROOT}/logs"

release_lock() {
  local owner_pid=""
  [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
  if [[ "${owner_pid}" == "$$" ]]; then
    /bin/rm -f "${LOCK_OWNER}"
    rmdir "${LOCK_DIR}" 2>/dev/null || true
  fi
}

typeset -ga ACTIVE_VIDEO_PIDS=()
typeset -ga ACTIVE_WATCHDOG_PIDS=()

process_group_running() {
  local leader_pid="$1"
  kill -0 -- "-${leader_pid}" 2>/dev/null
}

terminate_process_trees() {
  local signal_name="$1"
  shift
  local root_pid stop_epoch
  local -a roots=("$@") survivors=()
  (( ${#roots} )) || return 0
  for root_pid in "${roots[@]}"; do
    (( root_pid > 0 )) || continue
    # Each managed command starts in a private session/process group. Signal
    # the group so Python, ffmpeg, lockf, shell helpers, and uploader children
    # share one bounded lifecycle.
    kill -s "${signal_name}" -- "-${root_pid}" 2>/dev/null \
      || kill -s "${signal_name}" "${root_pid}" 2>/dev/null \
      || true
  done
  stop_epoch=$(( $(date +%s) + TERMINATE_GRACE_SECONDS ))
  while true; do
    survivors=()
    for root_pid in "${roots[@]}"; do
      process_group_running "${root_pid}" && survivors+=("${root_pid}")
    done
    (( ${#survivors} )) || return 0
    (( $(date +%s) < stop_epoch )) || break
    sleep 0.1
  done
  for root_pid in "${survivors[@]}"; do
    kill -KILL -- "-${root_pid}" 2>/dev/null \
      || kill -KILL "${root_pid}" 2>/dev/null \
      || true
  done
  stop_epoch=$(( $(date +%s) + KILL_REAP_SECONDS ))
  while true; do
    roots=()
    for root_pid in "${survivors[@]}"; do
      process_group_running "${root_pid}" && roots+=("${root_pid}")
    done
    (( ${#roots} )) || return 0
    (( $(date +%s) < stop_epoch )) || break
    sleep 0.1
  done
  print -u2 "Process groups did not exit after SIGKILL: ${roots[*]}"
  return 1
}

cancel_deadline_watchdog() {
  local watchdog_pid
  for watchdog_pid in "${ACTIVE_WATCHDOG_PIDS[@]}"; do
    kill -TERM "${watchdog_pid}" 2>/dev/null || true
    wait "${watchdog_pid}" 2>/dev/null || true
  done
  ACTIVE_WATCHDOG_PIDS=()
}

finish_deadline_watchdog() {
  local watchdog_pid
  if (( $(date +%s) < SCHEDULER_DEADLINE_EPOCH )); then
    cancel_deadline_watchdog
    return 0
  fi
  # Once the deadline fires, let the watchdog finish its bounded TERM/KILL
  # sequence. Cancelling it merely because the direct wrapper exited could
  # otherwise strand an encoder grandchild before the lock is released.
  for watchdog_pid in "${ACTIVE_WATCHDOG_PIDS[@]}"; do
    wait "${watchdog_pid}" 2>/dev/null || true
  done
  ACTIVE_WATCHDOG_PIDS=()
}

arm_deadline_watchdog() {
  local label="$1"
  shift
  local remaining=$(( SCHEDULER_DEADLINE_EPOCH - $(date +%s) ))
  local -a roots=("$@")
  (
    trap 'exit 0' INT TERM
    if (( remaining > 0 )); then
      # A zsh-native timeout keeps the watchdog childless. Cancelling a
      # watchdog must not strand an external `sleep` that keeps launchd/test
      # output pipes open for the full scheduler runtime.
      zselect -t "$(( remaining * 100 ))" || true
    fi
    print "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Scheduler deadline reached during ${label}; terminating its process tree."
    terminate_process_trees TERM "${roots[@]}" || true
  ) &
  ACTIVE_WATCHDOG_PIDS=("$!")
}

shutdown_scheduler() {
  local signal_name="$1" exit_code="$2" child_pid
  # Keep the global lock until every worker has forwarded the signal to its
  # encoder and has been reaped. This prevents a replacement launchd job from
  # starting beside an orphaned Python/FFmpeg process during redeploy.
  trap - INT TERM EXIT
  cancel_deadline_watchdog
  terminate_process_trees "${signal_name}" "${ACTIVE_VIDEO_PIDS[@]}" || true
  for child_pid in "${ACTIVE_VIDEO_PIDS[@]}"; do
    wait "${child_pid}" 2>/dev/null || true
  done
  ACTIVE_VIDEO_PIDS=()
  release_lock
  exit "${exit_code}"
}

acquire_lock() {
  local owner_pid="" attempts=0 stale_dir=""
  while ! mkdir "${LOCK_DIR}" 2>/dev/null; do
    owner_pid=""
    [[ -r "${LOCK_OWNER}" ]] && IFS= read -r owner_pid < "${LOCK_OWNER}"
    if [[ "${owner_pid}" =~ '^[0-9]+$' ]] && kill -0 "${owner_pid}" 2>/dev/null; then
      print "Radar-Sat video scheduler is already running as PID ${owner_pid}; exiting."
      return 1
    fi
    if [[ -z "${owner_pid}" && "${attempts}" -eq 0 ]]; then
      sleep 1
      attempts=1
      continue
    fi
    stale_dir="${LOCK_DIR}.stale.$$"
    if mv "${LOCK_DIR}" "${stale_dir}" 2>/dev/null; then
      /bin/rm -f "${stale_dir}/pid"
      rmdir "${stale_dir}" 2>/dev/null || true
      attempts=0
    fi
  done
  print -r -- "$$" > "${LOCK_OWNER}"
}

if ! acquire_lock; then
  exit 0
fi
trap release_lock EXIT
trap 'shutdown_scheduler INT 130' INT
trap 'shutdown_scheduler TERM 143' TERM

export PYTHONPATH="${PROJECT_ROOT}"

if [[ "${RADARSAT_VIDEO_ENABLED:-${RADARSAT_H264_PILOT_ENABLED:-0}}" != "1" ]]; then
  exit 0
fi

started_epoch="$(date +%s)"
SCHEDULER_DEADLINE_EPOCH=$(( started_epoch + MAX_RUNTIME_SECONDS ))

# Resolve the exact policy from the same Python configuration used by the
# builder. The source digest is an automatic render/config revision: a deploy
# that changes composition code, presets, opacity policy, or exact-range
# configuration invalidates stale scheduler tokens even if source data did not
# change. RADARSAT_VIDEO_BUILD_REVISION remains an explicit emergency override.
VIDEO_BUILD_POLICY_JSON="$("${PYTHON_BIN}" -c '
import hashlib
import json
import os
from pathlib import Path
import re

import radarsat.config as config

package = Path(config.__file__).parent
video_path = package / "video.py"
composite_path = package / "composite_video.py"
source_paths = (Path(config.__file__), video_path, composite_path)
digest = hashlib.sha256()
for path in source_paths:
    digest.update(path.name.encode())
    digest.update(path.read_bytes())

def integer_constant(path, name):
    match = re.search(rf"^{name}\s*=\s*(\d+)", path.read_text(), re.MULTILINE)
    if match is None:
        raise RuntimeError(f"missing {name} in {path}")
    return int(match.group(1))

print(json.dumps({
    "deploymentRevision": os.environ.get("RADARSAT_VIDEO_BUILD_REVISION") or digest.hexdigest(),
    "compositeRenderVersion": integer_constant(video_path, "COMPOSITE_RENDER_VERSION"),
    "sidecarSchemaVersion": integer_constant(composite_path, "COMPOSITE_SIDECAR_SCHEMA_VERSION"),
    "frameCacheVersion": integer_constant(composite_path, "COMPOSITE_FRAME_CACHE_VERSION"),
    "exactRanges": config.VIDEO_EXACT_RANGES,
    "presetPolicy": config.VIDEO_COMPOSITE_PRESETS,
}, sort_keys=True, separators=(",", ":")))
')"

log_message() {
  print "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

run_driver() {
  local driver="$1"
  shift
  if [[ "${driver}" == *.py ]]; then
    "${PYTHON_BIN}" "${driver}" "$@"
  else
    "${driver}" "$@"
  fi
}

typeset -g STARTED_COMMAND_PID=0

start_isolated_command() {
  # macOS does not ship `setsid`. Use the configured Python only as a tiny
  # exec trampoline so every potentially expensive stage owns a private
  # process group that can be terminated without process-table traversal.
  "${PYTHON_BIN}" -c \
    'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    "$@" &
  STARTED_COMMAND_PID=$!
}

start_isolated_driver() {
  local driver="$1"
  shift
  if [[ "${driver}" == *.py ]]; then
    start_isolated_command "${PYTHON_BIN}" "${driver}" "$@"
  else
    start_isolated_command "${driver}" "$@"
  fi
}

read_epoch() {
  local state_file="$1" value="0"
  [[ -r "${state_file}" ]] && IFS= read -r value < "${state_file}"
  [[ "${value}" =~ '^[0-9]+$' ]] || value=0
  print -r -- "${value}"
}

atomic_state() {
  local state_file="$1" value="$2" temporary
  temporary="${state_file}.$$"
  print -r -- "${value}" > "${temporary}"
  /bin/mv "${temporary}" "${state_file}"
}

state_key() {
  print -r -- "$1" | tr -c 'A-Za-z0-9._-' '_'
}

latest_token() {
  local product_id="${1:-}" range_hours="${2:-}" payload=""
  local token_now_epoch="${RADARSAT_VIDEO_TOKEN_NOW_EPOCH:-$(date +%s)}"
  if [[ ! -s "${CATALOG_PATH}" ]] \
    || [[ -z "${product_id}" ]] \
    || [[ ! "${range_hours}" =~ '^[0-9]+$' ]] \
    || [[ ! "${token_now_epoch}" =~ '^[0-9]+$' ]]; then
    return 1
  fi
  payload="$(jq -ceS \
    --arg requested "${product_id}" \
    --argjson rangeHours "${range_hours}" \
    --argjson nowEpoch "${token_now_epoch}" \
    --argjson buildPolicy "${VIDEO_BUILD_POLICY_JSON}" '
    def regional_key($productId):
      {
        "bc-small-overlay": "small",
        "bc-southwest-overlay": "southwest",
        "bc-southeast-overlay": "southeast",
        "bc-northeast-overlay": "northeast",
        "bc-south-coast-overlay": "south-coast"
      }[$productId] // null;
    def regional_dynamic:
      [
        "radar-rain", "lightning-trail", "lightning-hour", "hotspots",
        "hrdps-hgt500", "hrdps-mslp"
      ];
    def regional_static:
      ["watersheds", "transmission-lines", "boundaries"];
    def frame_epoch:
      try (.validTime | fromdateiso8601) catch null;
    def compact_frames($layer; $cutoff; $ceiling; $ceilingTolerance):
      ($layer // {}) as $value
      | (($value.maxAgeMinutes // 0) | tonumber) as $maxAge
      | {
          maxAgeMinutes: ($value.maxAgeMinutes // null),
          role: ($value.role // null),
          frames: [
            $value.frames[]?
            | (frame_epoch // 0) as $epoch
            | select(
                $epoch >= ($cutoff - $maxAge * 60)
                and $epoch <= ($ceiling + $ceilingTolerance)
              )
          ]
        };
    def exact_slots($layer; $cadenceSeconds; $timelineCeiling):
      [
        $layer.frames[]?
        | frame_epoch
        | select(. != null)
        | . as $epoch
        | ((((($epoch + 120) / $cadenceSeconds) | floor) * $cadenceSeconds)) as $slot
        | select(($epoch - $slot | fabs) <= 120 and $slot <= $timelineCeiling)
        | $slot
      ] | unique;
    def resolved_layers($product; $satellite; $optionalLayers):
      [
        $product.layers[]?
        | . as $recipe
        | select(
            if $recipe.enabledWith then
              ($optionalLayers | index($recipe.enabledWith)) != null
            elif $recipe.choiceGroup == "satellite" then
              $recipe.id == $satellite
            elif $recipe.optional then
              ($optionalLayers | index($recipe.id)) != null
            else
              true
            end
          )
        | .id
      ];
    . as $root
    | ($root.products[] | select(.id == $requested)) as $product
    | ($root.domains[$product.domain]) as $domain
    | (
        if $rangeHours == 24 then "day"
        elif $rangeHours == 168 then "archive"
        else "live"
        end
      ) as $track
    | ([
        $product.layers[]?
        | select(.choiceGroup == "satellite" and .defaultEnabled == true)
        | .id
      ][0] // $product.anchorLayer) as $satellite
    | ($buildPolicy.presetPolicy[$product.id]) as $presetPolicy
    | select($presetPolicy != null)
    | ([ $domain.layers["radar-rain"].frames[]? | frame_epoch | select(. != null) ]) as $radarEpochs
    | ($radarEpochs | max) as $newestRadarEpoch
    | (
        $product.domain == "bc"
        and $track != "archive"
        and $rangeHours <= 24
        and ($radarEpochs | length) >= 2
      ) as $rapidRadar
    | ({
        satelliteLayerId: $satellite,
        cadenceMinutes: (
          if $rapidRadar then 6
          elif $track == "day" then ($product.dayFrameIntervalMinutes // 30)
          elif $track == "archive" then ($product.archiveFrameIntervalMinutes // 60)
          else ($product.frameIntervalMinutes // 10)
          end
        ),
        presets: [
          $presetPolicy[]
          | . as $preset
          | {
              id: $preset.id,
              layerIds: resolved_layers(
                $product;
                $satellite;
                ($preset.optionalLayers // [])
              )
            }
        ]
      }) as $profile
    | ([ $profile.presets[]?.layerIds[]? ] | unique) as $resolvedLayerIds
    | (regional_key($product.id)) as $region
    | [
        $resolvedLayerIds[] as $recipeId
        | ([ $product.layers[]? | select(.id == $recipeId) ][0] // {id: $recipeId})
      ] as $recipes
    | (
        if $product.domain == "bc" and $satellite == "eccc-geocolor"
        then ["eccc-geocolor", "raw-visir", "raw-visir-native"]
        else [$satellite]
        end
      ) as $satelliteInputs
    | ([
        $satelliteInputs[] as $layerId
        | $domain.layers[$layerId].frames[]?
        | frame_epoch
        | select(. != null)
      ] | max) as $newestSatelliteEpoch
    | select($newestSatelliteEpoch != null)
    | ($profile.cadenceMinutes * 60) as $cadenceSeconds
    | (((($newestSatelliteEpoch / $cadenceSeconds) | floor) * $cadenceSeconds)) as $timelineCeiling
    | (
        if $rapidRadar then
          $newestRadarEpoch
        elif $product.domain == "bc" and $satellite == "eccc-geocolor" then
          (exact_slots($domain.layers["eccc-geocolor"]; $cadenceSeconds; $timelineCeiling)) as $mscSlots
          | ((
              exact_slots($domain.layers["raw-visir-native"]; $cadenceSeconds; $timelineCeiling)
              + exact_slots($domain.layers["raw-visir"]; $cadenceSeconds; $timelineCeiling)
            ) | unique) as $noaaSlots
          | ([
              $mscSlots[],
              (
                $noaaSlots[] as $slot
                | select(
                    ($mscSlots | index($slot)) == null
                    and ($nowEpoch - $slot) >= 35 * 60
                  )
                | $slot
              )
            ] | max)
        else
          $newestSatelliteEpoch
        end
      ) as $selectedAnchorEpoch
    | select($selectedAnchorEpoch != null)
    | ($selectedAnchorEpoch - $rangeHours * 3600) as $cutoff
    | {
        schema: 3,
        rangeHours: $rangeHours,
        track: $track,
        selectedAnchorEpoch: $selectedAnchorEpoch,
        buildPolicy: {
          deploymentRevision: $buildPolicy.deploymentRevision,
          compositeRenderVersion: $buildPolicy.compositeRenderVersion,
          sidecarSchemaVersion: $buildPolicy.sidecarSchemaVersion,
          frameCacheVersion: $buildPolicy.frameCacheVersion,
          exactRanges: $buildPolicy.exactRanges[$product.id],
          presetPolicy: $buildPolicy.presetPolicy[$product.id],
          resolvedProfile: $profile
        },
        product: {
          id: $product.id,
          domain: $product.domain,
          viewport: ($product.viewport // null),
          frameIntervalMinutes: ($product.frameIntervalMinutes // null),
          dayFrameIntervalMinutes: ($product.dayFrameIntervalMinutes // null)
        },
        defaultSatellite: $satellite,
        recipes: $recipes,
        inputs: (
          [
            $satelliteInputs[] as $layerId
            | {
                recipeId: $satellite,
                renderedLayerId: $layerId,
                kind: "frames",
                value: compact_frames(
                  $domain.layers[$layerId];
                  $cutoff;
                  $selectedAnchorEpoch;
                  (if $product.domain == "bc" and $satellite == "eccc-geocolor" then 120 else 0 end)
                )
              }
          ]
          + [
              $recipes[]
              | select(.id != "base-dark" and (.choiceGroup // "") != "satellite")
              | . as $recipe
              | (
                  if $track == "archive" and $recipe.id == "lightning-trail" then
                    "lightning-hour"
                  elif $track == "archive" and $recipe.id == "glm-lightning-trail" then
                    "glm-lightning-hour"
                  elif $recipe.id == "model-hgt500" then
                    if $product.domain == "bc" then "hrdps-hgt500" else "ecmwf-hgt500" end
                  elif $recipe.id == "model-mslp" then
                    if $product.domain == "bc" then "hrdps-mslp" else "ecmwf-mslp" end
                  else $recipe.id
                  end
                ) as $baseId
              | (
                  if $region != null
                    and (regional_dynamic | index($baseId)) != null
                    and (($domain.layers[($baseId + "-region-" + $region)].frames // []) | length) > 0
                  then $baseId + "-region-" + $region
                  else $baseId
                  end
                ) as $renderedId
              | (
                  if $region != null
                    and (regional_static | index($recipe.id)) != null
                    and $domain.staticLayers[($recipe.id + "-region-" + $region)] != null
                  then $recipe.id + "-region-" + $region
                  else $recipe.id
                  end
                ) as $staticId
              | if $domain.staticLayers[$staticId] != null then
                  {
                    recipeId: $recipe.id,
                    renderedLayerId: $staticId,
                    kind: "static",
                    value: $domain.staticLayers[$staticId]
                  }
                else
                  {
                    recipeId: $recipe.id,
                    renderedLayerId: $renderedId,
                    kind: "frames",
                    value: compact_frames(
                      $domain.layers[$renderedId];
                      $cutoff;
                      $selectedAnchorEpoch;
                      0
                    )
                  }
                end
            ]
          + [{
              recipeId: "base-dark",
              renderedLayerId: "base-dark",
              kind: "static",
              value: ($domain.staticLayers["base-dark"] // null)
            }]
        )
      }
  ' "${CATALOG_PATH}")" || return 1
  print -rn -- "${payload}" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}'
}

range_interval() {
  case "$1" in
    3) print 0 ;;
    6) print 900 ;;
    12|24) print 1800 ;;
    168) print 3600 ;;
    *) return 1 ;;
  esac
}

range_track() {
  case "$1" in
    3|6|12) print live ;;
    24) print day ;;
    168) print archive ;;
    *) return 1 ;;
  esac
}

range_products() {
  case "$1" in
    3|6)
      print -l -- \
        bc-large-overlay \
        bc-small-overlay \
        bc-southwest-overlay \
        bc-southeast-overlay \
        bc-northeast-overlay \
        bc-south-coast-overlay
      ;;
    12)
      print -l -- \
        bc-large-overlay \
        bc-small-overlay \
        bc-southwest-overlay \
        bc-southeast-overlay \
        bc-northeast-overlay \
        bc-south-coast-overlay \
        pacific-wna-overlay \
        north-america-overlay \
        north-pacific-overlay
      ;;
    24)
      print -l -- \
        bc-large-overlay \
        bc-small-overlay \
        bc-southwest-overlay \
        bc-southeast-overlay \
        bc-northeast-overlay \
        pacific-wna-overlay \
        north-america-overlay \
        north-pacific-overlay
      ;;
    168)
      print -l -- \
        bc-large-overlay \
        pacific-wna-overlay \
        north-america-overlay \
        north-pacific-overlay
      ;;
    *) return 1 ;;
  esac
}

batch_due() {
  local range="$1" token="$2" now_epoch="$3" task_id="${4:-range-$1}"
  local key interval last_success last_attempt last_token=""
  key="$(state_key "${task_id}")"
  interval="$(range_interval "${range}")"
  last_success="$(read_epoch "${SCHEDULER_STATE}/${key}.success-epoch")"
  last_attempt="$(read_epoch "${SCHEDULER_STATE}/${key}.attempt-epoch")"
  [[ -r "${SCHEDULER_STATE}/${key}.token" ]] \
    && IFS= read -r last_token < "${SCHEDULER_STATE}/${key}.token"
  if [[ "${token}" == "${last_token}" ]]; then
    return 1
  fi
  if (( now_epoch - last_attempt < FAILURE_BACKOFF_SECONDS )); then
    return 1
  fi
  if (( interval > 0 && now_epoch - last_success < interval )); then
    return 1
  fi
  return 0
}

mark_attempt() {
  local task_id="$1" epoch="$2"
  atomic_state "${SCHEDULER_STATE}/$(state_key "${task_id}").attempt-epoch" "${epoch}"
}

mark_success() {
  local task_id="$1" token="$2" epoch="$3" key
  key="$(state_key "${task_id}")"
  atomic_state "${SCHEDULER_STATE}/${key}.token" "${token}"
  atomic_state "${SCHEDULER_STATE}/${key}.success-epoch" "${epoch}"
}

mark_dirty() {
  atomic_state "${DIRTY_FILE}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}

run_with_deadline() {
  local label="$1" result=0 child_pid
  shift
  if (( $(date +%s) >= SCHEDULER_DEADLINE_EPOCH )); then
    log_message "Scheduler deadline already reached before ${label}; deferring it."
    return 124
  fi
  start_isolated_command "$@"
  child_pid="${STARTED_COMMAND_PID}"
  ACTIVE_VIDEO_PIDS=("${child_pid}")
  arm_deadline_watchdog "${label}" "${child_pid}"
  wait "${child_pid}" || result=$?
  finish_deadline_watchdog
  ACTIVE_VIDEO_PIDS=()
  return "${result}"
}

run_driver_with_deadline() {
  local label="$1" driver="$2"
  shift 2
  if [[ "${driver}" == *.py ]]; then
    run_with_deadline "${label}" "${PYTHON_BIN}" "${driver}" "$@"
  else
    run_with_deadline "${label}" "${driver}" "$@"
  fi
}

publish_dirty() {
  [[ -e "${DIRTY_FILE}" ]] || return 0
  log_message "Publishing completed video batches."
  if ! run_driver_with_deadline \
    "catalog generation" \
    "${CATALOG_WRITER}" --output-root "${OUTPUT_ROOT}"; then
    log_message "Catalog write failed; publication remains dirty."
    return 1
  fi
  if ! run_with_deadline \
    "R2 publication" \
    "${PUBLISHER}" --fast --whole-frame-only --recovery-hours 24; then
    log_message "R2 publication failed; completed media will be retried without rebuilding."
    return 1
  fi
  /bin/rm -f "${DIRTY_FILE}"
}

run_exact_worker() {
  local range="$1" track="$2" product="$3"
  local -a args=(
    --source-root "${OUTPUT_ROOT}"
    --output-root "${OUTPUT_ROOT}"
    --track "${track}"
    --range-hours "${range}"
    --defer-cache-prune
  )
  args+=(--product "${product}")
  if [[ -n "${RADARSAT_FFMPEG:-}" ]]; then
    args+=(--ffmpeg "${RADARSAT_FFMPEG}")
  fi
  start_isolated_driver "${COMPOSITE_BUILDER}" "${args[@]}"
}

hybrid_range_products() {
  case "$1" in
    3|6)
      print -l -- bc-large-overlay bc-northeast-overlay
      ;;
    12|24)
      print -l -- \
        bc-large-overlay \
        bc-northeast-overlay \
        north-america-overlay
      ;;
    *) return 1 ;;
  esac
}

run_hybrid_worker() {
  local range="$1" track="$2" product="$3" preset="$4"
  local -a args=(
    --source-root "${OUTPUT_ROOT}"
    --output-root "${OUTPUT_ROOT}"
    --track "${track}"
    --range-hours "${range}"
    --preset "${preset}"
    --defer-cache-prune
    --product "${product}"
  )
  if [[ -n "${RADARSAT_FFMPEG:-}" ]]; then
    args+=(--ffmpeg "${RADARSAT_FFMPEG}")
  fi
  if [[ "${COMPOSITE_BUILDER}" == *.py ]]; then
    start_isolated_command \
      /usr/bin/nice -n 10 "${PYTHON_BIN}" "${COMPOSITE_BUILDER}" "${args[@]}"
  else
    start_isolated_command \
      /usr/bin/nice -n 10 "${COMPOSITE_BUILDER}" "${args[@]}"
  fi
}

run_one_hybrid_profile() {
  local now_epoch="$1" range product preset token task_id track worker_result=0 worker_pid=0
  local -a products
  [[ "${HYBRID_CORE_ENABLED}" == "1" ]] || return 2
  for preset in "${HYBRID_CORE_PRESETS[@]}"; do
    for range in 3 6 12 24; do
      products=("${(@f)$(hybrid_range_products "${range}")}")
      for product in "${products[@]}"; do
        token="$(latest_token "${product}" "${range}" || true)"
        if [[ "${preset}" == "weather-smoke-core-v1" ]]; then
          # Preserve the deployed smoke-core state keys across this upgrade.
          task_id="hybrid-${range}-${product}"
        else
          task_id="hybrid-${preset}-${range}-${product}"
        fi
        if [[ -z "${token}" ]] \
          || ! batch_due "${range}" "${token}" "${now_epoch}" "${task_id}"; then
          continue
        fi
        track="$(range_track "${range}")"
        mark_attempt "${task_id}" "${now_epoch}"
        mark_dirty
        log_message \
          "Building lower-priority ${range}h ${preset} for ${product}."
        run_hybrid_worker "${range}" "${track}" "${product}" "${preset}"
        worker_pid="${STARTED_COMMAND_PID}"
        ACTIVE_VIDEO_PIDS=("${worker_pid}")
        arm_deadline_watchdog \
          "${product} ${range}h ${preset} worker" "${worker_pid}"
        wait "${worker_pid}" || worker_result=$?
        finish_deadline_watchdog
        ACTIVE_VIDEO_PIDS=()
        if (( worker_result == 0 )); then
          mark_success "${task_id}" "${token}" "${now_epoch}"
        fi
        return "${worker_result}"
      done
    done
  done
  return 2
}

typeset -gA SELECTED_TOKENS

build_exact_batch() {
  local range="$1" track="$2" now_epoch="$3"
  shift 3
  local -a products=("$@")
  local next_index=1 batch_status=0 slot product pid worker_result
  local -a pids worker_products
  while (( next_index <= ${#products} )); do
    pids=()
    worker_products=()
    for slot in {1..${MAX_EXACT_WORKERS}}; do
      (( next_index <= ${#products} )) || break
      product="${products[${next_index}]}"
      mark_attempt "exact-${range}-${product}" "${now_epoch}"
      run_exact_worker "${range}" "${track}" "${product}"
      pid="${STARTED_COMMAND_PID}"
      ACTIVE_VIDEO_PIDS+=("${pid}")
      pids+=("${pid}")
      worker_products+=("${product}")
      (( next_index += 1 ))
    done
    arm_deadline_watchdog "${range}h exact-video workers" "${pids[@]}"
    for slot in {1..${#pids}}; do
      worker_result=0
      wait "${pids[${slot}]}" || worker_result=$?
      if (( worker_result == 0 )); then
        product="${worker_products[${slot}]}"
        mark_success \
          "exact-${range}-${product}" \
          "${SELECTED_TOKENS[${product}]}" \
          "${now_epoch}"
      else
        batch_status=1
      fi
    done
    finish_deadline_watchdog
    ACTIVE_VIDEO_PIDS=()
  done
  return "${batch_status}"
}

run_archive_worker() {
  local product="$1" layer=""
  case "${product}" in
    bc-large-overlay) layer="eccc-geocolor" ;;
    north-america-overlay) layer="westwx-visir" ;;
    pacific-wna-overlay|north-pacific-overlay) layer="raw-visir" ;;
    *)
      print -u2 "No public archive satellite layer is configured for ${product}."
      return 2
      ;;
  esac
  local -a args=(
    --source-root "${OUTPUT_ROOT}"
    --output-root "${OUTPUT_ROOT}"
    --track archive
    --archive-hours "${RADARSAT_VIDEO_ARCHIVE_HOURS:-168}"
    --defer-shared-prune
    --layer "${layer}"
  )
  args+=(--product "${product}")
  if [[ -n "${RADARSAT_FFMPEG:-}" ]]; then
    args+=(--ffmpeg "${RADARSAT_FFMPEG}")
  fi
  start_isolated_command \
    /usr/bin/nice -n 15 "${PYTHON_BIN}" "${LEGACY_BUILDER}" "${args[@]}"
}

run_batch() {
  local range="$1" now_epoch="$2" track
  shift 2
  local -a products=("$@")
  track="$(range_track "${range}")"
  log_message "Building ${range}h ${track} batch for ${#products} products."
  mark_dirty
  if ! build_exact_batch "${range}" "${track}" "${now_epoch}" "${products[@]}"; then
    log_message "The ${range}h batch was incomplete; successful profiles will publish and failures will retry."
    return 1
  fi
  return 0
}

run_one_archive_product() {
  local now_epoch="$1" cursor offset index product token task_id archive_result=0 archive_worker_pid=0
  local -a products=("${(@f)$(range_products 168)}")
  cursor="$(read_epoch "${SCHEDULER_STATE}/archive-cursor")"
  for offset in {0..3}; do
    index=$(( (cursor + offset) % ${#products} + 1 ))
    product="${products[${index}]}"
    token="$(latest_token "${product}" 168 || true)"
    task_id="archive-${product}"
    if [[ -z "${token}" ]] || ! batch_due 168 "${token}" "${now_epoch}" "${task_id}"; then
      continue
    fi
    mark_attempt "${task_id}" "${now_epoch}"
    mark_dirty
    log_message "Building bounded 168h archive unit for ${product}."
    run_archive_worker "${product}"
    archive_worker_pid="${STARTED_COMMAND_PID}"
    ACTIVE_VIDEO_PIDS=("${archive_worker_pid}")
    arm_deadline_watchdog "${product} archive-video worker" "${archive_worker_pid}"
    wait "${archive_worker_pid}" || archive_result=$?
    finish_deadline_watchdog
    ACTIVE_VIDEO_PIDS=()
    if (( archive_result == 0 )); then
      mark_success "${task_id}" "${token}" "${now_epoch}"
    fi
    atomic_state "${SCHEDULER_STATE}/archive-cursor" "$(( index % ${#products} ))"
    return "${archive_result}"
  done
  return 2
}

prune_quiescent() {
  local now_epoch last_prune
  now_epoch="$(date +%s)"
  last_prune="$(read_epoch "${SCHEDULER_STATE}/last-prune-epoch")"
  if (( now_epoch - last_prune < PRUNE_INTERVAL_SECONDS )); then
    return 0
  fi
  log_message "Pruning unreferenced composite cache and media while the worker is quiescent."
  if ! run_driver "${COMPOSITE_BUILDER}" \
    --source-root "${OUTPUT_ROOT}" \
    --output-root "${OUTPUT_ROOT}" \
    --prune-cache-only; then
    return 1
  fi
  # Sidecar manifest retention runs first. The shared-media dependency scan
  # now includes both legacy and sidecar manifests, so it removes only MP4s
  # whose generation has safely fallen out of current+previous retention.
  "${PYTHON_BIN}" "${LEGACY_BUILDER}" \
    --source-root "${OUTPUT_ROOT}" \
    --output-root "${OUTPUT_ROOT}" \
    --prune-shared-only
  atomic_state "${SCHEDULER_STATE}/last-prune-epoch" "$(date +%s)"
}

# A previous build may have completed while its publication lock was busy.
# Publish that immutable generation first; never waste CPU rebuilding it.
if ! publish_dirty; then
  exit 1
fi

ran_batch=0
while true; do
  now_epoch="$(date +%s)"
  if (( now_epoch - started_epoch >= MAX_RUNTIME_SECONDS )); then
    log_message "Scheduler time budget reached; remaining lower-priority work stays coalesced."
    break
  fi
  range=""
  products=()
  # Re-evaluate after every bounded range. Newly arrived 3h work therefore
  # preempts 6/12/24h work before another lower-priority unit can start.
  for candidate in 3 6 12 24; do
    candidate_products=("${(@f)$(range_products "${candidate}")}")
    due_products=()
    SELECTED_TOKENS=()
    for candidate_product in "${candidate_products[@]}"; do
      candidate_token="$(latest_token "${candidate_product}" "${candidate}" || true)"
      if [[ -n "${candidate_token}" ]] \
        && batch_due \
          "${candidate}" \
          "${candidate_token}" \
          "${now_epoch}" \
          "exact-${candidate}-${candidate_product}"; then
        due_products+=("${candidate_product}")
        SELECTED_TOKENS[${candidate_product}]="${candidate_token}"
      fi
    done
    if (( ${#due_products} )); then
      range="${candidate}"
      products=("${due_products[@]}")
      break
    fi
  done
  [[ -n "${range}" ]] || break
  ran_batch=1
  batch_status=0
  run_batch "${range}" "${now_epoch}" "${products[@]}" || batch_status=$?
  # Publication is range-batched: immutable assets precede catalog pointers.
  # A warning still exposes successful profiles and retains failed last-good.
  if ! publish_dirty; then
    exit 1
  fi
  if (( batch_status != 0 )); then
    # Retry this incomplete high-priority range after a short backoff rather
    # than spending the rest of the cycle on lower-priority archive work.
    exit "${batch_status}"
  fi
done

now_epoch="$(date +%s)"
if (( now_epoch - started_epoch < MAX_RUNTIME_SECONDS )); then
  hybrid_status=0
  run_one_hybrid_profile "${now_epoch}" || hybrid_status=$?
  if (( hybrid_status != 2 )); then
    ran_batch=1
    if ! publish_dirty; then
      exit 1
    fi
    (( hybrid_status == 0 )) || exit "${hybrid_status}"
  fi
fi

now_epoch="$(date +%s)"
if (( now_epoch - started_epoch < MAX_RUNTIME_SECONDS )); then
  archive_status=0
  run_one_archive_product "${now_epoch}" || archive_status=$?
  if (( archive_status != 2 )); then
    ran_batch=1
    if ! publish_dirty; then
      exit 1
    fi
    (( archive_status == 0 )) || exit "${archive_status}"
  fi
fi

if (( ran_batch )); then
  prune_quiescent || log_message "Deferred pruning failed; media publication remains valid."
fi
