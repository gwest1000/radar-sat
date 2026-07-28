"""Generate cacheable XYZ WebP pyramids for WestWX raster playback."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

from .config import DOMAINS, Domain
from .geomet import projected_bbox


UTC = dt.timezone.utc
WEB_MERCATOR = CRS.from_epsg(3857)
WEB_MERCATOR_LIMIT = 20037508.342789244
TILE_SIZE = 512
TILE_RENDER_VERSION = 1


@dataclass(frozen=True)
class TileProfile:
    domain_id: str
    layer_id: str
    min_zoom: int
    max_zoom: int
    encoding: str = "lossy-webp"


RASTER_PROFILES = (
    TileProfile("north-america", "westwx-visir", 2, 4),
    TileProfile("north-america", "westwx-ir", 2, 4),
    TileProfile("north-america", "radar-rain", 2, 5, "lossless-webp"),
    TileProfile("north-america", "smoke", 2, 5, "lossless-webp"),
    TileProfile("bc", "raw-ir", 3, 7),
    TileProfile("bc", "raw-visir-5min", 3, 7),
    TileProfile("bc", "ptype", 3, 6, "lossless-webp"),
)
PROFILES = {
    (profile.domain_id, profile.layer_id): profile
    for profile in RASTER_PROFILES
}


def _parse_time(value: object) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _tile_index(x: float, y: float, zoom: int) -> tuple[int, int]:
    count = 1 << zoom
    tile_x = math.floor((x + WEB_MERCATOR_LIMIT) / (2 * WEB_MERCATOR_LIMIT) * count)
    tile_y = math.floor((WEB_MERCATOR_LIMIT - y) / (2 * WEB_MERCATOR_LIMIT) * count)
    return (
        max(0, min(count - 1, tile_x)),
        max(0, min(count - 1, tile_y)),
    )


def _tile_bounds(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    count = 1 << zoom
    span = 2 * WEB_MERCATOR_LIMIT / count
    left = -WEB_MERCATOR_LIMIT + x * span
    right = left + span
    top = WEB_MERCATOR_LIMIT - y * span
    bottom = top - span
    return left, bottom, right, top


def _geographic_bounds(domain: Domain) -> list[float]:
    return [domain.west, domain.south, domain.east, domain.north]


def _projected_tile_range(domain: Domain, zoom: int) -> tuple[range, range]:
    source_bounds = projected_bbox(domain)
    west, south, east, north = transform_bounds(
        CRS.from_user_input(domain.crs),
        WEB_MERCATOR,
        *source_bounds,
        densify_pts=41,
    )
    min_x, max_y = _tile_index(west, north, zoom)
    max_x, min_y = _tile_index(east, south, zoom)
    return range(min_x, max_x + 1), range(max_y, min_y + 1)


def _render_tile(
    source: np.ndarray,
    source_transform,
    source_crs: CRS,
    x: int,
    y: int,
    zoom: int,
    *,
    resampling: Resampling,
) -> np.ndarray:
    rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
    destination_transform = from_bounds(*_tile_bounds(x, y, zoom), TILE_SIZE, TILE_SIZE)
    for band in range(3):
        reproject(
            source=source[:, :, band],
            destination=rgba[:, :, band],
            src_transform=source_transform,
            src_crs=source_crs,
            dst_transform=destination_transform,
            dst_crs=WEB_MERCATOR,
            resampling=resampling,
            num_threads=2,
            init_dest_nodata=True,
        )
    alpha = source[:, :, 3] if source.shape[2] > 3 else np.full(source.shape[:2], 255, dtype=np.uint8)
    reproject(
        source=alpha,
        destination=rgba[:, :, 3],
        src_transform=source_transform,
        src_crs=source_crs,
        dst_transform=destination_transform,
        dst_crs=WEB_MERCATOR,
        resampling=Resampling.nearest,
        num_threads=2,
        init_dest_nodata=True,
    )
    return rgba


def _save_tile(image: np.ndarray, destination: Path, encoding: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = Image.fromarray(image)
    if encoding == "lossless-webp":
        rendered.save(destination, "WEBP", lossless=True, method=6, exact=True)
    else:
        rendered.save(destination, "WEBP", quality=90, method=6, exact=True)


def _tile_paths(
    root: Path,
    profile: TileProfile,
    valid_time: dt.datetime,
) -> tuple[Path, Path, str]:
    date_path = Path(valid_time.strftime("%Y/%m/%d"))
    stamp = valid_time.strftime("%Y%m%dT%H%MZ")
    tile_root = root / "tiles" / profile.domain_id / profile.layer_id / date_path / stamp
    manifest = root / "tile-manifests" / profile.domain_id / profile.layer_id / date_path / f"{stamp}.json"
    template = (tile_root.relative_to(root) / "{z}" / "{x}" / "{y}.webp").as_posix()
    return tile_root, manifest, template


def generate_tiles(
    root: Path,
    metadata_path: Path,
    profile: TileProfile,
) -> dict[str, object]:
    payload = json.loads(metadata_path.read_text())
    existing = payload.get("tiles")
    if (
        isinstance(existing, dict)
        and existing.get("renderVersion") == TILE_RENDER_VERSION
        and isinstance(existing.get("manifest"), str)
        and (root / existing["manifest"]).is_file()
    ):
        # Catalog rebuilding uses directory mtimes to avoid reopening thousands
        # of unchanged metadata files. Touch the layer directory so the newly
        # added tile metadata cannot be hidden by a previously cached catalog.
        os.utime(metadata_path.parent, None)
        return {"status": "unchanged", "metadata": metadata_path.as_posix()}

    source_path = root / str(payload["path"])
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    domain = DOMAINS[profile.domain_id]
    valid_time = _parse_time(payload["validTime"])
    tile_root, manifest_path, template = _tile_paths(root, profile, valid_time)
    temporary_root = tile_root.with_name(f".{tile_root.name}.{os.getpid()}.tmp")
    shutil.rmtree(temporary_root, ignore_errors=True)
    temporary_root.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as opened:
        source = np.asarray(opened.convert("RGBA"))
    source_transform = from_bounds(
        *projected_bbox(domain),
        source.shape[1],
        source.shape[0],
    )
    source_crs = CRS.from_user_input(domain.crs)
    files: list[str] = []
    total_bytes = 0
    try:
        for zoom in range(profile.min_zoom, profile.max_zoom + 1):
            x_values, y_values = _projected_tile_range(domain, zoom)
            for x in x_values:
                for y in y_values:
                    tile = _render_tile(
                        source,
                        source_transform,
                        source_crs,
                        x,
                        y,
                        zoom,
                        resampling=Resampling.lanczos,
                    )
                    if not np.any(tile[:, :, 3]):
                        continue
                    relative = Path(str(zoom)) / str(x) / f"{y}.webp"
                    destination = temporary_root / relative
                    _save_tile(tile, destination, profile.encoding)
                    final_relative = (tile_root.relative_to(root) / relative).as_posix()
                    files.append(final_relative)
                    total_bytes += destination.stat().st_size
        if tile_root.exists():
            shutil.rmtree(tile_root)
        temporary_root.replace(tile_root)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)

    manifest_payload = {
        "schemaVersion": 1,
        "validTime": payload["validTime"],
        "domain": profile.domain_id,
        "layer": profile.layer_id,
        "format": "webp",
        "encoding": profile.encoding,
        "tileSize": TILE_SIZE,
        "minZoom": profile.min_zoom,
        "maxZoom": profile.max_zoom,
        "files": files,
        "bytes": total_bytes,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = manifest_path.with_name(f"{manifest_path.name}.{os.getpid()}.tmp")
    temporary_manifest.write_text(json.dumps(manifest_payload, separators=(",", ":")) + "\n")
    temporary_manifest.replace(manifest_path)

    payload["tiles"] = {
        "template": template,
        "manifest": manifest_path.relative_to(root).as_posix(),
        "bounds": _geographic_bounds(domain),
        "minZoom": profile.min_zoom,
        "maxZoom": profile.max_zoom,
        "tileSize": TILE_SIZE,
        "format": "webp",
        "encoding": profile.encoding,
        "renderVersion": TILE_RENDER_VERSION,
        "tileCount": len(files),
        "bytes": total_bytes,
    }
    temporary_metadata = metadata_path.with_name(f"{metadata_path.name}.{os.getpid()}.tmp")
    temporary_metadata.write_text(json.dumps(payload, indent=2) + "\n")
    temporary_metadata.replace(metadata_path)
    os.utime(metadata_path.parent, None)
    return {
        "status": "rendered",
        "metadata": metadata_path.as_posix(),
        "tiles": len(files),
        "bytes": total_bytes,
    }


def metadata_candidates(
    root: Path,
    profile: TileProfile,
    *,
    hours: float,
    max_frames: int,
    now: dt.datetime | None = None,
) -> list[Path]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    cutoff = current - dt.timedelta(hours=hours)
    directory = root / "metadata" / profile.domain_id / profile.layer_id
    values: list[tuple[dt.datetime, Path]] = []
    for path in directory.rglob("*.json") if directory.exists() else ():
        try:
            payload = json.loads(path.read_text())
            valid_time = _parse_time(payload.get("validTime"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if valid_time >= cutoff:
            values.append((valid_time, path))
    values.sort(reverse=True)
    return [path for _valid_time, path in values[:max_frames]]


def prune_orphan_tiles(root: Path) -> dict[str, int]:
    """Remove pyramids whose source-frame metadata has already aged out."""
    referenced_manifests: set[str] = set()
    metadata_root = root / "metadata"
    for metadata in metadata_root.rglob("*.json") if metadata_root.exists() else ():
        try:
            payload = json.loads(metadata.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        tiles = payload.get("tiles")
        manifest = tiles.get("manifest") if isinstance(tiles, dict) else None
        if isinstance(manifest, str):
            referenced_manifests.add(manifest)

    removed_files = 0
    removed_manifests = 0
    manifest_root = root / "tile-manifests"
    for manifest in manifest_root.rglob("*.json") if manifest_root.exists() else ():
        relative_manifest = manifest.relative_to(root).as_posix()
        if relative_manifest in referenced_manifests:
            continue
        try:
            payload = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        files = payload.get("files")
        if isinstance(files, list):
            for value in files:
                candidate = Path(str(value))
                if candidate.is_absolute() or ".." in candidate.parts:
                    continue
                target = root / candidate
                if target.is_file():
                    target.unlink()
                    removed_files += 1
        manifest.unlink(missing_ok=True)
        removed_manifests += 1

    # Remove only empty directories inside the two dedicated tile trees.
    for tree in (root / "tiles", manifest_root):
        if not tree.exists():
            continue
        for directory in sorted(
            (path for path in tree.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {"files": removed_files, "manifests": removed_manifests}


def build_profiles(
    root: Path,
    profiles: Iterable[TileProfile],
    *,
    hours: float,
    max_frames: int,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for profile in profiles:
        for metadata in reversed(
            metadata_candidates(root, profile, hours=hours, max_frames=max_frames)
        ):
            results.append(generate_tiles(root, metadata, profile))
    cleanup = prune_orphan_tiles(root)
    if cleanup["files"] or cleanup["manifests"]:
        results.append({"status": "pruned", **cleanup})
    return results
