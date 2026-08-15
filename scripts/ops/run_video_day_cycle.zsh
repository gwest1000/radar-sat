#!/bin/zsh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export RADARSAT_VIDEO_TRACK=day
exec "${PROJECT_ROOT}/scripts/ops/run_video_cycle.zsh"
