#!/usr/bin/env python3
"""Plan or run a bounded genuine 10-minute GOES-18 WestWX backfill."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from radarsat.geomet import format_utc
from radarsat.raw_satellite import PublicSatelliteClient
from radarsat.westwx_satellite import (
    DEFAULT_MAX_SOURCE_BYTES,
    BackfillResult,
    PlannedBackfill,
    discover_goes18_scans,
    execute_backfill,
    plan_backfill,
    render_westwx_scan,
)


UTC = dt.timezone.utc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover exact GOES-18 full-disk scan times and render a newest-first, "
            "bounded North America and BC rapid archive. The default is a dry run."
        )
    )
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path("var/cache/westwx-satellite"),
        help="Dedicated raw/render cache; source files are removed after every scan.",
    )
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument(
        "--max-frames",
        type=int,
        default=1,
        help="Hard frame ceiling. Set this explicitly for a multi-frame catch-up.",
    )
    parser.add_argument(
        "--max-download-gb",
        type=float,
        default=0.4,
        help="Hard decimal-GB ceiling for the complete run (default: 0.4).",
    )
    parser.add_argument(
        "--max-source-mb",
        type=float,
        default=DEFAULT_MAX_SOURCE_BYTES / 1_000_000,
        help="Reject any single unexpectedly large NOAA object (default: 400).",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Require a one-frame plan and report download/render timing.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Download and render the plan. Without this flag, only JSON is printed.",
    )
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be positive")
    if args.max_frames <= 0:
        parser.error("--max-frames must be positive")
    if args.max_download_gb <= 0:
        parser.error("--max-download-gb must be positive")
    if args.max_source_mb <= 0:
        parser.error("--max-source-mb must be positive")
    if args.benchmark and args.max_frames != 1:
        parser.error("--benchmark requires --max-frames 1")
    return args


def _plan_payload(
    plan: PlannedBackfill,
    *,
    start: dt.datetime,
    end: dt.datetime,
    apply: bool,
) -> dict[str, object]:
    return {
        "status": "planned" if apply else "dry-run",
        "apply": apply,
        "windowStart": format_utc(start),
        "windowEnd": format_utc(end),
        "plannedFrames": len(plan.scans),
        "estimatedBytes": plan.estimated_bytes,
        "estimatedDecimalGB": round(plan.estimated_bytes / 1_000_000_000, 3),
        "skippedReady": plan.skipped_ready,
        "excludedByFrameLimit": plan.excluded_by_frame_limit,
        "excludedByByteLimit": plan.excluded_by_byte_limit,
        "warnings": list(plan.warnings),
        "scans": [
            {
                "validTime": format_utc(scan.valid_time),
                "bytes": scan.size,
                "source": scan.url,
            }
            for scan in plan.scans
        ],
    }


def _benchmark_payload(result: BackfillResult) -> dict[str, object] | None:
    rendered = next((item for item in result.scans if item.status == "rendered"), None)
    if rendered is None:
        return None
    megabytes = rendered.source_bytes / 1_000_000
    return {
        "sourceMB": round(megabytes, 1),
        "downloadSeconds": round(rendered.download_seconds, 3),
        "downloadMBPerSecond": (
            round(megabytes / rendered.download_seconds, 2)
            if rendered.download_seconds > 0
            else None
        ),
        "renderSeconds": round(rendered.render_seconds, 3),
        "totalSeconds": round(rendered.download_seconds + rendered.render_seconds, 3),
    }


def _write_status(output_root: Path, payload: dict[str, object]) -> None:
    destination = output_root / "status" / "westwx-satellite-backfill.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(destination)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output_root.resolve()
    cache_root = args.cache_root.resolve()
    end = dt.datetime.now(UTC)
    start = end - dt.timedelta(hours=args.hours)
    maximum_download = int(args.max_download_gb * 1_000_000_000)
    maximum_source = int(args.max_source_mb * 1_000_000)

    with PublicSatelliteClient() as client:
        discovery = discover_goes18_scans(client, start, end)
        plan = plan_backfill(
            output_root,
            discovery,
            max_frames=args.max_frames,
            max_download_bytes=maximum_download,
            overwrite=args.overwrite,
        )
        plan_payload = _plan_payload(plan, start=start, end=end, apply=args.apply)
        if not args.apply:
            print(json.dumps(plan_payload, indent=2))
            return 0

        result = execute_backfill(
            output_root,
            plan,
            lambda scan: render_westwx_scan(
                output_root,
                scan,
                client,
                cache_root,
                max_source_bytes=maximum_source,
                overwrite=args.overwrite,
            ),
        )

    payload = {**plan_payload, **result.as_dict()}
    payload["windowStart"] = format_utc(start)
    payload["windowEnd"] = format_utc(end)
    if args.benchmark:
        payload["benchmark"] = _benchmark_payload(result)
    _write_status(output_root, payload)
    print(json.dumps(payload, indent=2))
    return 1 if result.status == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
