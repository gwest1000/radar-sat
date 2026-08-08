#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.video import VIDEO_PROFILES, build_satellite_videos


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build shared H.264 satellite loops and display-resolution overlay proxies."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("data/output"),
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
    args = parser.parse_args(argv)
    if args.hours <= 0 or args.archive_hours <= 0:
        parser.error("--hours and --archive-hours must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_satellite_videos(
        args.source_root,
        args.output_root,
        product_ids=args.product,
        ffmpeg=args.ffmpeg,
        hours=min(args.hours, 24.0),
        archive_hours=min(args.archive_hours, 168.0),
    )
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
