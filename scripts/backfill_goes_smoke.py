#!/usr/bin/env python3
"""Backfill the last 24 hours of GOES-18 ADPF smoke for North America."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.catalog import write_catalog
from radarsat.pipeline import ingest_goes_smoke_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Catch up the bounded 24-hour GOES-18 ADPF smoke archive for the "
            "north-america domain, then atomically refresh catalog.json."
        )
    )
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = args.output_root.resolve()
    result = ingest_goes_smoke_archive(output_root, ["north-america"])
    result["catalog"] = write_catalog(output_root).relative_to(output_root).as_posix()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
