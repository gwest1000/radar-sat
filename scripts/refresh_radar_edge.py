#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from radarsat.config import DOMAINS
from radarsat.geomet import GeoMetClient
from radarsat.paths import output_root as default_output_root
from radarsat.pipeline import ingest_geomet


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh only latency-sensitive radar layers.")
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    parser.add_argument("--hours", type=float, default=1.0)
    args = parser.parse_args()
    domain_ids = ("bc", "north-america", "north-pacific")
    results: dict[str, object] = {}

    def render(domain_id: str) -> tuple[str, object]:
        with GeoMetClient() as client:
            timelines = ingest_geomet(
                client,
                args.output_root,
                DOMAINS[domain_id],
                args.hours,
                True,
                include_layers=("radar-rain", "radar-coverage"),
            )
        return domain_id, {
            layer_id: [value.isoformat().replace("+00:00", "Z") for value in values]
            for layer_id, values in timelines.items()
        }

    with ThreadPoolExecutor(max_workers=len(domain_ids)) as executor:
        futures = [executor.submit(render, domain_id) for domain_id in domain_ids]
        for future in as_completed(futures):
            domain_id, status = future.result()
            results[domain_id] = status
    print(json.dumps({"status": "ok", "domains": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
