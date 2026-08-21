#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict
import json

from radarsat.r2 import R2Config, boto3_client, list_remote_inventory


def main() -> int:
    config = R2Config.from_environment()
    inventory = list_remote_inventory(boto3_client(config), config.bucket)
    prefixes: dict[str, dict[str, int]] = defaultdict(lambda: {"objects": 0, "bytes": 0})
    video_profiles: dict[str, dict[str, int]] = defaultdict(lambda: {"objects": 0, "bytes": 0})
    for key, size in inventory.sizes.items():
        prefix = key.split("/", 1)[0]
        prefixes[prefix]["objects"] += 1
        prefixes[prefix]["bytes"] += size
        if prefix in {"videos", "video-segments", "video-manifests"}:
            parts = key.split("/")
            owner = parts[1] if len(parts) > 1 else "unknown"
            video_profiles[owner]["objects"] += 1
            video_profiles[owner]["bytes"] += size
    print(json.dumps({
        "bucket": config.bucket,
        "objects": len(inventory.sizes),
        "bytes": sum(inventory.sizes.values()),
        "prefixes": dict(sorted(prefixes.items())),
        "videoOwners": dict(sorted(video_profiles.items())),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
