#!/usr/bin/env python3
"""Atomically rebuild the Radar-Sat runtime catalog."""

from __future__ import annotations

import argparse
from pathlib import Path

from radarsat.catalog import write_catalog
from radarsat.paths import output_root as default_output_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    args = parser.parse_args()
    destination = write_catalog(args.output_root.resolve())
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
