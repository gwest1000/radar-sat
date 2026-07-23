"""Bounded five-minute GOES-18 PACUS ingest for southern British Columbia.

PACUS reaches about 53.5 N.  Each frame is therefore composited over the most
recent ten-minute full-disk BC image: observed PACUS pixels update at five
minutes while northern BC remains honest ten-minute fallback imagery.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import distance_transform_edt

from .catalog import write_catalog
from .config import DOMAINS, LAYERS, Domain
from .geomet import format_utc
from .pipeline import frame_path, metadata_path, safe_archive_path, write_metadata
from .raw_satellite import (
    GOES_FILENAME,
    PublicObject,
    PublicSatelliteClient,
    RenderedSatellite,
    clear_downloads,
    compose_visible_infrared,
    render_satpy_domain_isolated,
)


UTC = dt.timezone.utc
PRODUCT = "ABI-L2-MCMIPC"
SOURCE = "NOAA GOES-18"
SOURCE_LAYER = "ABI-L2-MCMIPC"
RENDER_VERSION = 1
DEFAULT_MAX_SOURCE_BYTES = 100_000_000


@dataclass(frozen=True)
class FiveMinuteScan:
    valid_time: dt.datetime
    source_time: dt.datetime
    source: PublicObject

    @property
    def size(self) -> int:
        return self.source.size


@dataclass(frozen=True)
class DiscoveryResult:
    scans: tuple[FiveMinuteScan, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedBackfill:
    scans: tuple[FiveMinuteScan, ...]
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


def _scan_start(key: str) -> dt.datetime | None:
    match = GOES_FILENAME.search(key)
    if match is None:
        return None
    return dt.datetime.strptime(
        "".join(match.group(name) for name in ("year", "day", "hour", "minute", "second")),
        "%Y%j%H%M%S",
    ).replace(tzinfo=UTC)


def _nominal_time(source_time: dt.datetime) -> dt.datetime:
    current = source_time.astimezone(UTC)
    return current.replace(
        minute=(current.minute // 5) * 5,
        second=0,
        microsecond=0,
    )


def _hour_starts(start: dt.datetime, end: dt.datetime) -> Iterable[dt.datetime]:
    current = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    final = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    while current <= final:
        yield current
        current += dt.timedelta(hours=1)


def discover_scans(
    client: PublicSatelliteClient,
    start: dt.datetime,
    end: dt.datetime,
) -> DiscoveryResult:
    first = start.astimezone(UTC)
    last = end.astimezone(UTC)
    if first > last:
        raise ValueError("start must not be after end")
    found: dict[dt.datetime, FiveMinuteScan] = {}
    warnings: list[str] = []
    for hour in _hour_starts(first, last):
        prefix = f"{PRODUCT}/{hour:%Y}/{hour:%j}/{hour:%H}/"
        try:
            listing = client.list_prefix("noaa-goes18", prefix)
        except Exception as error:
            warnings.append(f"{prefix}: {type(error).__name__}: {error}")
            continue
        for key, size in listing:
            source_time = _scan_start(key)
            if source_time is None or source_time < first or source_time > last:
                continue
            nominal = _nominal_time(source_time)
            scan = FiveMinuteScan(
                nominal,
                source_time,
                PublicObject("noaa-goes18", key, size, source_time),
            )
            previous = found.get(nominal)
            if previous is None or scan.source.key > previous.source.key:
                found[nominal] = scan
    return DiscoveryResult(
        tuple(sorted(found.values(), key=lambda item: item.valid_time, reverse=True)),
        tuple(warnings),
    )


def scan_ready(root: Path, scan: FiveMinuteScan) -> bool:
    domain = DOMAINS["bc"]
    layer = LAYERS["raw-visir-5min"]
    image = frame_path(root, domain, layer, scan.valid_time)
    metadata = metadata_path(root, domain, layer, scan.valid_time)
    if not image.is_file() or not metadata.is_file():
        return False
    try:
        payload = json.loads(metadata.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("renderVersion") == RENDER_VERSION
        and payload.get("sourceFile") == Path(scan.source.key).name
    )


def plan_backfill(
    root: Path,
    discovery: DiscoveryResult,
    *,
    max_frames: int,
    max_download_bytes: int,
    overwrite: bool = False,
) -> PlannedBackfill:
    if max_frames <= 0 or max_download_bytes <= 0:
        raise ValueError("five-minute BC limits must be positive")
    pending = [
        scan for scan in discovery.scans
        if overwrite or not scan_ready(root, scan)
    ]
    skipped_ready = len(discovery.scans) - len(pending)
    frame_limited = pending[:max_frames]
    selected: list[FiveMinuteScan] = []
    estimated_bytes = 0
    excluded_by_byte_limit = 0
    for index, scan in enumerate(frame_limited):
        if estimated_bytes + scan.size > max_download_bytes:
            excluded_by_byte_limit = len(frame_limited) - index
            break
        selected.append(scan)
        estimated_bytes += scan.size
    return PlannedBackfill(
        tuple(selected),
        estimated_bytes,
        skipped_ready,
        max(0, len(pending) - len(frame_limited)),
        excluded_by_byte_limit,
        discovery.warnings,
    )


def _fallback_frame(
    root: Path,
    source_time: dt.datetime,
    *,
    max_age_minutes: int = 25,
) -> tuple[Path, dict[str, object]]:
    metadata_root = root / "metadata" / "bc" / "raw-visir"
    selected: tuple[dt.datetime, Path, dict[str, object]] | None = None
    for path in metadata_root.rglob("*.json") if metadata_root.exists() else ():
        try:
            payload = json.loads(path.read_text())
            valid_time = dt.datetime.fromisoformat(str(payload["validTime"]).replace("Z", "+00:00"))
            image = safe_archive_path(root, str(payload["path"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if not image.is_file() or valid_time > source_time:
            continue
        if selected is None or valid_time > selected[0]:
            selected = (valid_time, image, payload)
    if selected is None or source_time - selected[0] > dt.timedelta(minutes=max_age_minutes):
        raise RuntimeError("no recent ten-minute BC full-disk fallback is available")
    return selected[1], selected[2]


def composite_over_fallback(
    pacus_path: Path,
    valid_mask_path: Path,
    fallback_path: Path,
    destination: Path,
) -> None:
    with Image.open(pacus_path) as pacus_image, Image.open(valid_mask_path) as mask_image, Image.open(fallback_path) as fallback_image:
        pacus = pacus_image.convert("RGBA")
        fallback = fallback_image.convert("RGBA")
        if fallback.size != pacus.size:
            fallback = fallback.resize(pacus.size, Image.Resampling.LANCZOS)
        mask = ImageOps.grayscale(mask_image)
        if mask.size != pacus.size:
            mask = mask.resize(pacus.size, Image.Resampling.NEAREST)
        # Respect both Satpy's projected validity mask and the composite alpha,
        # then soften only the scan edge to avoid a one-pixel seam.
        valid = np.minimum(
            np.asarray(mask),
            np.asarray(pacus.getchannel("A")),
        ) > 127
        # Ramp inward from every part of the curved PACUS scan edge. A normal
        # symmetric blur leaves a visible 50/50 footprint arc; the inward
        # distance ramp reaches zero exactly at the edge and becomes fully
        # five-minute imagery roughly 200 km inside it.
        feather_pixels = max(2, min(120, round(min(pacus.size) * 0.08)))
        edge_weight = np.clip(distance_transform_edt(valid) / feather_pixels, 0, 1)
        edge_weight = edge_weight * edge_weight * (3 - 2 * edge_weight)
        mask = Image.fromarray((edge_weight * 255).astype("uint8"))
        output = Image.composite(pacus, fallback, mask).convert("RGB")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            output.save(temporary, "WEBP", quality=86, method=6)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


RenderSource = Callable[
    [Iterable[Path], str, str, Domain, Path, str], RenderedSatellite
]


def render_scan(
    root: Path,
    scan: FiveMinuteScan,
    client: PublicSatelliteClient,
    cache_root: Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    overwrite: bool = False,
    render_source: RenderSource = render_satpy_domain_isolated,
) -> ScanResult:
    if not overwrite and scan_ready(root, scan):
        return ScanResult(scan.valid_time, scan.source_time, "skipped", scan.size)
    if scan.size > max_source_bytes:
        raise RuntimeError(
            f"PACUS source is {scan.size:,} bytes, above the {max_source_bytes:,}-byte cap"
        )
    fallback_path, fallback_metadata = _fallback_frame(root, scan.source_time)
    cache_root.mkdir(parents=True, exist_ok=True)
    clear_downloads(cache_root)
    source_path: Path | None = None
    download_started = time.perf_counter()
    try:
        source_path = client.download(scan.source, cache_root, max_source_bytes)
        download_seconds = time.perf_counter() - download_started
        render_started = time.perf_counter()
        domain = DOMAINS["bc"]
        stem = f"five-minute-bc-g18-{scan.source_time:%Y%m%dT%H%M%S}"
        rendered = render_source(
            [source_path], "abi_l2_nc", "C13", domain, cache_root, stem
        )
        staging = cache_root / "five-minute-staging"
        staging.mkdir(parents=True, exist_ok=True)
        pacus_visir = staging / f"{stem}-pacus.webp"
        compose_details = compose_visible_infrared(
            rendered.visible,
            rendered.infrared_gray,
            domain,
            scan.source_time,
            pacus_visir,
        )
        layer = LAYERS["raw-visir-5min"]
        destination = frame_path(root, domain, layer, scan.valid_time)
        composite_over_fallback(
            pacus_visir,
            rendered.valid_mask,
            fallback_path,
            destination,
        )
        fallback_source_times = fallback_metadata.get("sourceTimes")
        fallback_time = str(fallback_metadata.get("validTime"))
        if isinstance(fallback_source_times, dict) and fallback_source_times:
            fallback_time = max(
                (str(value) for value in fallback_source_times.values()),
                key=lambda value: dt.datetime.fromisoformat(value.replace("Z", "+00:00")),
            )
        write_metadata(
            root,
            domain,
            layer,
            scan.valid_time,
            destination,
            {
                "GOES-18 PACUS scan start": scan.source_time,
                "GOES-18 full-disk fallback": dt.datetime.fromisoformat(fallback_time.replace("Z", "+00:00")),
            },
            source=SOURCE,
            source_layer=f"{SOURCE_LAYER} five-minute PACUS over ten-minute full-disk fallback",
            extra={
                **compose_details,
                "renderVersion": RENDER_VERSION,
                "nominalCadenceMinutes": 5,
                "pacusNorthBoundDegrees": 53.5,
                "retentionHours": 24,
                "sourceFile": Path(scan.source.key).name,
                "sourceBytes": scan.size,
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
        clear_downloads(cache_root)
        shutil.rmtree(cache_root / "renders", ignore_errors=True)
        shutil.rmtree(cache_root / "five-minute-staging", ignore_errors=True)


def execute_backfill(
    root: Path,
    plan: PlannedBackfill,
    processor: Callable[[FiveMinuteScan], ScanResult],
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
