#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.health import inspect_health
from radarsat.pipeline import write_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Radar-Sat ingest and R2 health.")
    parser.add_argument("--root", type=Path, default=Path("data/output"))
    parser.add_argument(
        "--publish-status", type=Path, default=Path("var/status/publish.json")
    )
    parser.add_argument(
        "--status-path", type=Path, default=Path("var/status/health.json")
    )
    parser.add_argument("--max-service-age-minutes", type=int, default=15)
    parser.add_argument("--skip-publish", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = inspect_health(
        args.root,
        args.publish_status,
        require_publish=not args.skip_publish,
        service_max_age_minutes=args.max_service_age_minutes,
    )
    write_status(args.status_path, result)
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
