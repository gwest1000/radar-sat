#!/usr/bin/env python3
"""Plan or run a bounded NOAA/NESDIS/STAR CIRA GeoColor backfill."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

from radarsat.geomet import format_utc
from radarsat.noaa_star_geocolor import (
    DEFAULT_MAX_SOURCE_BYTES,
    SECTORS,
    StarClient,
    discover_scans,
    execute_backfill,
    plan_backfill,
    render_scan,
)


UTC = dt.timezone.utc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sector", choices=sorted(SECTORS), required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--cache-root", type=Path, default=Path("var/cache/noaa-star-geocolor"))
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--max-frames", type=int, default=2)
    parser.add_argument("--max-download-gb", type=float, default=0.25)
    parser.add_argument(
        "--max-source-mb",
        type=float,
        default=DEFAULT_MAX_SOURCE_BYTES / 1_000_000,
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--defer-catalog",
        action="store_true",
        help="Defer catalog construction so the coordinating cycle can rebuild it once.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if (
        args.hours <= 0
        or args.max_frames <= 0
        or args.max_download_gb <= 0
        or args.max_source_mb <= 0
    ):
        parser.error("hours, frames and byte limits must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output_root.resolve()
    cache_root = args.cache_root.resolve() / args.sector
    sector = SECTORS[args.sector]
    end = dt.datetime.now(UTC)
    start = end - dt.timedelta(hours=args.hours)
    with StarClient() as client:
        discovery = discover_scans(client, sector, start, end)
        plan = plan_backfill(
            output_root,
            discovery,
            max_frames=args.max_frames,
            max_download_bytes=int(args.max_download_gb * 1_000_000_000),
            overwrite=args.overwrite,
        )
        payload: dict[str, object] = {
            "status": "planned" if args.apply else "dry-run",
            "sector": sector.id,
            "windowStart": format_utc(start),
            "windowEnd": format_utc(end),
            "plannedFrames": len(plan.scans),
            "estimatedBytes": plan.estimated_bytes,
            "skippedReady": plan.skipped_ready,
            "excludedByFrameLimit": plan.excluded_by_frame_limit,
            "excludedByByteLimit": plan.excluded_by_byte_limit,
            "warnings": list(plan.warnings),
            "scans": [
                {
                    "validTime": format_utc(scan.valid_time),
                    "sourceTime": format_utc(scan.source_time),
                    "bytes": scan.size,
                    "source": scan.url,
                }
                for scan in plan.scans
            ],
        }
        if args.apply:
            result = execute_backfill(
                output_root,
                plan,
                lambda scan: render_scan(
                    output_root,
                    scan,
                    client,
                    cache_root,
                    max_source_bytes=int(args.max_source_mb * 1_000_000),
                    overwrite=args.overwrite,
                ),
                rebuild_catalog=not args.defer_catalog,
            )
            payload.update(result.as_dict())
    if args.apply:
        status = output_root / "status" / f"noaa-star-{sector.id}-geocolor-backfill.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        temporary = status.with_name(f"{status.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(status)
    print(json.dumps(payload, indent=2))
    return 1 if payload.get("status") == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
