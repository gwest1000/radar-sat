#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal

from radarsat.composite_video import (
    build_composite_videos,
    prune_composite_frame_cache,
    prune_composite_sidecar_manifests,
)
from radarsat.config import VIDEO_COMPOSITE_PRESETS, VIDEO_EXACT_RANGES
from radarsat.paths import output_root as default_output_root
from radarsat.video import VIDEO_PROFILES, _composite_presets


OPERATIONAL_PROFILES = tuple(
    spec for spec in VIDEO_PROFILES if _composite_presets(spec)
)


def _terminate(signum: int, _frame: object) -> None:
    # Raising through subprocess.run makes Python terminate and reap its active
    # ffmpeg child before the scheduler releases the global worker lock.
    raise SystemExit(128 + signum)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build independent exact-range operational composite MP4 sidecars."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=default_output_root(),
        help="Existing Radar-Sat processed archive containing catalog inputs.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Artifact destination; defaults to the source root.",
    )
    parser.add_argument(
        "--product",
        action="append",
        choices=sorted({spec.product_id for spec in OPERATIONAL_PROFILES}),
        help="Product to build; repeat to select several (default: all).",
    )
    parser.add_argument(
        "--layer",
        action="append",
        choices=sorted({spec.layer_id for spec in OPERATIONAL_PROFILES}),
        help="Default satellite layer to build; repeat to select several (default: all).",
    )
    parser.add_argument(
        "--track",
        action="append",
        choices=("live", "day"),
        help="Track to build; live owns 3/6/12 h and day owns 24 h.",
    )
    parser.add_argument(
        "--range-hours",
        action="append",
        type=int,
        choices=sorted({value for values in VIDEO_EXACT_RANGES.values() for value in values}),
        help="Exact range to build; repeat to select several.",
    )
    parser.add_argument(
        "--preset",
        action="append",
        choices=sorted(
            {
                str(value["id"])
                for values in VIDEO_COMPOSITE_PRESETS.values()
                for value in values
            }
        ),
        help=(
            "Composite preset to build; repeat to select several. This lets "
            "the scheduler publish exact loops before lower-priority hybrids."
        ),
    )
    parser.add_argument(
        "--ffmpeg",
        help="Explicit ffmpeg executable; defaults to PATH discovery.",
    )
    parser.add_argument(
        "--defer-cache-prune",
        action="store_true",
        help="Defer the local unpublished composite-frame cache prune.",
    )
    parser.add_argument(
        "--prune-cache-only",
        action="store_true",
        help="Prune expired local composite frame-cache entries and exit.",
    )
    parser.add_argument(
        "--cache-max-age-hours",
        type=float,
        default=36.0,
        help="Maximum unused composite-frame cache age (default: 36 hours).",
    )
    args = parser.parse_args(argv)
    if args.cache_max_age_hours <= 0:
        parser.error("--cache-max-age-hours must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)
    args = parse_args(argv)
    output_root = (args.output_root or args.source_root).resolve()
    if args.prune_cache_only:
        removed = prune_composite_frame_cache(
            output_root,
            max_age_hours=args.cache_max_age_hours,
        )
        removed_manifests = prune_composite_sidecar_manifests(output_root)
        print(json.dumps({
            "status": "ok",
            "prunedCacheFrames": removed,
            "prunedManifests": removed_manifests,
        }, indent=2))
        return 0
    result = build_composite_videos(
        args.source_root,
        args.output_root,
        product_ids=args.product,
        layer_ids=args.layer,
        track_names=args.track,
        ranges=args.range_hours,
        preset_ids=args.preset,
        ffmpeg=args.ffmpeg,
        prune_cache=not args.defer_cache_prune,
    )
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
