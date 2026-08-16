#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from radarsat.config import DOMAINS, LAYERS
from radarsat.geomet import GeoMetClient, LayerTimeline, format_utc
from radarsat.nexrad_hybrid import derive_south_coast_hybrid_radar
from radarsat.paths import output_root as default_output_root
from radarsat.pipeline import ingest_geomet


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh only latency-sensitive radar layers.")
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    parser.add_argument("--hours", type=float, default=1.0)
    args = parser.parse_args()
    domain_ids = ("bc", "north-america", "north-pacific")
    layer_ids = ("radar-rain", "radar-coverage")
    results: dict[str, object] = {}
    shared_errors: list[str] = []
    preloaded_timelines: dict[str, LayerTimeline] = {}

    # A layer timeline does not depend on the output domain. Fetch each one
    # once instead of making six simultaneous GetCapabilities requests every
    # two minutes. This also keeps all domains on the same advertised source
    # clock for a cycle.
    with GeoMetClient() as timeline_client:
        for layer_id in layer_ids:
            source_layer = LAYERS[layer_id].source_layer
            if source_layer is None:
                continue
            try:
                preloaded_timelines[source_layer] = timeline_client.timeline(source_layer)
            except Exception as error:
                shared_errors.append(
                    f"{layer_id} timeline: {type(error).__name__}: {error}"
                )
    available_layers = tuple(
        layer_id
        for layer_id in layer_ids
        if LAYERS[layer_id].source_layer in preloaded_timelines
    )

    def render(domain_id: str) -> tuple[str, object, bool]:
        errors: list[str] = []
        with GeoMetClient() as client:
            timelines = ingest_geomet(
                client,
                args.output_root,
                DOMAINS[domain_id],
                args.hours,
                True,
                include_layers=available_layers,
                preloaded_timelines=preloaded_timelines,
                continue_on_error=True,
                errors=errors,
            )
        status: dict[str, object] = {
            layer_id: {
                "count": len(values),
                "latest": format_utc(values[-1]) if values else None,
            }
            for layer_id, values in timelines.items()
        }
        if domain_id == "bc":
            status["southCoastHybrid"] = derive_south_coast_hybrid_radar(
                args.output_root,
                hours=args.hours,
                latest_only=True,
            )
        status["errors"] = errors
        radar_failed = any(
            value.startswith(f"{domain_id}/radar-rain ")
            or value.startswith(f"{domain_id}/radar-rain timeline:")
            for value in errors
        )
        radar_available = "radar-rain" in available_layers and not radar_failed
        return domain_id, status, radar_available

    radar_successes = 0
    with ThreadPoolExecutor(max_workers=len(domain_ids)) as executor:
        futures = [executor.submit(render, domain_id) for domain_id in domain_ids]
        for future in as_completed(futures):
            try:
                domain_id, status, radar_available = future.result()
            except Exception as error:
                shared_errors.append(
                    f"domain worker: {type(error).__name__}: {error}"
                )
                continue
            results[domain_id] = status
            radar_successes += int(radar_available)
    all_errors = [
        *shared_errors,
        *(
            str(error)
            for status in results.values()
            if isinstance(status, dict)
            for error in status.get("errors", [])
        ),
    ]
    print(json.dumps({
        "status": "ok" if not all_errors else "warning",
        "radarDomainsSucceeded": radar_successes,
        "errors": all_errors,
        "domains": results,
    }, indent=2))
    # Partial success is useful and will be published. A total rain-rate
    # failure remains non-zero for launchd/monitoring, after the wrapper has
    # had a chance to publish any independent NEXRAD or coverage update.
    return 0 if radar_successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
