#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.r2 import R2Config, publish, write_publish_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a validated Radar-Sat archive to Cloudflare R2."
    )
    parser.add_argument("--root", type=Path, default=Path("data/output"))
    parser.add_argument(
        "--state-path", type=Path, default=Path("var/state/r2-publish.sqlite3")
    )
    parser.add_argument(
        "--status-path", type=Path, default=Path("var/status/publish.json")
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="Keep expired remote frame objects (the bucket lifecycle remains a backstop).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Use the durable successful-upload index instead of listing the "
            "entire bucket; a regular archive publication still reconciles R2."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = publish(
            args.root,
            R2Config.from_environment(),
            args.state_path,
            args.status_path,
            sync_delete=not args.no_delete,
            fast=args.fast,
            dry_run=args.dry_run,
        )
    except Exception as error:
        write_publish_error(args.status_path, error)
        raise
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
