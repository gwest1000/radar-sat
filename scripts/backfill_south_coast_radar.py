#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.nexrad_hybrid import derive_south_coast_hybrid_radar
from radarsat.paths import output_root as default_output_root


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the stage-aligned ECCC/KATX/KLGX South Coast radar archive."
    )
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    parser.add_argument("--hours", type=float, default=168.0)
    parser.add_argument("--latest-only", action="store_true")
    args = parser.parse_args()
    result = derive_south_coast_hybrid_radar(
        args.output_root,
        hours=args.hours,
        latest_only=args.latest_only,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in {"ok", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
