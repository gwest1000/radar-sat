#!/usr/bin/env python3
"""Create a same-origin last-good browser catalog from an operational catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.r2 import build_catalog_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--asset-base-url", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = json.loads(build_catalog_index(args.source.read_bytes()))
    if not catalog.get("products") or not catalog.get("domains"):
        raise ValueError("Bootstrap catalog source contains no products or domains")
    catalog["assetBaseUrl"] = args.asset_base_url.rstrip("/") + "/"
    payload = json.dumps(catalog, indent=2) + "\n"
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.destination.with_suffix(args.destination.suffix + ".tmp")
    try:
        temporary.write_text(payload)
        temporary.replace(args.destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
