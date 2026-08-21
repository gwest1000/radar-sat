#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal

from radarsat.paths import output_root as default_output_root

from radarsat.video import (
    VIDEO_PROFILES,
    build_satellite_videos,
    prune_shared_video_orphans,
)


def _terminate(signum: int, _frame: object) -> None:
    # Let subprocess.run unwind so an active ffmpeg process is killed/reaped.
    raise SystemExit(128 + signum)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build shared H.264 satellite loops and display-resolution overlay proxies."
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
        help="Artifact destination; defaults to the source root for production.",
    )
    parser.add_argument(
        "--product",
        action="append",
        choices=sorted({spec.product_id for spec in VIDEO_PROFILES}),
        help="Product to build; repeat to select several (default: all products).",
    )
    parser.add_argument(
        "--track",
        action="append",
        choices=("live", "day", "archive"),
        help="Track to build; repeat to select both (default: both tracks).",
    )
    parser.add_argument(
        "--layer",
        action="append",
        choices=sorted({spec.layer_id for spec in VIDEO_PROFILES}),
        help="Satellite layer to build; repeat to select several (default: all layers).",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Live history to encode, capped at 24 hours.",
    )
    parser.add_argument(
        "--archive-hours",
        type=float,
        default=168.0,
        help="Hourly archive history to encode, capped at 168 hours.",
    )
    parser.add_argument(
        "--ffmpeg",
        help="Explicit ffmpeg executable; defaults to PATH discovery.",
    )
    parser.add_argument(
        "--defer-shared-prune",
        action="store_true",
        help="defer the shared media scan until all parallel workers finish",
    )
    parser.add_argument(
        "--prune-shared-only",
        action="store_true",
        help="only prune unreferenced shared media and exit",
    )
    args = parser.parse_args(argv)
    if args.hours <= 0 or args.archive_hours <= 0:
        parser.error("--hours and --archive-hours must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)
    args = parse_args(argv)
    output_root = (args.output_root or args.source_root).resolve()
    if args.prune_shared_only:
        removed = prune_shared_video_orphans(output_root)
        print(json.dumps({"status": "ok", "removedSharedDependencies": removed}, indent=2))
        return 0
    result = build_satellite_videos(
        args.source_root,
        args.output_root,
        product_ids=args.product,
        layer_ids=args.layer,
        track_names=args.track,
        ffmpeg=args.ffmpeg,
        hours=min(args.hours, 24.0),
        archive_hours=min(args.archive_hours, 168.0),
        prune_shared_assets=not args.defer_shared_prune,
    )
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
