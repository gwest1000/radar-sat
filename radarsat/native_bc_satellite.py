from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from .catalog import write_catalog
from .config import DOMAINS, LAYERS, Domain
from .geomet import format_utc
from .pipeline import frame_path, metadata_path, write_metadata
from .raw_satellite import (
    GOES_FILENAME,
    PublicObject,
    PublicSatelliteClient,
    RenderedSatellite,
    clear_downloads,
    compose_visible_infrared,
    render_satpy_domain_isolated,
    solar_daylight_weight,
)


UTC = dt.timezone.utc
NATIVE_BC_PRODUCT = "ABI-L2-CMIPF"
NATIVE_BC_CHANNELS = ("01", "02", "03", "13")
NATIVE_BC_RENDER_VERSION = 1
NATIVE_BC_WIDTH = 3000
NATIVE_BC_HEIGHT = 2300
NATIVE_BC_SOURCE = "NOAA GOES-18"
CHANNEL_RE = re.compile(r"M6C(?P<channel>01|02|03|13)_G18_")


@dataclass(frozen=True)
class NativeBcScan:
    valid_time: dt.datetime
    objects: tuple[PublicObject, ...]

    @property
    def size(self) -> int:
        return sum(item.size for item in self.objects)


@dataclass(frozen=True)
class NativeBcDiscovery:
    scans: tuple[NativeBcScan, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeBcPlan:
    scans: tuple[NativeBcScan, ...]
    estimated_bytes: int
    skipped_ready: int
    skipped_night: int
    excluded_by_frame_limit: int
    excluded_by_byte_limit: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeBcResult:
    valid_time: dt.datetime
    status: str
    source_bytes: int
    download_seconds: float = 0.0
    render_seconds: float = 0.0
    error: str | None = None


@dataclass
class NativeBcBackfillResult:
    plan: NativeBcPlan
    scans: list[NativeBcResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "warning" if any(item.status == "failed" for item in self.scans) else "rendered"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "renderedFrames": sum(item.status == "rendered" for item in self.scans),
            "failedFrames": sum(item.status == "failed" for item in self.scans),
            "scans": [
                {
                    "validTime": format_utc(item.valid_time),
                    "status": item.status,
                    "sourceBytes": item.source_bytes,
                    "downloadSeconds": round(item.download_seconds, 3),
                    "renderSeconds": round(item.render_seconds, 3),
                    **({"error": item.error} if item.error else {}),
                }
                for item in self.scans
            ],
        }


def discover_native_bc_scans(
    client: PublicSatelliteClient,
    start: dt.datetime,
    end: dt.datetime,
) -> NativeBcDiscovery:
    current = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    final_hour = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    grouped: dict[dt.datetime, dict[str, PublicObject]] = {}
    warnings: list[str] = []
    while current <= final_hour:
        prefix = f"{NATIVE_BC_PRODUCT}/{current:%Y}/{current:%j}/{current:%H}/"
        try:
            listing = client.list_prefix("noaa-goes18", prefix)
        except Exception as error:
            warnings.append(f"{prefix}: {type(error).__name__}: {error}")
            current += dt.timedelta(hours=1)
            continue
        for key, size in listing:
            channel_match = CHANNEL_RE.search(key)
            time_match = GOES_FILENAME.search(key)
            if not channel_match or not time_match:
                continue
            valid_time = dt.datetime.strptime(
                "".join(time_match.group(name) for name in ("year", "day", "hour", "minute", "second")),
                "%Y%j%H%M%S",
            ).replace(tzinfo=UTC)
            if valid_time < start or valid_time > end:
                continue
            grouped.setdefault(valid_time, {})[channel_match.group("channel")] = PublicObject(
                "noaa-goes18", key, size, valid_time
            )
        current += dt.timedelta(hours=1)
    scans = tuple(
        NativeBcScan(valid_time, tuple(channels[channel] for channel in NATIVE_BC_CHANNELS))
        for valid_time, channels in sorted(grouped.items(), reverse=True)
        if all(channel in channels for channel in NATIVE_BC_CHANNELS)
    )
    return NativeBcDiscovery(scans, tuple(warnings))


def scan_has_bc_daylight(scan: NativeBcScan, domain: Domain | None = None) -> bool:
    selected = domain or DOMAINS["bc"]
    latitudes = np.array(
        [selected.south, selected.south, selected.north, selected.north, (selected.south + selected.north) / 2],
        dtype=np.float32,
    )
    longitudes = np.array(
        [selected.west, selected.east, selected.west, selected.east, (selected.west + selected.east) / 2],
        dtype=np.float32,
    )
    return bool(np.max(solar_daylight_weight(latitudes, longitudes, scan.valid_time)) > 0.02)


def native_bc_scan_ready(root: Path, scan: NativeBcScan) -> bool:
    domain = DOMAINS["bc"]
    layer = LAYERS["raw-visir-native"]
    image = frame_path(root, domain, layer, scan.valid_time)
    metadata = metadata_path(root, domain, layer, scan.valid_time)
    if not image.is_file() or not metadata.is_file():
        return False
    try:
        payload = json.loads(metadata.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("renderVersion") == NATIVE_BC_RENDER_VERSION
        and payload.get("sourceFiles") == [Path(item.key).name for item in scan.objects]
    )


def plan_native_bc_backfill(
    root: Path,
    discovery: NativeBcDiscovery,
    *,
    max_frames: int,
    max_download_bytes: int,
    overwrite: bool = False,
) -> NativeBcPlan:
    if max_frames <= 0 or max_download_bytes <= 0:
        raise ValueError("native BC limits must be positive")
    pending: list[NativeBcScan] = []
    skipped_ready = 0
    skipped_night = 0
    for scan in discovery.scans:
        if not scan_has_bc_daylight(scan):
            skipped_night += 1
            continue
        if not overwrite and native_bc_scan_ready(root, scan):
            skipped_ready += 1
            continue
        pending.append(scan)
    frame_limited = pending[:max_frames]
    selected: list[NativeBcScan] = []
    estimated_bytes = 0
    excluded_by_byte_limit = 0
    for index, scan in enumerate(frame_limited):
        if estimated_bytes + scan.size > max_download_bytes:
            excluded_by_byte_limit = len(frame_limited) - index
            break
        selected.append(scan)
        estimated_bytes += scan.size
    return NativeBcPlan(
        tuple(selected),
        estimated_bytes,
        skipped_ready,
        skipped_night,
        max(0, len(pending) - len(frame_limited)),
        excluded_by_byte_limit,
        discovery.warnings,
    )


RenderSource = Callable[[Iterable[Path], str, str, Domain, Path, str], RenderedSatellite]


def render_native_bc_scan(
    root: Path,
    scan: NativeBcScan,
    client: PublicSatelliteClient,
    cache_root: Path,
    *,
    max_source_bytes: int,
    overwrite: bool = False,
    render_source: RenderSource = render_satpy_domain_isolated,
) -> NativeBcResult:
    if not overwrite and native_bc_scan_ready(root, scan):
        return NativeBcResult(scan.valid_time, "skipped", scan.size)
    if scan.size > max_source_bytes:
        raise RuntimeError(
            f"native BC source set is {scan.size:,} bytes, above the {max_source_bytes:,}-byte cap"
        )
    cache_root.mkdir(parents=True, exist_ok=True)
    clear_downloads(cache_root)
    source_paths: list[Path] = []
    download_started = time.perf_counter()
    try:
        for item in scan.objects:
            source_paths.append(client.download(item, cache_root, max_source_bytes))
        download_seconds = time.perf_counter() - download_started
        render_started = time.perf_counter()
        base_domain = DOMAINS["bc"]
        render_domain = replace(base_domain, width=NATIVE_BC_WIDTH, height=NATIVE_BC_HEIGHT)
        stem = f"native-bc-g18-{scan.valid_time:%Y%m%dT%H%M%S}"
        rendered = render_source(
            source_paths,
            "abi_l2_nc",
            "C13",
            render_domain,
            cache_root,
            stem,
        )
        staging = cache_root / "native-bc-staging"
        staging.mkdir(parents=True, exist_ok=True)
        staged_visir = staging / f"{stem}-visir.webp"
        compose_details = compose_visible_infrared(
            rendered.visible,
            rendered.infrared_gray,
            render_domain,
            scan.valid_time,
            staged_visir,
        )
        layer = LAYERS["raw-visir-native"]
        destination = frame_path(root, base_domain, layer, scan.valid_time)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(staged_visir, temporary)
        temporary.replace(destination)
        write_metadata(
            root,
            base_domain,
            layer,
            scan.valid_time,
            destination,
            {f"GOES-18 ABI C{channel}": scan.valid_time for channel in NATIVE_BC_CHANNELS},
            source=NATIVE_BC_SOURCE,
            source_layer=f"{NATIVE_BC_PRODUCT} C01/C02/C03/C13 1 km daytime composite",
            extra={
                **compose_details,
                "renderVersion": NATIVE_BC_RENDER_VERSION,
                "nativeResolution": True,
                "renderWidth": NATIVE_BC_WIDTH,
                "renderHeight": NATIVE_BC_HEIGHT,
                "retentionHours": 24,
                "sourceFiles": [Path(item.key).name for item in scan.objects],
                "sourceBytes": scan.size,
            },
        )
        return NativeBcResult(
            scan.valid_time,
            "rendered",
            scan.size,
            download_seconds=download_seconds,
            render_seconds=time.perf_counter() - render_started,
        )
    finally:
        for source_path in source_paths:
            source_path.unlink(missing_ok=True)
        clear_downloads(cache_root)
        shutil.rmtree(cache_root / "renders", ignore_errors=True)
        shutil.rmtree(cache_root / "native-bc-staging", ignore_errors=True)


def execute_native_bc_backfill(
    root: Path,
    plan: NativeBcPlan,
    processor: Callable[[NativeBcScan], NativeBcResult],
) -> NativeBcBackfillResult:
    result = NativeBcBackfillResult(plan)
    for scan in plan.scans:
        try:
            result.scans.append(processor(scan))
        except Exception as error:
            result.scans.append(
                NativeBcResult(
                    scan.valid_time,
                    "failed",
                    scan.size,
                    error=f"{type(error).__name__}: {error}",
                )
            )
    if result.scans:
        write_catalog(root)
    return result
