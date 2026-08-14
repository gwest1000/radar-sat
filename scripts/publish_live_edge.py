#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.live_edge import build_live_edge_index, publish_live_edge
from radarsat.paths import output_root as default_output_root
from radarsat.r2 import R2Config


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish the small radar/lightning live-edge index.")
    parser.add_argument("--root", type=Path, default=default_output_root())
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        payload, objects = build_live_edge_index(args.root)
        result: object = {
            "status": "dry-run",
            "generatedAt": payload["generatedAt"],
            "domains": sorted(payload["domains"]),
            "objects": len(objects),
            "bytes": sum(item.size for item in objects),
        }
    else:
        result = publish_live_edge(
            args.root,
            R2Config.from_environment(),
            state_path=args.state_path,
        )
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
