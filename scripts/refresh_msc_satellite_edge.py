#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.config import DOMAINS
from radarsat.paths import output_root as default_output_root
from radarsat.spool import discover_spool, ingest_spool


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render only the newest MSC GeoColor frame for the BC display."
    )
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    parser.add_argument(
        "--spool-root",
        type=Path,
        default=Path.home() / ".local/share/radar-sat/spool/eccc",
    )
    parser.add_argument("--spool-hours", type=float, default=1.0)
    args = parser.parse_args()

    discovered = discover_spool(args.spool_root)
    result = ingest_spool(
        args.spool_root,
        args.output_root,
        DOMAINS["bc"],
        args.spool_hours,
        True,
        include_layers=("eccc-geocolor",),
        discovered=discovered,
    )
    print(json.dumps({"status": "ok", "bc": result.status()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
