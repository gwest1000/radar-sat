#!/usr/bin/env python3
"""Run a bounded daylight-only native-resolution GOES-18 BC backfill."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from radarsat.geomet import format_utc
from radarsat.native_bc_satellite import (
    execute_native_bc_backfill,
    discover_native_bc_scans,
    plan_native_bc_backfill,
    render_native_bc_scan,
)
from radarsat.raw_satellite import PublicSatelliteClient


UTC = dt.timezone.utc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--cache-root", type=Path, default=Path("var/cache/native-bc-satellite"))
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--max-frames", type=int, default=1)
    parser.add_argument("--max-download-gb", type=float, default=0.7)
    parser.add_argument("--max-source-mb", type=float, default=700.0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.hours <= 0 or args.max_frames <= 0 or args.max_download_gb <= 0 or args.max_source_mb <= 0:
        parser.error("hours, frames and byte limits must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output_root.resolve()
    cache_root = args.cache_root.resolve()
    end = dt.datetime.now(UTC)
    start = end - dt.timedelta(hours=args.hours)
    with PublicSatelliteClient() as client:
        discovery = discover_native_bc_scans(client, start, end)
        plan = plan_native_bc_backfill(
            output_root,
            discovery,
            max_frames=args.max_frames,
            max_download_bytes=int(args.max_download_gb * 1_000_000_000),
            overwrite=args.overwrite,
        )
        payload: dict[str, object] = {
            "status": "planned" if args.apply else "dry-run",
            "windowStart": format_utc(start),
            "windowEnd": format_utc(end),
            "plannedFrames": len(plan.scans),
            "estimatedBytes": plan.estimated_bytes,
            "skippedReady": plan.skipped_ready,
            "skippedNight": plan.skipped_night,
            "excludedByFrameLimit": plan.excluded_by_frame_limit,
            "excludedByByteLimit": plan.excluded_by_byte_limit,
            "warnings": list(plan.warnings),
        }
        if args.apply:
            result = execute_native_bc_backfill(
                output_root,
                plan,
                lambda scan: render_native_bc_scan(
                    output_root,
                    scan,
                    client,
                    cache_root,
                    max_source_bytes=int(args.max_source_mb * 1_000_000),
                    overwrite=args.overwrite,
                ),
            )
            payload.update(result.as_dict())
    status = output_root / "status" / "native-bc-satellite-backfill.json"
    if args.apply:
        status.parent.mkdir(parents=True, exist_ok=True)
        temporary = status.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(status)
    print(json.dumps(payload, indent=2))
    return 1 if payload.get("status") == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
