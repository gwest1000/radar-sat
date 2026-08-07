#!/usr/bin/env python3
"""Build bounded WestWX raster-tile pyramids for recent archive frames."""

from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path

from radarsat.raster_tiles import PROFILES, RASTER_PROFILES, build_profiles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--hours", type=float, default=3)
    parser.add_argument("--max-frames", type=int, default=3)
    parser.add_argument(
        "--layer",
        action="append",
        default=[],
        help="Optional domain:layer profile; may be repeated.",
    )
    args = parser.parse_args()
    if args.hours <= 0 or args.max_frames <= 0:
        parser.error("hours and max-frames must be positive")
    return args


def main() -> int:
    args = parse_args()
    profiles = []
    for value in args.layer:
        try:
            domain_id, layer_id = value.split(":", 1)
            profiles.append(PROFILES[(domain_id, layer_id)])
        except (ValueError, KeyError):
            raise SystemExit(f"Unknown tile profile: {value}")
    if not profiles:
        profiles = list(RASTER_PROFILES)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    # Full-disk and rapid-BC workers run independently. A shared advisory lock
    # prevents their tile cleanup/generation phases from racing each other.
    lock_path = output_root / ".raster-tiles.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        results = build_profiles(
            output_root,
            profiles,
            hours=args.hours,
            max_frames=args.max_frames,
        )
    print(json.dumps({"status": "ok", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
