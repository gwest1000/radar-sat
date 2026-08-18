#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radarsat.config import DOMAINS
from radarsat.paths import output_root as default_output_root
from radarsat.pipeline import derive_fire_overlays, ingest_active_fire_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh only the newest agency-fire points and fire overlays."
    )
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    args = parser.parse_args()

    results: dict[str, object] = {}
    failures: list[str] = []
    for domain_id in ("bc", "north-america", "north-pacific"):
        domain = DOMAINS[domain_id]
        try:
            active = ingest_active_fire_snapshot(args.output_root, domain)
            overlay = derive_fire_overlays(
                args.output_root,
                domain,
                latest_only=True,
            )
            results[domain_id] = {"activeFires": active, "fireOverlay": overlay}
        except Exception as error:
            failures.append(f"{domain_id}: {type(error).__name__}: {error}")

    print(json.dumps({
        "status": "ok" if not failures else "warning",
        "domains": results,
        "failures": failures,
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
