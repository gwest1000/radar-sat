#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from radarsat.config import DOMAINS
from radarsat.paths import output_root as default_output_root
from radarsat.pipeline import derive_lightning_trails, ingest_glm_live
from radarsat.spool import discover_spool, ingest_spool


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh only latency-sensitive lightning layers.")
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    parser.add_argument(
        "--spool-root",
        type=Path,
        default=Path.home() / ".local/share/radar-sat/spool/eccc",
    )
    parser.add_argument("--spool-hours", type=float, default=1.0)
    args = parser.parse_args()
    domains = [DOMAINS[value] for value in ("bc", "north-america", "north-pacific")]
    results: dict[str, object] = {}
    discovered = discover_spool(args.spool_root)

    def render_eccc(domain_id: str) -> tuple[str, object]:
        domain = DOMAINS[domain_id]
        result = ingest_spool(
            args.spool_root,
            args.output_root,
            domain,
            args.spool_hours,
            True,
            include_layers=("lightning",),
            discovered=discovered,
        )
        derive_lightning_trails(args.output_root, domain, result.timelines, 24.0)
        return domain_id, result.status()

    with ThreadPoolExecutor(max_workers=len(domains)) as executor:
        futures = [executor.submit(render_eccc, domain.id) for domain in domains]
        for future in as_completed(futures):
            domain_id, status = future.result()
            results[domain_id] = status

    glm_status = ingest_glm_live(
        args.output_root,
        [domain.id for domain in domains],
    )
    print(json.dumps({"status": "ok", "eccc": results, "glm": glm_status}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
