from __future__ import annotations

import datetime as dt
import json
import os
import re
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import (
    DOMAINS,
    LAYERS,
    LEGENDS,
    PRODUCTS,
    VIDEO_TRACKS_BY_PRODUCT,
    VIEWPORTS,
)


UTC = dt.timezone.utc
CANONICAL_FIVE_MINUTE_SOURCE = "NOAA/NESDIS/STAR"
CANONICAL_FIVE_MINUTE_RENDER_VERSION = 4
VIDEO_GENERATION_RE = re.compile(r"^\d{8}T\d{4}Z-[0-9a-f]{12}$")
VIDEO_TRACKS = frozenset({"live", "day", "archive"})
PUBLIC_VIDEO_LAYERS = {
    "bc-large-overlay": "eccc-geocolor",
    "bc-small-overlay": "eccc-geocolor",
    "bc-southwest-overlay": "eccc-geocolor",
    "bc-southeast-overlay": "eccc-geocolor",
    "bc-northeast-overlay": "eccc-geocolor",
    "bc-south-coast-overlay": "eccc-geocolor",
    "north-america-overlay": "westwx-visir",
    "pacific-wna-overlay": "raw-visir",
    "north-pacific-overlay": "raw-visir",
}


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


def _frame_asset_exists(root: Path, frame: dict[str, Any]) -> bool:
    relative = Path(str(frame.get("path", "")))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return False
    try:
        return (root / relative).is_file()
    except OSError:
        return False


def _matches_current_regional_viewport(
    layer_id: str,
    frame: dict[str, Any],
) -> bool:
    """Exclude explicitly stale pre-cropped assets after a viewport change."""
    for region_id, viewport in VIEWPORTS.items():
        if not layer_id.endswith(f"-region-{region_id}"):
            continue
        return (
            "regionalViewport" not in frame
            or frame.get("regionalViewport") == viewport
        )
    return True


def _without_broken_tiles(root: Path, frame: dict[str, Any]) -> dict[str, Any]:
    """Retain the whole-frame fallback when a tile manifest is unavailable."""
    tiles = frame.get("tiles")
    manifest_value = tiles.get("manifest") if isinstance(tiles, dict) else None
    if not isinstance(manifest_value, str):
        return frame
    relative = Path(manifest_value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        valid = False
    else:
        try:
            valid = (root / relative).is_file()
        except OSError:
            valid = False
    if valid:
        try:
            payload = json.loads((root / relative).read_text())
            files = payload.get("files")
            valid = isinstance(files, list) and bool(files)
            if valid:
                for value in files:
                    tile = Path(str(value))
                    if (
                        tile.is_absolute()
                        or not tile.parts
                        or ".." in tile.parts
                        or not (root / tile).is_file()
                    ):
                        valid = False
                        break
        except (OSError, json.JSONDecodeError):
            valid = False
    if valid:
        return frame
    sanitized = dict(frame)
    sanitized.pop("tiles", None)
    return sanitized


def _parse_frame_time(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _known_product_layers() -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for product in PRODUCTS:
        product_id = product.get("id")
        layers = product.get("layers")
        if not isinstance(product_id, str) or not isinstance(layers, list):
            continue
        result[product_id] = frozenset(
            str(layer["id"])
            for layer in layers
            if isinstance(layer, dict) and isinstance(layer.get("id"), str)
        )
    return result


def _valid_video_manifest_pointer(
    root: Path,
    product_id: str,
    layer_id: str,
    track: str,
    pointer: object,
) -> dict[str, str] | None:
    """Return a safe immutable video pointer, or omit the optional fast path.

    The local index is mutable, but it may only point at a versioned manifest
    whose path repeats the product/layer/track/generation tuple.  Reading the
    manifest header here prevents a partially rotated or cross-profile pointer
    from entering the public catalog.  The publisher performs the deeper media
    and proxy validation immediately before upload.
    """
    if not isinstance(pointer, dict):
        return None
    generation = pointer.get("generation")
    manifest_value = pointer.get("manifestPath")
    if (
        not isinstance(generation, str)
        or not VIDEO_GENERATION_RE.fullmatch(generation)
        or not isinstance(manifest_value, str)
    ):
        return None
    relative = Path(manifest_value)
    expected_parts = (
        "video-manifests",
        product_id,
        layer_id,
        track,
        f"{generation}.json",
    )
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts != expected_parts
        or relative.as_posix() != manifest_value
    ):
        return None
    manifest = root / relative
    try:
        if (
            manifest.is_symlink()
            or not manifest.resolve().is_relative_to(root.resolve())
            or not manifest.is_file()
            or manifest.stat().st_size <= 0
        ):
            return None
        payload = json.loads(manifest.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or any(
        (
            payload.get("schemaVersion") != 2,
            payload.get("productId") != product_id,
            payload.get("layerId") != layer_id,
            payload.get("track") != track,
            payload.get("generation") != generation,
        )
    ):
        return None
    return {"generation": generation, "manifestPath": manifest_value}


def read_video_profiles(root: Path) -> dict[str, Any]:
    """Read the atomically maintained local video index files.

    Each encoder profile owns one small pointer at
    ``video-index/<product>/<layer>.json``. Invalid, incomplete, or stale
    pointers are deliberately ignored so the conventional image archive
    remains a complete fallback. Only the operational default satellite layer
    is published for each domain; old secondary pointers would otherwise keep
    stale video dependencies protected indefinitely.
    """
    index_root = root / "video-index"
    if not index_root.is_dir():
        return {}
    known_layers = _known_product_layers()
    video_profiles: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    for index_path in sorted(index_root.rglob("*.json")):
        try:
            if index_path.is_symlink() or not index_path.is_file():
                continue
            payload = json.loads(index_path.read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        # Schema-v1 pointers predate exact-range composites and may represent
        # old secondary satellite experiments.  Once a layer becomes the
        # operational default, admitting those pointers would briefly revive
        # stale media during migration.  New encoders always publish v2.
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 2:
            continue
        product_id = payload.get("productId")
        layer_id = payload.get("layerId")
        profiles = payload.get("profiles")
        if (
            not isinstance(product_id, str)
            or not isinstance(layer_id, str)
            or layer_id not in known_layers.get(product_id, ())
            or PUBLIC_VIDEO_LAYERS.get(product_id) != layer_id
            or not isinstance(profiles, dict)
        ):
            continue
        expected_index = index_root / product_id / f"{layer_id}.json"
        try:
            if index_path.resolve() != expected_index.resolve():
                continue
        except OSError:
            continue
        for track, pointer in profiles.items():
            if (
                track not in VIDEO_TRACKS
                or track not in VIDEO_TRACKS_BY_PRODUCT.get(product_id, ())
            ):
                continue
            validated = _valid_video_manifest_pointer(
                root,
                product_id,
                layer_id,
                track,
                pointer,
            )
            if validated is None:
                continue
            existing = (
                video_profiles
                .setdefault(product_id, {})
                .setdefault(layer_id, {})
                .get(track)
            )
            if existing is None or validated["generation"] > existing["generation"]:
                video_profiles[product_id][layer_id][track] = validated
    return video_profiles


def _canonical_five_minute_frames(
    domain_id: str,
    layer_id: str,
    frames: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose one resolution/source family with nondecreasing fallback time.

    Raw NODD PACUS frames and STAR/CIRA GeoColor used to share this layer.
    Switching between their projections and resolutions made clouds jump even
    when valid times advanced. STAR is now the canonical public renderer; raw
    products remain available as inputs for its northern full-disk fallback.
    """
    if domain_id != "bc" or layer_id != "raw-visir-5min":
        return frames
    canonical = [
        frame
        for frame in frames
        if frame.get("source") == CANONICAL_FIVE_MINUTE_SOURCE
        and frame.get("starRenderVersion") == CANONICAL_FIVE_MINUTE_RENDER_VERSION
        and _parse_frame_time(frame.get("fallbackSourceTime")) is not None
    ]
    if not canonical:
        return []
    latest = canonical[-1]
    dimensions = (latest.get("renderWidth"), latest.get("renderHeight"))
    selected: list[dict[str, Any]] = []
    previous_fallback: dt.datetime | None = None
    for frame in canonical:
        if (frame.get("renderWidth"), frame.get("renderHeight")) != dimensions:
            continue
        fallback = _parse_frame_time(frame.get("fallbackSourceTime"))
        if fallback is None or (
            previous_fallback is not None and fallback < previous_fallback
        ):
            continue
        selected.append(frame)
        previous_fallback = fallback
    return selected


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
    # Independent ingest and retention workers can rotate a frame immediately
    # after its metadata directory was judged unchanged. Revalidate every
    # referenced asset, including incrementally reused entries, so a catalog
    # can never publish a path that has already disappeared.
    frames = [
        _without_broken_tiles(root, frame)
        for frame in frames_by_path.values()
        if _frame_asset_exists(root, frame)
        and _matches_current_regional_viewport(layer_id, frame)
    ]
    frames.sort(key=lambda item: item["validTime"])
    return _canonical_five_minute_frames(domain_id, layer_id, frames)


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
            static_files = [
                ("base-dark", "base-dark.png"),
                ("watersheds", "bch-watersheds.png"),
                ("transmission-lines", "transmission-lines.png"),
                ("boundaries", "boundaries.png"),
            ]
            if domain_id == "bc":
                static_files.extend(
                    (
                        f"watersheds-region-{region_id}",
                        f"bch-watersheds-region-{region_id}.png",
                    )
                    for region_id in VIEWPORTS
                )
            for layer_id, filename in static_files:
                path = root / "static" / domain_id / filename
                if path.exists():
                    static_layers[layer_id] = {
                        "path": path.relative_to(root).as_posix(),
                        "revision": str(path.stat().st_mtime_ns),
                    }
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
    catalog = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "domains": domains,
        "products": PRODUCTS,
        "legends": LEGENDS,
        "sources": {
            "ECCC GeoMet": "https://eccc-msc.github.io/open-data/msc-geomet/",
            "ECCC Datamart": "https://dd.weather.gc.ca/",
            "ECCC HRDPS Continental 2.5 km": "https://eccc-msc.github.io/open-data/msc-data/nwp_hrdps/readme_hrdps-datamart_en/",
            "ECMWF IFS Control": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
            "NRCan CWFIS": "https://cwfis.cfs.nrcan.gc.ca/downloads/docs/en/references/cwfif/cwfis-data-placemat.pdf",
            "BC Wildfire Service": "https://services6.arcgis.com/ubm4tcTYICKBpist/ArcGIS/rest/services/BCWS_ActiveFires_PublicView/FeatureServer/0",
            "NIFC WFIGS": "https://www.arcgis.com/home/item.html?id=4181a117dc9e43db8598533e29972015",
            "NOAA Open Data": "https://www.ncei.noaa.gov/products/ncei-data-noaa-open-dissemination-program",
            "NOAA GOES-18": "https://www.ncei.noaa.gov/products/satellite/goes-r-series",
            "NOAA/NESDIS/STAR": "https://www.goes.noaa.gov/",
            "GeoBC": "https://catalogue.data.gov.bc.ca/dataset/transmission-lines",
        },
    }
    video_profiles = read_video_profiles(root)
    if video_profiles:
        catalog["videoProfiles"] = video_profiles
    return catalog


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
