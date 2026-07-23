"""Dedicated ten-minute GOES-18 satellite ingest for WestWX.

This module intentionally does not change the lower-cadence, multi-satellite
path in :mod:`radarsat.pipeline`. It downloads each GOES-18 scan once, renders
the ``north-america`` WestWX layers, and reuses the same source for Radar-Sat's
BC raw layers at the genuine ten-minute scan cadence.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image

from .catalog import write_catalog
from .config import DOMAINS, LAYERS, Domain
from .geomet import format_utc
from .pipeline import frame_path, metadata_path, write_metadata
from .raw_satellite import (
    GOES_BUCKETS,
    GOES_FILENAME,
    PublicObject,
    PublicSatelliteClient,
    RenderedSatellite,
    clear_downloads,
    compose_visible_infrared,
    render_satpy_domain_isolated,
)


UTC = dt.timezone.utc
WESTWX_DOMAIN_ID = "north-america"
WESTWX_DOMAIN_IDS = (WESTWX_DOMAIN_ID, "bc")
WESTWX_PRODUCT = "ABI-L2-MCMIPF"
WESTWX_SOURCE = "NOAA GOES-18"
WESTWX_SOURCE_LAYER = "ABI-L2-MCMIPF"
WESTWX_RENDER_VERSION = 3
DEFAULT_MAX_SOURCE_BYTES = 400_000_000


class WestWxDownloadBudgetError(RuntimeError):
    """Raised when an explicit backfill plan exceeds its download budget."""


@dataclass(frozen=True)
class DiscoveryResult:
    scans: tuple[PublicObject, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedBackfill:
    scans: tuple[PublicObject, ...]
    estimated_bytes: int
    skipped_ready: int
    excluded_by_frame_limit: int
    excluded_by_byte_limit: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    valid_time: dt.datetime
    status: str
    source_bytes: int
    download_seconds: float = 0.0
    render_seconds: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "validTime": format_utc(self.valid_time),
            "status": self.status,
            "sourceBytes": self.source_bytes,
            "downloadSeconds": round(self.download_seconds, 3),
            "renderSeconds": round(self.render_seconds, 3),
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class BackfillResult:
    planned: PlannedBackfill
    scans: list[ScanResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(item.status == "failed" for item in self.scans):
            return "warning"
        if any(item.status == "rendered" for item in self.scans):
            return "rendered"
        return "unchanged"

    @property
    def downloaded_bytes(self) -> int:
        return sum(item.source_bytes for item in self.scans if item.status == "rendered")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "plannedFrames": len(self.planned.scans),
            "estimatedBytes": self.planned.estimated_bytes,
            "downloadedBytes": self.downloaded_bytes,
            "skippedReady": self.planned.skipped_ready,
            "excludedByFrameLimit": self.planned.excluded_by_frame_limit,
            "excludedByByteLimit": self.planned.excluded_by_byte_limit,
            "warnings": list(self.planned.warnings),
            "scans": [item.as_dict() for item in self.scans],
        }


def _scan_start_from_key(key: str) -> dt.datetime | None:
    match = GOES_FILENAME.search(key)
    if match is None:
        return None
    return dt.datetime.strptime(
        f"{match.group('year')}{match.group('day')}{match.group('hour')}"
        f"{match.group('minute')}{match.group('second')}",
        "%Y%j%H%M%S",
    ).replace(tzinfo=UTC)


def _hour_starts(start: dt.datetime, end: dt.datetime) -> Iterable[dt.datetime]:
    current = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    final = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    while current <= final:
        yield current
        current += dt.timedelta(hours=1)


def discover_goes18_scans(
    client: PublicSatelliteClient,
    start: dt.datetime,
    end: dt.datetime,
) -> DiscoveryResult:
    """List completed GOES-18 full-disk scans without changing their times.

    A failure listing one source hour is isolated so a bounded recovery job can
    continue with every other available hour.  Unlike the Forecast Graphics
    discovery method, no half-hour cadence filter or timestamp normalization is
    applied here.
    """

    first = start.astimezone(UTC)
    last = end.astimezone(UTC)
    if first > last:
        raise ValueError("start must not be after end")
    bucket = GOES_BUCKETS["G18"]
    found: dict[dt.datetime, PublicObject] = {}
    warnings: list[str] = []
    for hour in _hour_starts(first, last):
        prefix = f"{WESTWX_PRODUCT}/{hour:%Y}/{hour:%j}/{hour:%H}/"
        try:
            objects = client.list_prefix(bucket, prefix)
        except Exception as error:
            warnings.append(
                f"{prefix}: {type(error).__name__}: {error}"
            )
            continue
        for key, size in objects:
            valid_time = _scan_start_from_key(key)
            if valid_time is None or valid_time < first or valid_time > last:
                continue
            # Mode-6 full-disk scans start at ten-minute intervals.  Preserve
            # the seconds from NOAA's filename instead of relabelling a frame.
            if valid_time.minute % 10 != 0:
                continue
            item = PublicObject(bucket, key, size, valid_time)
            previous = found.get(valid_time)
            if previous is None or item.key > previous.key:
                found[valid_time] = item
    scans = tuple(sorted(found.values(), key=lambda item: item.valid_time, reverse=True))
    return DiscoveryResult(scans, tuple(warnings))


def _metadata_matches(path: Path, scan: PublicObject) -> bool:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("renderVersion") == WESTWX_RENDER_VERSION
        and payload.get("sourceFile") == Path(scan.key).name
        and payload.get("validTime") == format_utc(scan.valid_time)
    )


def westwx_scan_ready(
    root: Path,
    scan: PublicObject,
    domain: Domain | None = None,
) -> bool:
    selected_domains = (domain,) if domain is not None else tuple(
        DOMAINS[domain_id] for domain_id in WESTWX_DOMAIN_IDS
    )
    return all(_domain_scan_ready(root, scan, selected) for selected in selected_domains)


def _layer_ids(domain: Domain) -> tuple[str, str]:
    if domain.id == "bc":
        return "raw-visir", "raw-ir"
    return "westwx-visir", "westwx-ir"


def _domain_scan_ready(root: Path, scan: PublicObject, domain: Domain) -> bool:
    for layer_id in _layer_ids(domain):
        layer = LAYERS[layer_id]
        image = frame_path(root, domain, layer, scan.valid_time)
        metadata = metadata_path(root, domain, layer, scan.valid_time)
        if not image.is_file() or not _metadata_matches(metadata, scan):
            return False
    return True


def plan_backfill(
    root: Path,
    discovery: DiscoveryResult,
    *,
    max_frames: int,
    max_download_bytes: int,
    overwrite: bool = False,
    domain: Domain | None = None,
) -> PlannedBackfill:
    """Return a newest-first plan bounded by both frames and source bytes."""

    if max_frames <= 0:
        raise ValueError("max_frames must be positive")
    if max_download_bytes <= 0:
        raise ValueError("max_download_bytes must be positive")
    pending: list[PublicObject] = []
    skipped_ready = 0
    for scan in discovery.scans:
        if not overwrite and westwx_scan_ready(root, scan, domain):
            skipped_ready += 1
            continue
        pending.append(scan)

    frame_limited = pending[:max_frames]
    excluded_by_frame_limit = max(0, len(pending) - len(frame_limited))
    planned: list[PublicObject] = []
    estimated_bytes = 0
    excluded_by_byte_limit = 0
    for index, scan in enumerate(frame_limited):
        if estimated_bytes + scan.size > max_download_bytes:
            # Keep the plan a contiguous newest-first prefix.  Selecting a
            # smaller, older file after skipping a newer scan makes catch-up
            # behaviour surprising and can leave holes near the live edge.
            excluded_by_byte_limit = len(frame_limited) - index
            break
        planned.append(scan)
        estimated_bytes += scan.size
    return PlannedBackfill(
        tuple(planned),
        estimated_bytes,
        skipped_ready,
        excluded_by_frame_limit,
        excluded_by_byte_limit,
        discovery.warnings,
    )


def _stage_rgb(source_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as source:
        source.convert("RGB").save(
            destination,
            "WEBP",
            quality=88,
            method=6,
        )


def _install_staged(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


RenderSource = Callable[
    [Iterable[Path], str, str, Domain, Path, str],
    RenderedSatellite,
]


def render_westwx_scan(
    root: Path,
    scan: PublicObject,
    client: PublicSatelliteClient,
    cache_root: Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    overwrite: bool = False,
    domain: Domain | None = None,
    render_source: RenderSource = render_satpy_domain_isolated,
) -> ScanResult:
    """Download once and atomically install every requested GOES-18 grid."""

    selected_domains = (domain,) if domain is not None else tuple(
        DOMAINS[domain_id] for domain_id in WESTWX_DOMAIN_IDS
    )
    if any(selected.id not in WESTWX_DOMAIN_IDS for selected in selected_domains):
        raise ValueError("the WestWX rapid path may render only north-america and bc")
    pending_domains = tuple(
        selected
        for selected in selected_domains
        if overwrite or not _domain_scan_ready(root, scan, selected)
    )
    if not pending_domains:
        return ScanResult(scan.valid_time, "skipped", scan.size)
    if scan.size > max_source_bytes:
        raise WestWxDownloadBudgetError(
            f"source object is {scan.size:,} bytes, above the {max_source_bytes:,}-byte per-file cap"
        )

    cache_root.mkdir(parents=True, exist_ok=True)
    clear_downloads(cache_root)
    source_path: Path | None = None
    download_started = time.perf_counter()
    try:
        source_path = client.download(scan, cache_root, max_source_bytes)
        download_seconds = time.perf_counter() - download_started
        render_started = time.perf_counter()
        def render_domain(selected_domain: Domain) -> None:
            stem = f"westwx-g18-{selected_domain.id}-{scan.valid_time:%Y%m%dT%H%M%S}"
            rendered = render_source(
                [source_path],
                "abi_l2_nc",
                "C13",
                selected_domain,
                cache_root,
                stem,
            )
            staging = cache_root / "westwx-staging" / selected_domain.id
            staging.mkdir(parents=True, exist_ok=True)
            staged_visir = staging / f"{stem}-visir.webp"
            staged_ir = staging / f"{stem}-ir.webp"
            compose_details = compose_visible_infrared(
                rendered.visible,
                rendered.infrared_gray,
                selected_domain,
                scan.valid_time,
                staged_visir,
            )
            _stage_rgb(rendered.infrared, staged_ir)

            visir_id, ir_id = _layer_ids(selected_domain)
            visir_layer = LAYERS[visir_id]
            ir_layer = LAYERS[ir_id]
            visir_destination = frame_path(
                root, selected_domain, visir_layer, scan.valid_time
            )
            ir_destination = frame_path(root, selected_domain, ir_layer, scan.valid_time)
            _install_staged(staged_visir, visir_destination)
            _install_staged(staged_ir, ir_destination)
            common_extra: dict[str, object] = {
                "renderVersion": WESTWX_RENDER_VERSION,
                "sourceFile": Path(scan.key).name,
                "sourceBytes": scan.size,
                "scanStart": format_utc(scan.valid_time),
                "nominalCadenceMinutes": 10,
                "westwxOnly": True,
                "rapidDomain": selected_domain.id,
            }
            source_times = {"GOES-18 ABI scan start": scan.valid_time}
            write_metadata(
                root,
                selected_domain,
                visir_layer,
                scan.valid_time,
                visir_destination,
                source_times,
                source=WESTWX_SOURCE,
                source_layer=f"{WESTWX_SOURCE_LAYER} true-colour/IR blend",
                extra={**common_extra, **compose_details},
            )
            write_metadata(
                root,
                selected_domain,
                ir_layer,
                scan.valid_time,
                ir_destination,
                source_times,
                source=WESTWX_SOURCE,
                source_layer=f"{WESTWX_SOURCE_LAYER} C13",
                extra=common_extra,
            )
        # North America and BC use the same downloaded full-disk scan but
        # independent output grids and render subprocesses. Running them
        # concurrently keeps one ten-minute source scan from consuming the
        # sum of both resampling times.
        with ThreadPoolExecutor(max_workers=len(pending_domains)) as executor:
            list(executor.map(render_domain, pending_domains))
        render_seconds = time.perf_counter() - render_started
        return ScanResult(
            scan.valid_time,
            "rendered",
            scan.size,
            download_seconds=download_seconds,
            render_seconds=render_seconds,
        )
    finally:
        if source_path is not None:
            source_path.unlink(missing_ok=True)
        clear_downloads(cache_root)
        # ``satpy-data`` is intentionally retained, but per-scan rasters are
        # discarded so a 24-hour catch-up does not accumulate another large
        # working set alongside the final archive.
        shutil.rmtree(cache_root / "renders", ignore_errors=True)
        shutil.rmtree(cache_root / "westwx-staging", ignore_errors=True)


ScanProcessor = Callable[[PublicObject], ScanResult]


def execute_backfill(
    root: Path,
    plan: PlannedBackfill,
    processor: ScanProcessor,
    *,
    rebuild_catalog: bool = True,
) -> BackfillResult:
    """Run every planned scan independently and continue after failures."""

    result = BackfillResult(plan)
    for scan in plan.scans:
        try:
            result.scans.append(processor(scan))
        except Exception as error:
            result.scans.append(
                ScanResult(
                    scan.valid_time,
                    "failed",
                    scan.size,
                    error=f"{type(error).__name__}: {error}",
                )
            )
    if rebuild_catalog and result.scans:
        write_catalog(root)
    return result
