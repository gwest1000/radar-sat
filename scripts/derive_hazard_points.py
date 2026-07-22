#!/usr/bin/env python3
"""Backfill compact hazard point frames from retained display PNGs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.catalog import write_catalog
from radarsat.config import DOMAINS
from radarsat.point_migration import derive_hazard_point_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Derive GLM and CWFIS point-frame JSON from retained PNGs without "
            "downloading NOAA or NRCan source data. Existing point targets are "
            "preserved unless --overwrite is explicit."
        )
    )
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--domain", action="append", choices=sorted(DOMAINS), default=[])
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    domains = args.domain or list(DOMAINS)
    output_root = args.output_root.resolve()
    result = derive_hazard_point_archive(
        output_root,
        domains,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        result["catalog"] = write_catalog(output_root).relative_to(output_root).as_posix()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
