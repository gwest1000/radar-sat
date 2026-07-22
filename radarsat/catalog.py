from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .config import DOMAINS, LAYERS, LEGENDS, PRODUCTS


UTC = dt.timezone.utc


def retention_policy(tier: str) -> dict[str, int]:
    """Describe the policy enforced locally and by the R2 expiry pass."""
    return {
        "allFramesHours": 24,
        "archiveDays": 7,
        "archiveCadenceMinutes": 30 if tier == "bc" else 60,
    }


def read_metadata(root: Path, domain_id: str, layer_id: str) -> list[dict[str, Any]]:
    directory = root / "metadata" / domain_id / layer_id
    frames: list[dict[str, Any]] = []
    if not directory.exists():
        return frames
    for path in sorted(directory.rglob("*.json")):
        try:
            frames.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    frames.sort(key=lambda item: item["validTime"])
    return frames


def build_catalog(root: Path) -> dict[str, Any]:
    domains: dict[str, Any] = {}
    for domain_id, domain in DOMAINS.items():
        layer_root = root / "metadata" / domain_id
        layers: dict[str, Any] = {}
        if layer_root.exists():
            for layer_directory in sorted(path for path in layer_root.iterdir() if path.is_dir()):
                frames = read_metadata(root, domain_id, layer_directory.name)
                if frames:
                    specification = LAYERS.get(layer_directory.name)
                    entry: dict[str, Any] = {
                        "title": specification.title if specification else layer_directory.name,
                        "maxAgeMinutes": specification.max_age_minutes if specification else 30,
                        "frames": frames,
                    }
                    if specification is not None:
                        entry["role"] = specification.role
                        entry["format"] = specification.image_format
                        if specification.point_schema:
                            entry["pointFrame"] = {
                                "schemaVersion": 1,
                                "coordinateSpace": "normalized-top-left",
                                "pointSchema": list(specification.point_schema),
                                "retention": retention_policy(domain.tier),
                            }
                    layers[layer_directory.name] = entry
        static_layers: dict[str, Any] = {}
        for layer_id, filename in (
            ("base-dark", "base-dark.png"),
            ("watersheds", "bch-watersheds.png"),
            ("boundaries", "boundaries.png"),
        ):
            path = root / "static" / domain_id / filename
            if path.exists():
                static_layers[layer_id] = {"path": path.relative_to(root).as_posix()}
        domains[domain_id] = {
            "id": domain.id,
            "title": domain.title,
            "width": domain.width,
            "height": domain.height,
            "projection": domain.crs,
            "retention": retention_policy(domain.tier),
            "layers": layers,
            "staticLayers": static_layers,
        }
    return {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "domains": domains,
        "products": PRODUCTS,
        "legends": LEGENDS,
        "sources": {
            "ECCC GeoMet": "https://eccc-msc.github.io/open-data/msc-geomet/",
            "ECCC Datamart": "https://dd.weather.gc.ca/",
            "NRCan CWFIS": "https://cwfis.cfs.nrcan.gc.ca/downloads/docs/en/references/cwfif/cwfis-data-placemat.pdf",
            "NOAA Open Data": "https://www.ncei.noaa.gov/products/ncei-data-noaa-open-dissemination-program",
            "NOAA GOES-18": "https://www.ncei.noaa.gov/products/satellite/goes-r-series",
        },
    }


def write_catalog(root: Path) -> Path:
    path = root / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(build_catalog(root), indent=2) + "\n")
    temporary.replace(path)
    return path
