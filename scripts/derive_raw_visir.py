#!/usr/bin/env python3
"""Backfill raw-visir frames from the existing compressed satellite archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.catalog import write_catalog
from radarsat.config import DOMAINS
from radarsat.pipeline import derive_raw_visir_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Derive solar-blended raw-visir WebPs from archived raw-visible/raw-ir pairs "
            "without downloading NOAA source files."
        )
    )
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--domain", action="append", choices=sorted(DOMAINS), default=[])
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    domains = args.domain or list(DOMAINS)
    output_root = args.output_root.resolve()
    result = derive_raw_visir_archive(
        output_root,
        domains,
        overwrite=args.overwrite,
    )
    result["catalog"] = write_catalog(output_root).relative_to(output_root).as_posix()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
