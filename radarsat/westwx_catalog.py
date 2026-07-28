"""Build the compact catalog consumed by the WestWX web and mobile clients."""

from __future__ import annotations

from typing import Any, Mapping


WESTWX_LAYERS = {
    "north-america": {
        "raw-visible",
        "raw-ir",
        "westwx-ir",
        "raw-visir",
        "vis-ir",
        "westwx-visir",
        "radar-rain",
        "smoke",
        "smoke-mask",
        "adp-smoke",
        "glm-lightning-points",
        "hotspot-points",
        "active-fire-points",
    },
    "bc": {
        "raw-visir-5min",
        "raw-ir",
        "lightning-points",
        "ptype",
    },
}
FRAME_FIELDS = (
    "validTime",
    "path",
    "availability",
    "validPixelCount",
    "lowConfidencePixels",
    "mediumConfidencePixels",
    "highConfidencePixels",
)
TILE_FIELDS = (
    "template",
    "bounds",
    "minZoom",
    "maxZoom",
    "tileSize",
    "format",
    "encoding",
)


def build_westwx_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the frame fields and layers WestWX can actually display."""
    domains: dict[str, Any] = {}
    source_domains = catalog.get("domains")
    if not isinstance(source_domains, Mapping):
        source_domains = {}

    for domain_id, allowed_layers in WESTWX_LAYERS.items():
        source_domain = source_domains.get(domain_id)
        source_layers = (
            source_domain.get("layers")
            if isinstance(source_domain, Mapping)
            else {}
        )
        if not isinstance(source_layers, Mapping):
            source_layers = {}
        layers: dict[str, Any] = {}
        for layer_id in sorted(allowed_layers):
            source_layer = source_layers.get(layer_id)
            source_frames = (
                source_layer.get("frames")
                if isinstance(source_layer, Mapping)
                else []
            )
            if not isinstance(source_frames, list):
                continue
            frames: list[dict[str, Any]] = []
            for source_frame in source_frames:
                if not isinstance(source_frame, Mapping):
                    continue
                frame = {
                    field: source_frame[field]
                    for field in FRAME_FIELDS
                    if field in source_frame
                }
                tiles = source_frame.get("tiles")
                if isinstance(tiles, Mapping):
                    frame["tiles"] = {
                        field: tiles[field]
                        for field in TILE_FIELDS
                        if field in tiles
                    }
                if isinstance(frame.get("validTime"), str) and isinstance(frame.get("path"), str):
                    frames.append(frame)
            if frames:
                layers[layer_id] = {"frames": frames}
        domains[domain_id] = {"layers": layers}

    return {
        "schemaVersion": 1,
        "generatedAt": catalog.get("generatedAt"),
        "domains": domains,
    }
