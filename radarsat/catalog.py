from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import Executor, ThreadPoolExecutor
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


def _read_metadata_path(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None


def _metadata_key_for_frame(frame: dict[str, Any]) -> str | None:
    parts = Path(str(frame.get("path", ""))).parts
    if not parts or parts[0] != "frames":
        return None
    return Path("metadata", *parts[1:]).with_suffix(".json").as_posix()


def _previous_metadata(root: Path) -> tuple[int | None, dict[str, dict[str, Any]]]:
    catalog_path = root / "catalog.json"
    try:
        catalog_mtime_ns = catalog_path.stat().st_mtime_ns
        catalog = json.loads(catalog_path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None, {}
    previous: dict[str, dict[str, Any]] = {}
    for domain in catalog.get("domains", {}).values():
        for layer in domain.get("layers", {}).values():
            for frame in layer.get("frames", []):
                if not isinstance(frame, dict):
                    continue
                key = _metadata_key_for_frame(frame)
                if key is not None:
                    previous[key] = frame
    return catalog_mtime_ns, previous


def read_metadata(
    root: Path,
    domain_id: str,
    layer_id: str,
    *,
    executor: Executor | None = None,
    previous: dict[str, dict[str, Any]] | None = None,
    catalog_mtime_ns: int | None = None,
) -> list[dict[str, Any]]:
    directory = root / "metadata" / domain_id / layer_id
    if not directory.exists():
        return []
    paths = sorted(directory.rglob("*.json"))
    prior = previous or {}
    directory_changed: dict[Path, bool] = {}
    pending: list[Path] = []
    frames_by_path: dict[Path, dict[str, Any]] = {}
    for path in paths:
        key = path.relative_to(root).as_posix()
        existing = prior.get(key)
        if catalog_mtime_ns is not None and existing is not None:
            changed = directory_changed.get(path.parent)
            if changed is None:
                try:
                    changed = path.parent.stat().st_mtime_ns > catalog_mtime_ns
                except OSError:
                    changed = True
                directory_changed[path.parent] = changed
            if not changed:
                frames_by_path[path] = existing
                continue
        pending.append(path)
    loaded = (
        executor.map(_read_metadata_path, pending)
        if executor is not None
        else map(_read_metadata_path, pending)
    )
    for path, frame in zip(pending, loaded, strict=True):
        if frame is not None:
            frames_by_path[path] = frame
    frames = list(frames_by_path.values())
    frames.sort(key=lambda item: item["validTime"])
    return frames


def build_catalog(root: Path) -> dict[str, Any]:
    domains: dict[str, Any] = {}
    catalog_mtime_ns, previous = _previous_metadata(root)
    # Catalog construction is dominated by opening thousands of small metadata
    # files. A bounded shared pool overlaps that local I/O while preserving the
    # deterministic path and validity-time ordering of every layer.
    with ThreadPoolExecutor(max_workers=8) as executor:
        for domain_id, domain in DOMAINS.items():
            layer_root = root / "metadata" / domain_id
            layers: dict[str, Any] = {}
            if layer_root.exists():
                for layer_directory in sorted(path for path in layer_root.iterdir() if path.is_dir()):
                    frames = read_metadata(
                        root,
                        domain_id,
                        layer_directory.name,
                        executor=executor,
                        previous=previous,
                        catalog_mtime_ns=catalog_mtime_ns,
                    )
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
            "BC Wildfire Service": "https://services6.arcgis.com/ubm4tcTYICKBpist/ArcGIS/rest/services/BCWS_ActiveFires_PublicView/FeatureServer/0",
            "NIFC WFIGS": "https://www.arcgis.com/home/item.html?id=4181a117dc9e43db8598533e29972015",
            "NOAA Open Data": "https://www.ncei.noaa.gov/products/ncei-data-noaa-open-dissemination-program",
            "NOAA GOES-18": "https://www.ncei.noaa.gov/products/satellite/goes-r-series",
        },
    }


def write_catalog(root: Path) -> Path:
    path = root / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Satellite, radar and slow archive workers intentionally run independently.
    # A PID-specific temporary keeps simultaneous atomic refreshes from
    # clobbering one another's staging file.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(build_catalog(root), indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
