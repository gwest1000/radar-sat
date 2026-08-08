#!/usr/bin/env python3
"""Render hourly HRDPS and time-interpolated ECMWF synoptic overlays."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from radarsat.ecmwf_contours import DEFAULT_DATA_ROOT as DEFAULT_ECMWF_ROOT
from radarsat.ecmwf_contours import update_recent as update_ecmwf
from radarsat.hrdps_contours import DEFAULT_DATA_ROOT as DEFAULT_HRDPS_ROOT
from radarsat.hrdps_contours import UTC, update_recent as update_hrdps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--hrdps-root", type=Path, default=DEFAULT_HRDPS_ROOT)
    parser.add_argument("--ecmwf-root", type=Path, default=DEFAULT_ECMWF_ROOT)
    parser.add_argument("--hours", type=int, default=12)
    parser.add_argument("--now", help="UTC ISO timestamp used for deterministic backfills")
    parser.add_argument("--no-download", action="store_true", help="Do not fetch missing HRDPS fields")
    args = parser.parse_args()
    now = (
        dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else None
    )
    output_root = args.output_root.resolve()
    results = {
        "hrdps": update_hrdps(
            output_root,
            args.hrdps_root.resolve(),
            hours=max(0, args.hours),
            domain_ids=("bc",),
            now=now,
            download=not args.no_download,
        ),
        "ecmwf": update_ecmwf(
            output_root,
            args.ecmwf_root.resolve(),
            hours=max(0, args.hours),
            domain_ids=("north-america", "north-pacific"),
            now=now,
        ),
    }
    print(json.dumps(results, indent=2))
    statuses = [item["status"] for model in results.values() for item in model]
    return 0 if any(status in {"rendered", "unchanged"} for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
