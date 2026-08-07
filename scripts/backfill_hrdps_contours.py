#!/usr/bin/env python3
"""Render hourly HRDPS 500-hPa height and MSLP overlays for Radar-Sat."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from radarsat.hrdps_contours import DEFAULT_DATA_ROOT, UTC, update_recent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--hours", type=int, default=12)
    parser.add_argument("--domain", action="append", dest="domains")
    parser.add_argument("--now", help="UTC ISO timestamp used for deterministic backfills")
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()
    now = (
        dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        if args.now
        else None
    )
    results = update_recent(
        args.output_root.resolve(),
        args.data_root.resolve(),
        hours=max(0, args.hours),
        domain_ids=args.domains or ("bc", "north-america", "north-pacific"),
        now=now,
        download=not args.no_download,
    )
    print(json.dumps(results, indent=2))
    return 0 if any(item["status"] in {"rendered", "unchanged"} for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
