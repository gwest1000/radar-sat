"""Bounded high-resolution GOES-18 GeoColor ingest from NOAA/NESDIS/STAR.

NOAA STAR publishes CIRA GeoColor on the native 0.5 km ABI fixed grid as
ordinary JPEGs.  Reprojecting those display products is much lighter than
downloading four separate full-disk channel files, while retaining the
variance-encoded detail visible in CIRA SLIDER.

The official STAR service is not operationally guaranteed.  These products
therefore occupy the existing preferred ``raw-visir-native`` and
``raw-visir-5min`` layers; the NOAA Open Data render remains the automatic
fallback whenever STAR is late or unavailable.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from xml.sax.saxutils import escape

import numpy as np
import requests
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject
from requests.adapters import HTTPAdapter
from scipy.ndimage import distance_transform_edt
from urllib3.util.retry import Retry

from .catalog import write_catalog
from .config import DOMAINS, LAYERS, Domain
from .geomet import format_utc, projected_bbox
from .pipeline import (
    RAW_VISIR_RENDER_VERSION,
    frame_path,
    metadata_path,
    safe_archive_path,
    write_metadata,
)


UTC = dt.timezone.utc
SOURCE = "NOAA/NESDIS/STAR"
RENDER_VERSION = 4
OUTPUT_WIDTH = 3840
OUTPUT_HEIGHT = 2944
DEFAULT_MAX_SOURCE_BYTES = 100_000_000
GOES_HEIGHT_METRES = 35_786_023.0
GOES_CRS = CRS.from_string(
    "+proj=geos +h=35786023 +lon_0=-137 +sweep=x "
    "+ellps=GRS80 +units=m +no_defs"
)


@dataclass(frozen=True)
class StarSector:
    id: str
    directory_url: str
    filename_pattern: re.Pattern[str]
    layer_id: str
    cadence_minutes: int
    source_width: int
    source_height: int
    x_bounds_radians: tuple[float, float]
    y_bounds_radians: tuple[float, float]


FULL_DISK = StarSector(
    id="full-disk",
    directory_url="https://cdn.star.nesdis.noaa.gov/GOES18/ABI/FD/GEOCOLOR/",
    filename_pattern=re.compile(
        r"(?P<stamp>\d{11})_GOES18-ABI-FD-GEOCOLOR-21696x21696\.jpg"
    ),
    layer_id="raw-visir-native",
    cadence_minutes=10,
    source_width=21696,
    source_height=21696,
    # ABI full-disk fixed-grid outer pixel edges.  The commonly quoted
    # +/-0.151844 values are 2 km pixel centres, not the image edges.
    x_bounds_radians=(-0.151872, 0.151872),
    y_bounds_radians=(-0.151872, 0.151872),
)

PACUS = StarSector(
    id="pacus",
    directory_url="https://cdn.star.nesdis.noaa.gov/GOES18/ABI/CONUS/GEOCOLOR/",
    filename_pattern=re.compile(
        r"(?P<stamp>\d{11})_GOES18-ABI-CONUS-GEOCOLOR-10000x6000\.jpg"
    ),
    layer_id="raw-visir-5min",
    cadence_minutes=5,
    source_width=10000,
    source_height=6000,
    # Verified against the operational ABI-L2-MCMIPC x/y coordinates.  The
    # source arrays contain 2 km pixel centres at x +/-0.069972 and
    # y 0.044268..0.128212; these are their exact outer edges.
    x_bounds_radians=(-0.070000, 0.070000),
    y_bounds_radians=(0.044240, 0.128240),
)

SECTORS = {sector.id: sector for sector in (FULL_DISK, PACUS)}


@dataclass(frozen=True)
class StarScan:
    sector: StarSector
    valid_time: dt.datetime
    source_time: dt.datetime
    filename: str
    size: int

    @property
    def url(self) -> str:
        return f"{self.sector.directory_url}{self.filename}"


@dataclass(frozen=True)
class RenderTarget:
    domain_id: str
    layer_id: str
    width: int
    height: int


def render_targets(sector: StarSector) -> tuple[RenderTarget, ...]:
    if sector.id == FULL_DISK.id:
        pacific = DOMAINS["north-pacific"]
        return (
            RenderTarget("bc", sector.layer_id, OUTPUT_WIDTH, OUTPUT_HEIGHT),
            RenderTarget(
                pacific.id,
                "raw-visir",
                pacific.width,
                pacific.height,
            ),
        )
    return (RenderTarget("bc", sector.layer_id, OUTPUT_WIDTH, OUTPUT_HEIGHT),)


@dataclass(frozen=True)
class DiscoveryResult:
    scans: tuple[StarScan, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedBackfill:
    scans: tuple[StarScan, ...]
    estimated_bytes: int
    skipped_ready: int
    excluded_by_frame_limit: int
    excluded_by_byte_limit: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    valid_time: dt.datetime
    source_time: dt.datetime
    status: str
    source_bytes: int
    download_seconds: float = 0.0
    render_seconds: float = 0.0
    error: str | None = None


@dataclass
class BackfillResult:
    plan: PlannedBackfill
    scans: list[ScanResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(item.status == "failed" for item in self.scans):
            return "warning"
        return "rendered" if any(item.status == "rendered" for item in self.scans) else "unchanged"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "renderedFrames": sum(item.status == "rendered" for item in self.scans),
            "failedFrames": sum(item.status == "failed" for item in self.scans),
            "scans": [
                {
                    "validTime": format_utc(item.valid_time),
                    "sourceTime": format_utc(item.source_time),
                    "status": item.status,
                    "sourceBytes": item.source_bytes,
                    "downloadSeconds": round(item.download_seconds, 3),
                    "renderSeconds": round(item.render_seconds, 3),
                    **({"error": item.error} if item.error else {}),
                }
                for item in self.scans
            ],
        }


class StarClient:
    def __init__(self, timeout: float = 90.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self.session.mount(
            "https://",
            HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2),
        )
        self.session.headers.update(
            {"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"}
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "StarClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def directory(self, sector: StarSector) -> str:
        response = self.session.get(sector.directory_url, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def download(self, scan: StarScan, cache_root: Path, maximum: int) -> Path:
        if scan.size > maximum:
            raise RuntimeError(
                f"{scan.sector.id} GeoColor source is {scan.size:,} bytes, "
                f"above the {maximum:,}-byte cap"
            )
        downloads = cache_root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        destination = downloads / scan.filename
        if destination.is_file() and destination.stat().st_size == scan.size:
            return destination
        destination.unlink(missing_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)
        received = 0
        try:
            with self.session.get(scan.url, stream=True, timeout=self.timeout) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if not block:
                            continue
                        received += len(block)
                        if received > maximum:
                            raise RuntimeError("GeoColor download exceeded its byte cap")
                        output.write(block)
            if received != scan.size:
                raise RuntimeError(
                    f"Incomplete GeoColor download: {received:,} != {scan.size:,} bytes"
                )
            partial.replace(destination)
            return destination
        finally:
            partial.unlink(missing_ok=True)


def _nominal_time(source_time: dt.datetime, cadence_minutes: int) -> dt.datetime:
    value = source_time.astimezone(UTC)
    return value.replace(
        minute=(value.minute // cadence_minutes) * cadence_minutes,
        second=0,
        microsecond=0,
    )


def _parse_source_time(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y%j%H%M").replace(tzinfo=UTC)


def discover_scans(
    client: StarClient,
    sector: StarSector,
    start: dt.datetime,
    end: dt.datetime,
) -> DiscoveryResult:
    first = start.astimezone(UTC)
    last = end.astimezone(UTC)
    if first > last:
        raise ValueError("start must not be after end")
    try:
        listing = client.directory(sector)
    except Exception as error:
        return DiscoveryResult((), (f"{type(error).__name__}: {error}",))
    found: dict[str, StarScan] = {}
    for raw_line in listing.splitlines():
        line = html.unescape(raw_line)
        match = sector.filename_pattern.search(line)
        if match is None:
            continue
        source_time = _parse_source_time(match.group("stamp"))
        if source_time < first or source_time > last:
            continue
        size_match = re.search(r"</a>\s+\S+\s+\S+\s+(?P<size>\d+)\s*$", line)
        if size_match is None:
            continue
        filename = match.group(0)
        found[filename] = StarScan(
            sector,
            _nominal_time(source_time, sector.cadence_minutes),
            source_time,
            filename,
            int(size_match.group("size")),
        )
    return DiscoveryResult(
        tuple(sorted(found.values(), key=lambda item: item.valid_time, reverse=True))
    )


def scan_ready(root: Path, scan: StarScan) -> bool:
    for target in render_targets(scan.sector):
        domain = DOMAINS[target.domain_id]
        layer = LAYERS[target.layer_id]
        image = frame_path(root, domain, layer, scan.valid_time)
        metadata = metadata_path(root, domain, layer, scan.valid_time)
        if not image.is_file() or not metadata.is_file():
            return False
        try:
            payload = json.loads(metadata.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        expected_version = (
            RENDER_VERSION
            if target.layer_id == scan.sector.layer_id
            else RAW_VISIR_RENDER_VERSION
        )
        if (
            payload.get("renderVersion") != expected_version
            or payload.get("starRenderVersion") != RENDER_VERSION
            or payload.get("sourceFile") != scan.filename
            or payload.get("source") != SOURCE
        ):
            return False
    return True


def plan_backfill(
    root: Path,
    discovery: DiscoveryResult,
    *,
    max_frames: int,
    max_download_bytes: int,
    overwrite: bool = False,
) -> PlannedBackfill:
    if max_frames <= 0 or max_download_bytes <= 0:
        raise ValueError("GeoColor limits must be positive")
    pending = [
        scan for scan in discovery.scans
        if overwrite or not scan_ready(root, scan)
    ]
    skipped_ready = len(discovery.scans) - len(pending)
    frame_limited = pending[:max_frames]
    selected: list[StarScan] = []
    estimated_bytes = 0
    excluded_by_byte_limit = 0
    for index, scan in enumerate(frame_limited):
        if estimated_bytes + scan.size > max_download_bytes:
            excluded_by_byte_limit = len(frame_limited) - index
            break
        selected.append(scan)
        estimated_bytes += scan.size
    return PlannedBackfill(
        # Oldest-first execution makes fallback source time naturally
        # nondecreasing during multi-frame repairs and initial backfills.
        tuple(sorted(selected, key=lambda item: item.valid_time)),
        estimated_bytes,
        skipped_ready,
        max(0, len(pending) - len(frame_limited)),
        excluded_by_byte_limit,
        discovery.warnings,
    )


def _virtual_raster(source: Path, sector: StarSector, cache_root: Path) -> Path:
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(source) as image:
            width, height, count = image.width, image.height, image.count
    if (width, height) != (sector.source_width, sector.source_height) or count < 3:
        raise RuntimeError(
            f"Unexpected {sector.id} GeoColor raster: {width}x{height}x{count}"
        )
    xmin, xmax = (value * GOES_HEIGHT_METRES for value in sector.x_bounds_radians)
    ymin, ymax = (value * GOES_HEIGHT_METRES for value in sector.y_bounds_radians)
    pixel_width = (xmax - xmin) / width
    pixel_height = (ymax - ymin) / height
    bands = "".join(
        (
            f'<VRTRasterBand dataType="Byte" band="{band}">'
            f"<SimpleSource><SourceFilename relativeToVRT=\"0\">{escape(str(source.resolve()))}"
            f"</SourceFilename><SourceBand>{band}</SourceBand></SimpleSource>"
            f"</VRTRasterBand>"
        )
        for band in (1, 2, 3)
    )
    destination = cache_root / f"{source.stem}.vrt"
    destination.write_text(
        (
            f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">'
            f"<SRS>{escape(GOES_CRS.to_wkt())}</SRS>"
            f"<GeoTransform>{xmin},{pixel_width},0,{ymax},0,{-pixel_height}</GeoTransform>"
            f"{bands}</VRTDataset>"
        )
    )
    return destination


def project_geocolor(
    source: Path,
    sector: StarSector,
    domain: Domain,
    cache_root: Path,
    *,
    width: int = OUTPUT_WIDTH,
    height: int = OUTPUT_HEIGHT,
) -> tuple[np.ndarray, np.ndarray]:
    import rasterio

    vrt = _virtual_raster(source, sector, cache_root)
    destination = np.zeros((3, height, width), dtype=np.uint8)
    coverage = np.zeros((height, width), dtype=np.uint8)
    target_transform = from_bounds(*projected_bbox(domain), width, height)
    with rasterio.open(vrt) as raster:
        reproject(
            source=rasterio.band(raster, [1, 2, 3]),
            destination=destination,
            src_transform=raster.transform,
            src_crs=raster.crs,
            dst_transform=target_transform,
            dst_crs=domain.crs,
            resampling=Resampling.bilinear,
            num_threads=4,
        )
        if sector.id == FULL_DISK.id:
            coverage = full_disk_coverage(domain, width, height)
        else:
            source_coverage = np.full((raster.height, raster.width), 255, dtype=np.uint8)
            reproject(
                source=source_coverage,
                destination=coverage,
                src_transform=raster.transform,
                src_crs=raster.crs,
                dst_transform=target_transform,
                dst_crs=domain.crs,
                resampling=Resampling.nearest,
                num_threads=4,
            )
    return np.moveaxis(destination, 0, -1), coverage


def full_disk_coverage(domain: Domain, width: int, height: int) -> np.ndarray:
    """Project the ABI Earth disk to a target grid without a source-sized mask."""
    mask_size = 1024
    xmin, xmax = (value * GOES_HEIGHT_METRES for value in FULL_DISK.x_bounds_radians)
    ymin, ymax = (value * GOES_HEIGHT_METRES for value in FULL_DISK.y_bounds_radians)
    x = np.linspace(-1, 1, mask_size, dtype=np.float32)
    y = np.linspace(1, -1, mask_size, dtype=np.float32)
    # The ABI limb is very nearly circular in fixed-grid scan angle. Keeping
    # the mask just inside the JPEG limb avoids feathering black space into
    # the Himawari/GOES fallback.
    source_coverage = np.where(
        x[None, :] ** 2 + y[:, None] ** 2 <= 0.994**2,
        255,
        0,
    ).astype(np.uint8)
    coverage = np.zeros((height, width), dtype=np.uint8)
    reproject(
        source=source_coverage,
        destination=coverage,
        src_transform=from_bounds(xmin, ymin, xmax, ymax, mask_size, mask_size),
        src_crs=GOES_CRS,
        dst_transform=from_bounds(*projected_bbox(domain), width, height),
        dst_crs=domain.crs,
        resampling=Resampling.nearest,
        num_threads=2,
    )
    return coverage


def _parse_time(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _fallback_source_time(payload: dict[str, object]) -> dt.datetime | None:
    explicit = _parse_time(payload.get("fallbackSourceTime"))
    if explicit is not None:
        return explicit
    source_times = payload.get("sourceTimes")
    if isinstance(source_times, dict):
        full_disk = [
            value for key, value in source_times.items()
            if "full-disk" in str(key).lower()
        ]
        parsed = [value for value in (_parse_time(item) for item in full_disk) if value]
        if parsed:
            return max(parsed)
        geocolor = [
            value for key, value in source_times.items()
            if "geocolor" in str(key).lower()
        ]
        parsed = [value for value in (_parse_time(item) for item in geocolor) if value]
        if parsed:
            return max(parsed)
        parsed = [value for value in (_parse_time(item) for item in source_times.values()) if value]
        if parsed:
            return max(parsed)
    return _parse_time(payload.get("validTime"))


def _rapid_neighbour_fallback(
    root: Path,
    valid_time: dt.datetime,
    *,
    after: bool,
) -> dt.datetime | None:
    metadata_root = root / "metadata" / "bc" / "raw-visir-5min"
    selected: tuple[float, dt.datetime] | None = None
    for path in metadata_root.rglob("*.json") if metadata_root.exists() else ():
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        frame_time = _parse_time(payload.get("validTime"))
        fallback_time = _fallback_source_time(payload)
        if frame_time is None or fallback_time is None:
            continue
        qualifies = frame_time > valid_time if after else frame_time < valid_time
        if not qualifies:
            continue
        distance = abs((frame_time - valid_time).total_seconds())
        if selected is None or distance < selected[0]:
            selected = (distance, fallback_time)
    return selected[1] if selected else None


def select_fallback(
    root: Path,
    valid_time: dt.datetime,
    source_time: dt.datetime,
    *,
    max_age_minutes: int = 35,
) -> tuple[Path, dict[str, object], dt.datetime]:
    domain = DOMAINS["bc"]
    candidates: list[tuple[dt.datetime, int, Path, dict[str, object]]] = []
    for preference, layer_id in enumerate(("raw-visir", "raw-visir-native")):
        metadata_root = root / "metadata" / domain.id / layer_id
        for path in metadata_root.rglob("*.json") if metadata_root.exists() else ():
            try:
                payload = json.loads(path.read_text())
                fallback_time = _fallback_source_time(payload)
                image = safe_archive_path(root, str(payload["path"]))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
            if fallback_time is None or not image.is_file() or fallback_time > source_time:
                continue
            if source_time - fallback_time > dt.timedelta(minutes=max_age_minutes):
                continue
            candidates.append((fallback_time, preference, image, payload))
    if not candidates:
        raise RuntimeError("no recent full-disk BC fallback is available")

    previous_floor = _rapid_neighbour_fallback(root, valid_time, after=False)
    next_ceiling = _rapid_neighbour_fallback(root, valid_time, after=True)
    bounded = [
        item for item in candidates
        if (previous_floor is None or item[0] >= previous_floor)
        and (next_ceiling is None or item[0] <= next_ceiling)
    ]
    if bounded:
        candidates = bounded
    # Temporal continuity is more important than selecting a preferred
    # renderer for the northern fallback. Choose the newest admissible scan,
    # using the higher-resolution native/STAR image only to break an exact
    # clock tie. This prevents a newer PACUS frame from being composited over
    # an older northern scan merely because that older image came from STAR.
    selected = max(candidates, key=lambda item: (item[0], item[1]))
    return selected[2], selected[3], selected[0]


def composite_over_fallback(
    pacus: np.ndarray,
    coverage: np.ndarray,
    fallback_path: Path,
) -> np.ndarray:
    fallback_image = Image.open(fallback_path).convert("RGB")
    if fallback_image.size != (pacus.shape[1], pacus.shape[0]):
        fallback_image = fallback_image.resize(
            (pacus.shape[1], pacus.shape[0]),
            Image.Resampling.LANCZOS,
        )
    fallback = np.asarray(fallback_image)
    valid = coverage > 127
    feather_pixels = max(2, min(160, round(min(pacus.shape[:2]) * 0.035)))
    weight = np.clip(distance_transform_edt(valid) / feather_pixels, 0, 1)
    weight = weight * weight * (3 - 2 * weight)
    return np.clip(
        pacus * weight[..., None] + fallback * (1 - weight[..., None]),
        0,
        255,
    ).astype(np.uint8)


def select_pacific_fallback(
    root: Path,
    valid_time: dt.datetime,
    *,
    max_age_minutes: int = 90,
) -> tuple[Path, dict[str, object], dt.datetime] | None:
    domain = DOMAINS["north-pacific"]
    metadata_root = root / "metadata" / domain.id / "raw-visir"
    candidates: list[tuple[dt.datetime, Path, dict[str, object]]] = []
    for path in metadata_root.rglob("*.json") if metadata_root.exists() else ():
        try:
            payload = json.loads(path.read_text())
            frame_time = _parse_time(payload.get("validTime"))
            image = safe_archive_path(root, str(payload["path"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if frame_time is None or frame_time > valid_time or not image.is_file():
            continue
        if valid_time - frame_time > dt.timedelta(minutes=max_age_minutes):
            continue
        # A same-clock STAR image is the output being repaired, not an
        # independent western-Pacific fallback.
        if frame_time == valid_time and payload.get("source") == SOURCE:
            continue
        candidates.append((frame_time, image, payload))
    if not candidates:
        return None
    frame_time, image, payload = max(candidates, key=lambda item: item[0])
    return image, payload, frame_time


def blend_pacific_geocolor(
    geocolor_path: Path,
    fallback_path: Path,
    domain: Domain,
    destination: Path,
) -> None:
    """Feather the GOES-18 disk over a dateline-safe Pacific fallback."""
    with Image.open(geocolor_path) as image:
        geocolor = np.asarray(image.convert("RGB"))
    coverage = full_disk_coverage(domain, geocolor.shape[1], geocolor.shape[0])
    blended = composite_over_fallback(geocolor, coverage, fallback_path)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    Image.fromarray(blended).save(temporary, "WEBP", quality=86, method=4)
    temporary.replace(destination)


def render_scan(
    root: Path,
    scan: StarScan,
    client: StarClient,
    cache_root: Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    overwrite: bool = False,
    projector: Callable[..., tuple[np.ndarray, np.ndarray]] = project_geocolor,
) -> ScanResult:
    if not overwrite and scan_ready(root, scan):
        return ScanResult(scan.valid_time, scan.source_time, "skipped", scan.size)
    scan_cache = (
        cache_root
        / f"work-{os.getpid()}-{scan.valid_time:%Y%m%dT%H%M%S}"
    )
    scan_cache.mkdir(parents=True, exist_ok=True)
    source_path: Path | None = None
    download_started = time.perf_counter()
    try:
        source_path = client.download(scan, scan_cache, max_source_bytes)
        download_seconds = time.perf_counter() - download_started
        render_started = time.perf_counter()
        fallback_time: dt.datetime | None = None
        fallback_source: str | None = None
        if scan.sector.id == PACUS.id:
            fallback_path, fallback_metadata, fallback_time = select_fallback(
                root,
                scan.valid_time,
                scan.source_time,
            )
            fallback_source = str(fallback_metadata.get("source") or "NOAA Open Data")
        source_times = {
            (
                "NOAA STAR GOES-18 full-disk GeoColor"
                if scan.sector.id == FULL_DISK.id
                else "NOAA STAR GOES-18 PACUS GeoColor"
            ): scan.source_time,
        }
        if fallback_time is not None:
            source_times[
                (
                    "NOAA STAR GOES-18 full-disk GeoColor fallback"
                    if fallback_source == SOURCE
                    else "Raw NOAA GOES-18 full-disk fallback"
                )
            ] = fallback_time
        for target in render_targets(scan.sector):
            domain = DOMAINS[target.domain_id]
            layer = LAYERS[target.layer_id]
            rendered, coverage = projector(
                source_path,
                scan.sector,
                domain,
                scan_cache,
                width=target.width,
                height=target.height,
            )
            if fallback_time is not None:
                rendered = composite_over_fallback(rendered, coverage, fallback_path)
            pacific_fallback: tuple[Path, dict[str, object], dt.datetime] | None = None
            if domain.id == "north-pacific":
                pacific_fallback = select_pacific_fallback(root, scan.valid_time)
                if pacific_fallback is not None:
                    rendered = composite_over_fallback(
                        rendered,
                        coverage,
                        pacific_fallback[0],
                    )
            destination = frame_path(root, domain, layer, scan.valid_time)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f"{destination.name}.{os.getpid()}.tmp"
            )
            Image.fromarray(rendered).save(
                temporary,
                "WEBP",
                quality=86,
                method=4,
            )
            temporary.replace(destination)
            is_native_layer = target.layer_id == scan.sector.layer_id
            target_source_times = dict(source_times)
            if pacific_fallback is not None:
                target_source_times["Pacific western-disk fallback"] = pacific_fallback[2]
            write_metadata(
                root,
                domain,
                layer,
                scan.valid_time,
                destination,
                target_source_times,
                source=SOURCE,
                source_layer=(
                    "CIRA GeoColor on the GOES-18 ABI 0.5 km full-disk grid"
                    if scan.sector.id == FULL_DISK.id
                    else "CIRA GeoColor on the GOES-18 ABI 0.5 km PACUS grid "
                    "over ten-minute full-disk fallback"
                ),
                extra={
                    "renderVersion": (
                        RENDER_VERSION
                        if is_native_layer
                        else RAW_VISIR_RENDER_VERSION
                    ),
                    "starRenderVersion": RENDER_VERSION,
                    "nativeResolution": is_native_layer,
                    "sourceGridResolutionKm": 0.5,
                    "renderWidth": target.width,
                    "renderHeight": target.height,
                    "nominalCadenceMinutes": scan.sector.cadence_minutes,
                    "retentionHours": 24,
                    "sourceFile": scan.filename,
                    "sourceBytes": scan.size,
                    **(
                        {
                            "westFallbackSource": pacific_fallback[1].get("source"),
                            "westFallbackValidTime": format_utc(pacific_fallback[2]),
                        }
                        if pacific_fallback is not None
                        else {}
                    ),
                    **(
                        {
                            "pacusNorthBoundDegrees": 53.5,
                            "fallbackSourceTime": format_utc(fallback_time),
                            "fallbackSource": fallback_source,
                        }
                        if fallback_time is not None
                        else {}
                    ),
                },
            )
        return ScanResult(
            scan.valid_time,
            scan.source_time,
            "rendered",
            scan.size,
            download_seconds,
            time.perf_counter() - render_started,
        )
    finally:
        if source_path is not None:
            source_path.unlink(missing_ok=True)
        shutil.rmtree(scan_cache, ignore_errors=True)


def execute_backfill(
    root: Path,
    plan: PlannedBackfill,
    processor: Callable[[StarScan], ScanResult],
    *,
    rebuild_catalog: bool = True,
) -> BackfillResult:
    result = BackfillResult(plan)
    for scan in plan.scans:
        try:
            result.scans.append(processor(scan))
        except Exception as error:
            result.scans.append(
                ScanResult(
                    scan.valid_time,
                    scan.source_time,
                    "failed",
                    scan.size,
                    error=f"{type(error).__name__}: {error}",
                )
            )
    if rebuild_catalog and result.scans:
        write_catalog(root)
    return result
