from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable, Mapping

from PIL import Image

from .active_fires import (
    BCWS_ACTIVE_FIRE_URL,
    CWFIF_ACTIVE_FIRE_LAYER,
    NIFC_ACTIVE_FIRE_URL,
    fetch_bc_active_fires,
    fetch_canadian_active_fires,
    fetch_us_active_fires,
    project_active_fires,
)
from .catalog import write_catalog
from .config import DOMAINS, LAYERS, VIEWPORTS, Domain, Layer, regional_layer_id
from .geomet import GeoMetClient, LayerTimeline, at_or_before, format_utc, frame_stamp
from .hotspots import (
    CWFIS_HOTSPOT_LAYER,
    fetch_hotspots,
    project_hotspots,
    render_fire_overlay,
    render_hotspots,
)
from .images import (
    lightning_trail,
    reproject_overlay,
    render_static_maps,
    render_transmission_overlay,
    render_watershed_overlay,
    save_coverage,
    save_overlay,
    save_satellite,
)
from .paths import output_root as default_output_root
from .retention import keep_frame, keep_layer_frame
from .point_frames import (
    glm_point_rows,
    normalized_pixel,
    point_frame_metadata,
    points_from_lightning_density_png,
    radarsat_product_uses_layer,
    write_point_frame,
)


UTC = dt.timezone.utc
LIGHTNING_TRAIL_RENDER_VERSION = 10
LIGHTNING_HOUR_RENDER_VERSION = 4
LIGHTNING_FLASH_RENDER_VERSION = 9
LIGHTNING_REGIONAL_RENDER_VERSION = 8
LIGHTNING_REGIONAL_HOUR_RENDER_VERSION = 5
LIGHTNING_REGIONAL_FLASH_RENDER_VERSION = 11
LIGHTNING_POINT_RENDER_VERSION = 1
HOTSPOT_RENDER_VERSION = 4
HOTSPOT_POINT_RENDER_VERSION = 2
ACTIVE_FIRE_POINT_RENDER_VERSION = 4
FIRE_OVERLAY_RENDER_VERSION = 4
FIRE_BROAD_OVERLAY_RENDER_VERSION = 4
FIRE_REGIONAL_RENDER_VERSION = 7
RAW_SATELLITE_RENDER_VERSION = 1
RAW_VISIR_RENDER_VERSION = 4
SMOKE_RENDER_VERSION = 3
GLM_LIGHTNING_RENDER_VERSION = 2
GLM_LIGHTNING_TRAIL_RENDER_VERSION = 9
GLM_LIGHTNING_HOUR_RENDER_VERSION = 2
GLM_LIGHTNING_FLASH_RENDER_VERSION = 10
GLM_LIGHTNING_POINT_RENDER_VERSION = 2
GLM_LIGHTNING_LIVE_RENDER_VERSION = 1
COVERAGE_RENDER_VERSION = 3
PRECIP_OVERLAY_RENDER_VERSION = 1
REGIONAL_HAZARD_WIDTH = 3840
DETAILED_REGIONAL_HAZARD_WIDTH = 3840
BC_LIGHTNING_WIDTH = 2560
DETAILED_REGIONAL_SYMBOL_REFERENCE_WIDTH = 1440
BC_SMALL_LIGHTNING_SYMBOL_REFERENCE_WIDTH = 1600
BC_SMALL_NOTABLE_FIRE_SCALE = 0.85
BROAD_HAZARD_SCALE = 2
BROAD_FIRE_SYMBOL_REFERENCE_WIDTH = 1920
LIGHTNING_TRAIL_HOURS = 24.0
LIGHTNING_ARCHIVE_HOURS = 168.0
FIRE_ARCHIVE_HOURS = 168.0
STATIC_BOUNDARY_RENDER_VERSION = 4
STATIC_TRANSMISSION_RENDER_VERSION = 2
STATIC_WATERSHED_RENDER_VERSION = 2
REGIONAL_WATERSHED_WIDTH = 2880
DEFAULT_SOURCE_LAYERS = (
    "daynight",
    "ir",
    "convective",
    "snowfog",
    "radar-rain",
    "radar-snow",
    "radar-coverage",
    "ptype",
    "ptype-coverage",
    "lightning",
)
HIGH_RESOLUTION_PRECIP_LAYERS = frozenset(
    {"radar-rain", "radar-snow", "radar-coverage", "ptype", "ptype-coverage"}
)


def precipitation_render_domain(domain: Domain) -> Domain:
    """Return a screen-sharp grid without exceeding useful source detail."""
    render_width = 3000 if domain.id == "bc" else domain.width * 2
    render_height = max(1, round(render_width * domain.height / domain.width))
    return Domain(
        id=f"{domain.id}-precip-hires",
        title=domain.title,
        west=domain.west,
        south=domain.south,
        east=domain.east,
        north=domain.north,
        crs=domain.crs,
        width=render_width,
        height=render_height,
        tier=domain.tier,
        projected_bounds=domain.projected_bounds,
    )


def geomet_render_version(layer_id: str) -> int | None:
    if layer_id.endswith("coverage"):
        return COVERAGE_RENDER_VERSION
    if layer_id in HIGH_RESOLUTION_PRECIP_LAYERS:
        return PRECIP_OVERLAY_RENDER_VERSION
    return None

def safe_archive_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"Archive metadata path escapes output root: {relative!r}")
    return candidate


def frame_path(root: Path, domain: Domain, layer: Layer, valid_time: dt.datetime) -> Path:
    date = valid_time.astimezone(UTC)
    return (
        root
        / "frames"
        / domain.id
        / layer.id
        / date.strftime("%Y")
        / date.strftime("%m")
        / date.strftime("%d")
        / f"{frame_stamp(date)}.{layer.extension}"
    )


def metadata_path(root: Path, domain: Domain, layer: Layer, valid_time: dt.datetime) -> Path:
    date = valid_time.astimezone(UTC)
    return (
        root
        / "metadata"
        / domain.id
        / layer.id
        / date.strftime("%Y")
        / date.strftime("%m")
        / date.strftime("%d")
        / f"{frame_stamp(date)}.json"
    )


def write_metadata(
    root: Path,
    domain: Domain,
    layer: Layer,
    valid_time: dt.datetime,
    image_path: Path,
    source_times: dict[str, dt.datetime] | None = None,
    *,
    source: str | None = None,
    source_layer: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    destination = metadata_path(root, domain, layer, valid_time)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "validTime": format_utc(valid_time),
        "path": image_path.relative_to(root).as_posix(),
        "source": source or layer.source,
        "sourceLayer": source_layer or layer.source_layer or "derived",
        "fetchedAt": format_utc(dt.datetime.now(UTC)),
    }
    if source_times:
        payload["sourceTimes"] = {key: format_utc(value) for key, value in source_times.items()}
    if extra:
        protected = {
            "validTime",
            "path",
            "source",
            "sourceLayer",
            "fetchedAt",
            "sourceTimes",
        }.intersection(extra)
        if protected:
            raise ValueError(f"Extra metadata cannot replace standard fields: {sorted(protected)}")
        payload.update(extra)
    # Independent rapid workers can legitimately complete the same timestamp
    # at once (for example a repair overlapping the scheduled edge refresh).
    # A process-specific staging name keeps one atomic replace from removing
    # another writer's temporary file.
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(destination)


def selected_times(times: Iterable[dt.datetime], hours: float, latest_only: bool) -> list[dt.datetime]:
    values = sorted(set(times))
    if not values:
        return []
    if latest_only:
        return [values[-1]]
    cutoff = values[-1] - dt.timedelta(hours=hours)
    return [value for value in values if value >= cutoff]


def retained_times(
    times: Iterable[dt.datetime],
    hours: float,
    latest_only: bool,
    now: dt.datetime,
    tier: str,
) -> list[dt.datetime]:
    """Select only source times that can survive the archive policy.

    A long bootstrap should not download every high-frequency source image and
    then immediately remove most older frames during ``prune``. Apply the same
    retention rule before each WMS request while preserving the latest-only
    probe used by diagnostics.
    """
    values = selected_times(times, hours, latest_only)
    if latest_only:
        return values
    # Keep the genuine six-minute ECCC radar clock during the first day on
    # continental/Pacific displays. ``keep_frame`` still thins broad archives
    # older than 24 hours to hourly, so the storage increase remains small.
    return [value for value in values if keep_frame(value, now, tier)]


def ingest_hotspot_snapshot(
    root: Path,
    domain: Domain,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Archive a ten-minute snapshot of CWFIS's rolling 24-hour BC hotspots."""
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    valid_time = current.replace(
        minute=(current.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    layer = LAYERS["hotspots"]
    point_layer = LAYERS["hotspot-points"]
    destination = frame_path(root, domain, layer, valid_time)
    metadata = metadata_path(root, domain, layer, valid_time)
    legacy_ready = False
    if destination.exists() and metadata.exists():
        try:
            existing = json.loads(metadata.read_text())
            if existing.get("renderVersion") == HOTSPOT_RENDER_VERSION:
                legacy_ready = True
        except (OSError, json.JSONDecodeError):
            pass
    point_ready = _rendered_frame_ready(
        root,
        domain,
        point_layer,
        valid_time,
        HOTSPOT_POINT_RENDER_VERSION,
    )
    if legacy_ready and point_ready:
        return {
            "status": "unchanged",
            "validTime": format_utc(valid_time),
            "detectionCount": existing.get("detectionCount", 0),
        }

    features = fetch_hotspots(domain)
    summary = render_hotspots(features, domain, destination, current)
    projected = project_hotspots(features, domain, current)
    points: list[list[float | int | None]] = []
    for point in projected:
        x, y = normalized_pixel(point.x, point.y, domain)
        points.append(
            [
                x,
                y,
                round(point.age_minutes, 3),
                round(point.frp, 3),
                point.count,
            ]
        )
    window_start = current - dt.timedelta(hours=24)
    point_destination = frame_path(root, domain, point_layer, valid_time)
    write_point_frame(
        point_destination,
        layer=point_layer.id,
        domain=domain,
        valid_time=valid_time,
        window_start=window_start,
        window_end=current,
        age_reference_time=current,
        point_schema=point_layer.point_schema,
        points=points,
        age_mode="exact-detection-time",
        age_precision_seconds=60,
    )
    write_metadata(
        root,
        domain,
        layer,
        valid_time,
        destination,
        {"hotspots": valid_time},
        source="NRCan CWFIS",
        source_layer=CWFIS_HOTSPOT_LAYER,
        extra={**summary, "renderVersion": HOTSPOT_RENDER_VERSION},
    )
    point_details = point_frame_metadata(
        points=points,
        point_schema=point_layer.point_schema,
        window_start=window_start,
        window_end=current,
        age_reference_time=current,
        age_mode="exact-detection-time",
        age_precision_seconds=60,
        render_version=HOTSPOT_POINT_RENDER_VERSION,
    )
    write_metadata(
        root,
        domain,
        point_layer,
        valid_time,
        point_destination,
        {"hotspots": current},
        source="NRCan CWFIS",
        source_layer=CWFIS_HOTSPOT_LAYER,
        extra=point_details,
    )
    return {
        "status": "rendered",
        "validTime": format_utc(valid_time),
        "pointCount": len(points),
        **summary,
    }


def ingest_active_fire_snapshot(
    root: Path,
    domain: Domain,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Archive current Canadian and U.S. agency-reported active wildfires."""
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    valid_time = current.replace(
        minute=(current.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    layer = LAYERS["active-fire-points"]
    if _rendered_frame_ready(
        root,
        domain,
        layer,
        valid_time,
        ACTIVE_FIRE_POINT_RENDER_VERSION,
    ):
        return {"status": "unchanged", "validTime": format_utc(valid_time)}

    canadian: list[dict[str, object]] = []
    british_columbia: list[dict[str, object]] = []
    united_states: list[dict[str, object]] = []
    source_errors: list[str] = []
    try:
        canadian = fetch_canadian_active_fires(current)
    except Exception as error:
        source_errors.append(f"CWFIF: {type(error).__name__}: {error}")
    try:
        british_columbia = fetch_bc_active_fires()
    except Exception as error:
        source_errors.append(f"BCWS: {type(error).__name__}: {error}")
    try:
        united_states = fetch_us_active_fires()
    except Exception as error:
        source_errors.append(f"NIFC WFIGS: {type(error).__name__}: {error}")
    if len(source_errors) == 3:
        raise RuntimeError("; ".join(source_errors))
    if source_errors:
        # Never archive a partial agency snapshot over a recent complete one.
        # Public ArcGIS services occasionally throttle an individual request;
        # keeping the last full frame is preferable to making one country of
        # fires disappear for ten minutes.
        metadata_root = root / "metadata" / domain.id / layer.id
        for previous_path in reversed(sorted(metadata_root.rglob("*.json"))) if metadata_root.exists() else []:
            try:
                previous = json.loads(previous_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            previous_errors = previous.get("sourceErrors")
            previous_frame = root / str(previous.get("path", ""))
            if previous_errors or not previous_frame.is_file():
                continue
            return {
                "status": "retained",
                "validTime": str(previous.get("validTime", "")),
                "pointCount": int(previous.get("pointCount", 0)),
                "canadianFeatureCount": int(previous.get("canadianFeatureCount", 0)),
                "bcwsFeatureCount": int(previous.get("bcwsFeatureCount", 0)),
                "usFeatureCount": int(previous.get("usFeatureCount", 0)),
                "warnings": source_errors,
            }

    projected = project_active_fires(
        canadian,
        united_states,
        domain,
        current,
        bc_features=british_columbia,
    )
    points: list[list[float | int | None]] = []
    for point in projected:
        x, y = normalized_pixel(point.x, point.y, domain)
        points.append(
            [
                x,
                y,
                round(point.status_age_minutes, 3) if point.status_age_minutes is not None else None,
                round(point.size_hectares, 3),
                point.source_code,
                point.highlight_code,
                point.status_code,
            ]
        )

    destination = frame_path(root, domain, layer, valid_time)
    write_point_frame(
        destination,
        layer=layer.id,
        domain=domain,
        valid_time=valid_time,
        window_start=current,
        window_end=current,
        age_reference_time=current,
        point_schema=layer.point_schema,
        points=points,
        age_mode="source-status-time",
        age_precision_seconds=60,
    )
    details = point_frame_metadata(
        points=points,
        point_schema=layer.point_schema,
        window_start=current,
        window_end=current,
        age_reference_time=current,
        age_mode="source-status-time",
        age_precision_seconds=60,
        render_version=ACTIVE_FIRE_POINT_RENDER_VERSION,
    )
    write_metadata(
        root,
        domain,
        layer,
        valid_time,
        destination,
        {
            "CWFIF active fires": current,
            "BCWS active fires": current,
            "NIFC WFIGS active fires": current,
        },
        source="NRCan CWFIS + BCWS + NIFC WFIGS",
        source_layer=(
            f"{CWFIF_ACTIVE_FIRE_LAYER} + {BCWS_ACTIVE_FIRE_URL} + {NIFC_ACTIVE_FIRE_URL}"
        ),
        extra={
            **details,
            "canadianFeatureCount": len(canadian),
            "bcwsFeatureCount": len(british_columbia),
            "usFeatureCount": len(united_states),
            "sourceErrors": source_errors,
        },
    )
    return {
        "status": "rendered",
        "validTime": format_utc(valid_time),
        "pointCount": len(points),
        "canadianFeatureCount": len(canadian),
        "bcwsFeatureCount": len(british_columbia),
        "usFeatureCount": len(united_states),
        "warnings": source_errors,
    }


def ensure_static_assets(client: GeoMetClient, root: Path, domain: Domain) -> None:
    version_path = root / "static" / domain.id / ".render-versions.json"
    try:
        static_versions = json.loads(version_path.read_text())
    except (OSError, json.JSONDecodeError):
        static_versions = {}
    base = root / "static" / domain.id / "base-dark.png"
    boundaries = root / "static" / domain.id / "boundaries.png"
    if (
        not base.exists()
        or not boundaries.exists()
        or static_versions.get("boundaries") != STATIC_BOUNDARY_RENDER_VERSION
    ):
        render_static_maps(domain, base, boundaries)
        static_versions["boundaries"] = STATIC_BOUNDARY_RENDER_VERSION
    watersheds = root / "static" / domain.id / "bch-watersheds.png"
    watershed_signature = {
        "renderVersion": STATIC_WATERSHED_RENDER_VERSION,
        "regionalWidth": REGIONAL_WATERSHED_WIDTH,
        "viewports": VIEWPORTS,
    }
    if domain.id == "bc":
        regional_watersheds = {
            region_id: root
            / "static"
            / domain.id
            / f"bch-watersheds-region-{region_id}.png"
            for region_id in VIEWPORTS
        }
        if (
            not watersheds.exists()
            or any(not path.exists() for path in regional_watersheds.values())
            or static_versions.get("watersheds") != watershed_signature
        ):
            render_watershed_overlay(domain, watersheds)
            for region_id, destination in regional_watersheds.items():
                render_watershed_overlay(
                    domain,
                    destination,
                    viewport=VIEWPORTS[region_id],
                    output_width=REGIONAL_WATERSHED_WIDTH,
                )
            static_versions["watersheds"] = watershed_signature
    transmission_lines = root / "static" / domain.id / "transmission-lines.png"
    if (
        not transmission_lines.exists()
        or static_versions.get("transmissionLines") != STATIC_TRANSMISSION_RENDER_VERSION
    ):
        render_transmission_overlay(
            domain,
            transmission_lines,
            output_width=domain.width * 2,
        )
        static_versions["transmissionLines"] = STATIC_TRANSMISSION_RENDER_VERSION
    version_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_versions = version_path.with_suffix(".tmp")
    try:
        temporary_versions.write_text(json.dumps(static_versions, indent=2) + "\n")
        temporary_versions.replace(version_path)
    finally:
        temporary_versions.unlink(missing_ok=True)

    legend_specs = {
        "legend-radar-rain.png": LAYERS["radar-rain"],
        "legend-radar-snow.png": LAYERS["radar-snow"],
        "legend-ptype.png": LAYERS["ptype"],
        "legend-lightning-density.png": LAYERS["lightning"],
    }
    for filename, layer in legend_specs.items():
        destination = root / "static" / filename
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = Image.open(io.BytesIO(client.get_legend(layer))).convert("RGBA")
        image.save(destination, "PNG", optimize=True)


def derive_eccc_lightning_points(
    root: Path,
    domain: Domain,
    valid_time: dt.datetime,
) -> dict[str, object]:
    """Derive app-native clusters from an archived ECCC density raster.

    The ECCC product is a ten-minute density aggregate, not strike-level data.
    Each point therefore uses the window midpoint age and ``count`` means the
    number of connected positive-density display cells in that cluster.
    """
    source_layer = LAYERS["lightning"]
    point_layer = LAYERS["lightning-points"]
    source = frame_path(root, domain, source_layer, valid_time)
    source_metadata_path = metadata_path(root, domain, source_layer, valid_time)
    if not source.is_file() or not source_metadata_path.is_file():
        raise FileNotFoundError(f"ECCC lightning source frame is incomplete at {format_utc(valid_time)}")
    destination = frame_path(root, domain, point_layer, valid_time)
    destination_metadata = metadata_path(root, domain, point_layer, valid_time)
    if _rendered_frame_ready(
        root,
        domain,
        point_layer,
        valid_time,
        LIGHTNING_POINT_RENDER_VERSION,
    ):
        existing = json.loads(destination_metadata.read_text())
        return {"status": "unchanged", "pointCount": existing.get("pointCount", 0)}

    source_metadata = json.loads(source_metadata_path.read_text())
    points = points_from_lightning_density_png(source, domain)
    window_start = valid_time - dt.timedelta(minutes=10)
    write_point_frame(
        destination,
        layer=point_layer.id,
        domain=domain,
        valid_time=valid_time,
        window_start=window_start,
        window_end=valid_time,
        age_reference_time=valid_time,
        point_schema=point_layer.point_schema,
        points=points,
        age_mode="window-midpoint-estimate",
        age_precision_seconds=600,
    )
    write_metadata(
        root,
        domain,
        point_layer,
        valid_time,
        destination,
        {"ECCC lightning density": valid_time},
        source=str(source_metadata.get("source") or point_layer.source),
        source_layer=str(
            source_metadata.get("sourceLayer")
            or source_layer.source_layer
            or "Lightning_2.5km_Density"
        ),
        extra={
            **point_frame_metadata(
                points=points,
                point_schema=point_layer.point_schema,
                window_start=window_start,
                window_end=valid_time,
                age_reference_time=valid_time,
                age_mode="window-midpoint-estimate",
                age_precision_seconds=600,
                render_version=LIGHTNING_POINT_RENDER_VERSION,
            ),
            "countMeaning": "connected positive 2.5-km density cells; not strokes",
            "densityWindowMinutes": 10,
        },
    )
    return {"status": "rendered", "pointCount": len(points)}


def ingest_geomet(
    client: GeoMetClient,
    root: Path,
    domain: Domain,
    hours: float,
    latest_only: bool,
    exclude_layers: set[str] | frozenset[str] | None = None,
    include_layers: Iterable[str] | None = None,
    *,
    preloaded_timelines: Mapping[str, LayerTimeline] | None = None,
    continue_on_error: bool = False,
    errors: list[str] | None = None,
) -> dict[str, list[dt.datetime]]:
    timelines: dict[str, list[dt.datetime]] = {}
    selected_layers = DEFAULT_SOURCE_LAYERS if include_layers is None else include_layers
    for layer_id in selected_layers:
        if exclude_layers and layer_id in exclude_layers:
            continue
        layer = LAYERS[layer_id]
        if layer.source_layer is None:
            continue
        try:
            if preloaded_timelines is None:
                timeline = client.timeline(layer.source_layer)
            else:
                timeline = preloaded_timelines[layer.source_layer]
        except Exception as error:
            if not continue_on_error and not layer.daylight_only and layer.role != "background":
                raise
            if errors is not None and layer.role != "background":
                errors.append(
                    f"{domain.id}/{layer_id} timeline: {type(error).__name__}: {error}"
                )
            continue
        # Radar, ptype, and lightning are usually newer than the slower
        # satellite anchor.  Fetch an extra hour of those layers so the oldest
        # satellite frame in a requested loop still has an honest at-or-before
        # match instead of starting with avoidable partial frames.
        matching_margin = 1.0 if layer_id in {
            "radar-rain",
            "radar-snow",
            "radar-coverage",
            "ptype",
            "ptype-coverage",
            "lightning",
        } else 0.0
        times = retained_times(
            timeline.times,
            hours + matching_margin,
            latest_only,
            dt.datetime.now(UTC),
            domain.tier,
        )
        timelines[layer_id] = list(timeline.times)
        for valid_time in times:
            destination = frame_path(root, domain, layer, valid_time)
            meta = metadata_path(root, domain, layer, valid_time)
            expected_render_version = geomet_render_version(layer_id)
            if destination.exists() and meta.exists():
                try:
                    current_render_version = json.loads(meta.read_text()).get("renderVersion")
                    if (
                        expected_render_version is None
                        or current_render_version == expected_render_version
                    ):
                        if layer_id == "lightning":
                            derive_eccc_lightning_points(root, domain, valid_time)
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            try:
                render_domain = (
                    precipitation_render_domain(domain)
                    if layer_id in HIGH_RESOLUTION_PRECIP_LAYERS
                    else domain
                )
                request_domain = render_domain
                if domain.id == "north-pacific":
                    request_domain = Domain(
                        id="north-pacific-radar-source",
                        title="North Pacific radar source",
                        west=-165.0,
                        south=5.0,
                        east=-50.0,
                        north=75.0,
                        crs="EPSG:3857",
                        width=2000 if layer_id in HIGH_RESOLUTION_PRECIP_LAYERS else 1000,
                        height=1800 if layer_id in HIGH_RESOLUTION_PRECIP_LAYERS else 900,
                        tier="broad",
                    )
                content = client.get_map(layer, request_domain, valid_time)
                if request_domain is not render_domain:
                    content = reproject_overlay(
                        content,
                        request_domain,
                        render_domain,
                        outside_no_coverage=layer_id.endswith("coverage"),
                    )
            except Exception as error:
                # A blank or temporarily unavailable qualitative satellite
                # frame must not abort radar, fire, and hazard ingest for every
                # later domain in the operational cycle.
                if layer.daylight_only or layer.role == "background" or continue_on_error:
                    if errors is not None and layer.role != "background":
                        errors.append(
                            f"{domain.id}/{layer_id} {format_utc(valid_time)}: "
                            f"{type(error).__name__}: {error}"
                        )
                    continue
                raise
            if layer.role == "background":
                save_satellite(content, destination)
            elif layer_id.endswith("coverage"):
                save_coverage(content, destination)
            else:
                save_overlay(content, destination)
            write_metadata(
                root,
                domain,
                layer,
                valid_time,
                destination,
                extra=(
                    {
                        "renderVersion": expected_render_version,
                        "outputWidth": render_domain.width,
                        "outputHeight": render_domain.height,
                    }
                    if expected_render_version is not None
                    else None
                ),
            )
            if layer_id == "lightning":
                derive_eccc_lightning_points(root, domain, valid_time)
    return timelines


def derive_lightning_trails(root: Path, domain: Domain, timelines: dict[str, list[dt.datetime]], hours: float) -> None:
    def local_times(layer_id: str) -> list[dt.datetime]:
        directory = root / "metadata" / domain.id / layer_id
        values: list[dt.datetime] = []
        if not directory.exists():
            return values
        for path in directory.rglob("*.json"):
            try:
                payload = json.loads(path.read_text())
                values.append(dt.datetime.fromisoformat(payload["validTime"].replace("Z", "+00:00")))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return sorted(set(values))

    lightning_times = local_times("lightning")
    radar_times = local_times("radar-rain")
    if not lightning_times:
        return
    newest_lightning = max(lightning_times)
    archive_cutoff = newest_lightning - dt.timedelta(hours=hours)
    trail_cutoff = newest_lightning - dt.timedelta(
        hours=min(hours, LIGHTNING_TRAIL_HOURS)
    )
    trail_start = trail_cutoff.replace(
        minute=trail_cutoff.minute - trail_cutoff.minute % 10,
        second=0,
        microsecond=0,
    )
    newest_observation = max(
        lightning_times[-1],
        radar_times[-1] if radar_times else lightning_times[-1],
    )
    anchor_end = newest_observation.replace(
        minute=newest_observation.minute - newest_observation.minute % 10,
        second=0,
        microsecond=0,
    )
    trail_anchors: set[dt.datetime] = set()
    anchor_cursor = trail_start
    while anchor_cursor <= anchor_end:
        if anchor_cursor >= trail_cutoff:
            trail_anchors.add(anchor_cursor)
        anchor_cursor += dt.timedelta(minutes=10)
    hour_anchors: set[dt.datetime] = set()
    hour_cursor = archive_cutoff.replace(minute=0, second=0, microsecond=0)
    while hour_cursor <= anchor_end:
        if hour_cursor >= archive_cutoff:
            hour_anchors.add(hour_cursor)
        hour_cursor += dt.timedelta(hours=1)
    anchors = sorted(trail_anchors | hour_anchors)
    output_layer = LAYERS["lightning-trail"]
    hour_layer = LAYERS["lightning-hour"]
    flash_layer = LAYERS["lightning-flash"]
    regional_trail_layers = (
        {
            region_id: LAYERS[regional_layer_id("lightning-trail", region_id)]
            for region_id in VIEWPORTS
        }
        if domain.id == "bc"
        else {}
    )
    regional_flash_layers = (
        {
            region_id: LAYERS[regional_layer_id("lightning-flash", region_id)]
            for region_id in VIEWPORTS
        }
        if domain.id == "bc"
        else {}
    )
    regional_hour_layers = (
        {
            region_id: LAYERS[regional_layer_id("lightning-hour", region_id)]
            for region_id in VIEWPORTS
        }
        if domain.id == "bc"
        else {}
    )
    source_layer = LAYERS["lightning"]
    # The display clock is ten-minute satellite time, not six-minute radar
    # time. Canonical ten-minute trail anchors keep new bolts, their halo and
    # their 10/20-minute age states synchronized with every displayed frame.
    trail_anchor_stamps = {frame_stamp(value) for value in trail_anchors}
    hour_anchor_stamps = {frame_stamp(value) for value in hour_anchors}
    derived_layers = (
        output_layer,
        flash_layer,
        *regional_trail_layers.values(),
        *regional_flash_layers.values(),
    )
    hour_layers = (hour_layer, *regional_hour_layers.values())
    for derived_layer, allowed_stamps in (
        *((layer, trail_anchor_stamps) for layer in derived_layers),
        *((layer, hour_anchor_stamps) for layer in hour_layers),
    ):
        output_metadata_root = root / "metadata" / domain.id / derived_layer.id
        if output_metadata_root.exists():
            for path in output_metadata_root.rglob("*.json"):
                if path.stem in allowed_stamps:
                    continue
                try:
                    payload = json.loads(path.read_text())
                    image_path = safe_archive_path(root, str(payload.get("path", "")))
                    if image_path.is_file():
                        image_path.unlink()
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                path.unlink(missing_ok=True)
        output_frame_root = root / "frames" / domain.id / derived_layer.id
        if output_frame_root.exists():
            for path in output_frame_root.rglob(f"*.{derived_layer.extension}"):
                if path.stem not in allowed_stamps:
                    path.unlink(missing_ok=True)

    def write_derived(
        layer: Layer,
        anchor: dt.datetime,
        existing: list[Path | None],
        source_times: list[dt.datetime | None],
        *,
        render_version: int,
        viewport: dict[str, float] | None = None,
        output_width: int | None = None,
        symbol_reference_width: int = 960,
        blur_glow: bool = True,
        arrival_only: bool = False,
        new_strike_halo: bool = True,
    ) -> None:
        destination = frame_path(root, domain, layer, anchor)
        meta = metadata_path(root, domain, layer, anchor)
        expected_sources = {
            f"age{index * 10}": format_utc(value)
            for index, value in enumerate(source_times)
            if value
        }
        current_sources: dict[str, str] = {}
        current_render_version: int | None = None
        current_region: dict[str, float] | None = None
        current_arrival_only: bool | None = None
        current_new_strike_halo: bool | None = None
        if meta.exists():
            try:
                current_metadata = json.loads(meta.read_text())
                current_sources = current_metadata.get("sourceTimes", {})
                current_render_version = current_metadata.get("renderVersion")
                current_region = current_metadata.get("regionalViewport")
                current_arrival_only = current_metadata.get("arrivalOnly", False)
                current_new_strike_halo = current_metadata.get("newStrikeHalo", True)
            except (OSError, json.JSONDecodeError):
                pass
        if (
            destination.exists()
            and current_sources == expected_sources
            and current_render_version == render_version
            and current_region == viewport
            and current_arrival_only == arrival_only
            and current_new_strike_halo == new_strike_halo
        ):
            return
        lightning_trail(
            existing,
            destination,
            viewport=viewport,
            output_width=output_width,
            symbol_reference_width=symbol_reference_width,
            blur_glow=blur_glow,
            arrival_only=arrival_only,
            new_strike_halo=new_strike_halo,
        )
        write_metadata(
            root,
            domain,
            layer,
            anchor,
            destination,
            {
                f"age{index * 10}": value
                for index, value in enumerate(source_times)
                if value
            },
            extra={
                "renderVersion": render_version,
                "arrivalOnly": arrival_only,
                "newStrikeHalo": new_strike_halo,
                **(
                    {
                        "regionalViewport": viewport,
                        "outputWidth": output_width,
                        "symbolReferenceWidth": symbol_reference_width,
                        "blurGlow": blur_glow,
                    }
                    if viewport is not None or output_width is not None
                    else {}
                ),
            },
        )

    def selected_sources(
        anchor: dt.datetime,
        offsets: tuple[int, ...],
    ) -> tuple[list[dt.datetime | None], list[Path | None]]:
        source_times: list[dt.datetime | None] = []
        used: set[dt.datetime] = set()
        for offset in offsets:
            target = anchor - dt.timedelta(minutes=offset)
            selected = at_or_before(lightning_times, target)
            if selected is not None and target - selected > dt.timedelta(minutes=2):
                selected = None
            if selected in used:
                selected = None
            if selected is not None:
                used.add(selected)
            source_times.append(selected)
        paths = [frame_path(root, domain, source_layer, value) if value else None for value in source_times]
        existing = [path if path and path.exists() else None for path in paths]
        return source_times, existing

    for anchor in anchors:
        base_output_width = BC_LIGHTNING_WIDTH if domain.id == "bc" else domain.width * 2
        base_symbol_reference_width = (
            domain.width if domain.id == "bc" else round(domain.width * 1.5)
        )
        base_blur_glow = domain.tier != "broad"
        if anchor in trail_anchors:
            source_times, existing = selected_sources(anchor, (0, 10, 20))
            if any(existing):
                fresh_arrival = (
                    existing[0] is not None
                    and source_times[0] is not None
                    and dt.timedelta(0) <= anchor - source_times[0] < dt.timedelta(minutes=5)
                )
                write_derived(
                    output_layer,
                    anchor,
                    existing,
                    source_times,
                    render_version=LIGHTNING_TRAIL_RENDER_VERSION,
                    output_width=base_output_width,
                    symbol_reference_width=base_symbol_reference_width,
                    blur_glow=base_blur_glow,
                    new_strike_halo=fresh_arrival,
                )
                for region_id in regional_trail_layers:
                    viewport = VIEWPORTS[region_id]
                    detailed_region = region_id != "small"
                    regional_symbol_reference_width = (
                        DETAILED_REGIONAL_SYMBOL_REFERENCE_WIDTH
                        if detailed_region
                        else BC_SMALL_LIGHTNING_SYMBOL_REFERENCE_WIDTH
                    )
                    write_derived(
                        regional_trail_layers[region_id],
                        anchor,
                        existing,
                        source_times,
                        render_version=LIGHTNING_REGIONAL_RENDER_VERSION,
                        viewport=viewport,
                        output_width=BC_LIGHTNING_WIDTH,
                        symbol_reference_width=regional_symbol_reference_width,
                        blur_glow=not detailed_region,
                        new_strike_halo=fresh_arrival,
                    )
            else:
                for layer in (output_layer, *regional_trail_layers.values()):
                    frame_path(root, domain, layer, anchor).unlink(missing_ok=True)
                    metadata_path(root, domain, layer, anchor).unlink(missing_ok=True)
            # The diffuse new-strike halo is composited into the same raster as
            # its bolt, eliminating separate-frame timing and registration drift.
            for layer in (flash_layer, *regional_flash_layers.values()):
                frame_path(root, domain, layer, anchor).unlink(missing_ok=True)
                metadata_path(root, domain, layer, anchor).unlink(missing_ok=True)
        if anchor in hour_anchors:
            hour_source_times, hour_existing = selected_sources(
                anchor,
                (0, 10, 20, 30, 40, 50),
            )
            if any(hour_existing):
                write_derived(
                    hour_layer,
                    anchor,
                    hour_existing,
                    hour_source_times,
                    render_version=LIGHTNING_HOUR_RENDER_VERSION,
                    output_width=base_output_width,
                    symbol_reference_width=base_symbol_reference_width,
                    blur_glow=base_blur_glow,
                    new_strike_halo=False,
                )
                for region_id, regional_hour_layer in regional_hour_layers.items():
                    viewport = VIEWPORTS[region_id]
                    detailed_region = region_id != "small"
                    write_derived(
                        regional_hour_layer,
                        anchor,
                        hour_existing,
                        hour_source_times,
                        render_version=LIGHTNING_REGIONAL_HOUR_RENDER_VERSION,
                        viewport=viewport,
                        output_width=BC_LIGHTNING_WIDTH,
                        symbol_reference_width=(
                            DETAILED_REGIONAL_SYMBOL_REFERENCE_WIDTH
                            if detailed_region
                            else BC_SMALL_LIGHTNING_SYMBOL_REFERENCE_WIDTH
                        ),
                        blur_glow=not detailed_region,
                        new_strike_halo=False,
                    )
def _rendered_frame_ready(
    root: Path,
    domain: Domain,
    layer: Layer,
    valid_time: dt.datetime,
    render_version: int,
) -> bool:
    image = frame_path(root, domain, layer, valid_time)
    metadata = metadata_path(root, domain, layer, valid_time)
    if not image.is_file() or not metadata.is_file():
        return False
    try:
        return json.loads(metadata.read_text()).get("renderVersion") == render_version
    except (OSError, json.JSONDecodeError):
        return False


def _archived_layer_times(root: Path, domain: Domain, layer: Layer) -> list[dt.datetime]:
    metadata_root = root / "metadata" / domain.id / layer.id
    values: list[dt.datetime] = []
    if not metadata_root.exists():
        return values
    for path in metadata_root.rglob("*.json"):
        try:
            payload = json.loads(path.read_text())
            values.append(dt.datetime.fromisoformat(payload["validTime"].replace("Z", "+00:00")))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
    return sorted(set(values))


def derive_fire_overlays(
    root: Path,
    domain: Domain,
    hours: float = FIRE_ARCHIVE_HOURS,
) -> dict[str, object]:
    """Combine agency fires and thermal hotspots into lightweight transparent PNGs."""
    hotspot_layer = LAYERS["hotspot-points"]
    active_layer = LAYERS["active-fire-points"]
    output_layer = LAYERS["hotspots"]
    hotspot_times = _archived_layer_times(root, domain, hotspot_layer)
    active_times = _archived_layer_times(root, domain, active_layer)
    if not hotspot_times:
        return {"status": "unavailable", "rendered": 0}
    cutoff = max(hotspot_times) - dt.timedelta(hours=hours)
    anchors = [value for value in hotspot_times if value >= cutoff]
    rendered = 0
    if domain.id == "bc":
        retained_stamps = {frame_stamp(value) for value in anchors}
        for region_id in VIEWPORTS:
            regional_layer = LAYERS[regional_layer_id("hotspots", region_id)]
            regional_metadata_root = root / "metadata" / domain.id / regional_layer.id
            if regional_metadata_root.exists():
                for path in regional_metadata_root.rglob("*.json"):
                    if path.stem in retained_stamps:
                        continue
                    try:
                        archived = json.loads(path.read_text())
                        image_path = safe_archive_path(root, str(archived.get("path", "")))
                        image_path.unlink(missing_ok=True)
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
                    path.unlink(missing_ok=True)

    def payload(layer: Layer, valid_time: dt.datetime) -> dict[str, object] | None:
        path = frame_path(root, domain, layer, valid_time)
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def metadata(layer: Layer, valid_time: dt.datetime) -> dict[str, object]:
        path = metadata_path(root, domain, layer, valid_time)
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def active_time_for(anchor: dt.datetime) -> dt.datetime | None:
        candidates = [
            value
            for value in active_times
            if value <= anchor and anchor - value <= dt.timedelta(hours=6)
        ]
        if not candidates:
            return None
        selected = candidates[-1]
        selected_metadata = metadata(active_layer, selected)
        if not selected_metadata.get("sourceErrors"):
            return selected
        for candidate in reversed(candidates[:-1]):
            candidate_metadata = metadata(active_layer, candidate)
            if (
                not candidate_metadata.get("sourceErrors")
                and int(candidate_metadata.get("usFeatureCount", 0)) > 0
            ):
                return candidate
        return selected

    for anchor in anchors:
        hotspot_payload = payload(hotspot_layer, anchor)
        if hotspot_payload is None:
            continue
        selected_active_time = active_time_for(anchor)
        active_payload = (
            payload(active_layer, selected_active_time)
            if selected_active_time is not None
            else None
        )
        expected_active_time = (
            format_utc(selected_active_time)
            if selected_active_time is not None
            else None
        )
        active_metadata = (
            metadata(active_layer, selected_active_time)
            if selected_active_time is not None
            else {}
        )
        source_times = {"hotspots": anchor}
        if selected_active_time is not None:
            source_times["active fires"] = selected_active_time
        standard_fire_version = (
            FIRE_BROAD_OVERLAY_RENDER_VERSION
            if domain.tier == "broad"
            else FIRE_OVERLAY_RENDER_VERSION
        )
        outputs: list[tuple[Layer, dict[str, float] | None, int, str | None]] = [
            (output_layer, None, standard_fire_version, None)
        ]
        if domain.id == "bc":
            outputs.extend(
                (
                    LAYERS[regional_layer_id("hotspots", region_id)],
                    viewport,
                    FIRE_REGIONAL_RENDER_VERSION,
                    region_id,
                )
                for region_id, viewport in VIEWPORTS.items()
            )
        rendered_anchor = False
        for layer, viewport, render_version, region_id in outputs:
            detailed_region = region_id is not None and region_id != "small"
            broad_view = viewport is None and domain.tier == "broad"
            bc_full_view = viewport is None and domain.id == "bc"
            regional_output_width = (
                DETAILED_REGIONAL_HAZARD_WIDTH
                if detailed_region
                else REGIONAL_HAZARD_WIDTH
            )
            regional_symbol_reference_width = (
                DETAILED_REGIONAL_SYMBOL_REFERENCE_WIDTH
                if detailed_region
                else 1120
            )
            render_output_width = (
                domain.width * BROAD_HAZARD_SCALE
                if broad_view
                else domain.width * 2 if bc_full_view
                else regional_output_width if viewport is not None else None
            )
            render_symbol_reference_width = (
                BROAD_FIRE_SYMBOL_REFERENCE_WIDTH
                if broad_view
                else 1280 if bc_full_view
                else regional_symbol_reference_width
            )
            render_symbol_size_scale = (
                0.85 if bc_full_view or region_id == "small" else 1.0
            )
            render_blur_glow = not (detailed_region or broad_view)
            destination = frame_path(root, domain, layer, anchor)
            existing_metadata = metadata(layer, anchor)
            version_key = (
                "fireOverlayRegionalRenderVersion"
                if viewport is not None
                else "fireOverlayRenderVersion"
            )
            if (
                destination.is_file()
                and existing_metadata.get(version_key) == render_version
                and existing_metadata.get("activeFireValidTime") == expected_active_time
                and existing_metadata.get("regionalViewport") == viewport
            ):
                continue
            summary = render_fire_overlay(
                hotspot_payload.get("points", []),
                active_payload.get("points", []) if active_payload else [],
                domain,
                destination,
                viewport=viewport,
                output_width=render_output_width,
                symbol_reference_width=render_symbol_reference_width,
                notable_size_scale=(
                    BC_SMALL_NOTABLE_FIRE_SCALE
                    if region_id == "small"
                    else 1.0
                ),
                symbol_size_scale=render_symbol_size_scale,
                supersample=1,
                blur_glow=render_blur_glow,
            )
            extra = {
                key: value
                for key, value in existing_metadata.items()
                if key not in {
                    "validTime",
                    "path",
                    "source",
                    "sourceLayer",
                    "fetchedAt",
                    "sourceTimes",
                }
            }
            extra.update({
                **summary,
                "renderVersion": HOTSPOT_RENDER_VERSION,
                version_key: render_version,
                "activeFireValidTime": expected_active_time,
                "activeFirePointCount": int(active_metadata.get("pointCount", 0)),
                "canadianFeatureCount": int(active_metadata.get("canadianFeatureCount", 0)),
                "bcwsFeatureCount": int(active_metadata.get("bcwsFeatureCount", 0)),
                "usFeatureCount": int(active_metadata.get("usFeatureCount", 0)),
                "sourceErrors": active_metadata.get("sourceErrors", []),
                "regionalViewport": viewport,
                **(
                    {
                        "outputWidth": render_output_width,
                        "symbolReferenceWidth": render_symbol_reference_width,
                        "notableSizeScale": (
                            BC_SMALL_NOTABLE_FIRE_SCALE
                            if region_id == "small"
                            else 1.0
                        ),
                        "symbolSizeScale": render_symbol_size_scale,
                        "supersample": 1,
                        "blurGlow": render_blur_glow,
                    }
                    if viewport is not None or broad_view or bc_full_view
                    else {}
                ),
            })
            write_metadata(
                root,
                domain,
                layer,
                anchor,
                destination,
                source_times,
                source="NRCan CWFIS + BCWS + NIFC WFIGS",
                source_layer=(
                    f"{CWFIS_HOTSPOT_LAYER} + agency-reported active-fire point frames"
                ),
                extra=extra,
            )
            rendered_anchor = True
        if rendered_anchor:
            rendered += 1
    return {
        "status": "rendered" if rendered else "unchanged",
        "rendered": rendered,
        "latestValidTime": format_utc(anchors[-1]),
    }


def derive_glm_lightning_trails(root: Path, domain: Domain, hours: float = 24.0) -> None:
    """Turn exact ten-minute GLM bins into the common fading bolt symbols."""
    source_layer = LAYERS["glm-lightning"]
    output_layer = LAYERS["glm-lightning-trail"]
    hour_layer = LAYERS["glm-lightning-hour"]
    flash_layer = LAYERS["glm-lightning-flash"]
    source_times = _archived_layer_times(root, domain, source_layer)
    if not source_times:
        return
    trail_cutoff = max(source_times) - dt.timedelta(
        hours=min(hours, LIGHTNING_TRAIL_HOURS)
    )
    hour_cutoff = max(source_times) - dt.timedelta(hours=hours)
    anchors = [value for value in source_times if value >= min(trail_cutoff, hour_cutoff)]
    source_set = set(source_times)
    broad_view = domain.tier == "broad"
    render_output_width = domain.width * BROAD_HAZARD_SCALE if broad_view else None
    render_symbol_reference_width = round(domain.width * 1.5) if broad_view else 960
    render_blur_glow = not broad_view
    for anchor in anchors:
        selected = [
            value if value in source_set else None
            for value in (anchor, anchor - dt.timedelta(minutes=10), anchor - dt.timedelta(minutes=20))
        ]
        paths = [
            frame_path(root, domain, source_layer, value) if value is not None else None
            for value in selected
        ]
        existing = [path if path is not None and path.is_file() else None for path in paths]
        if not any(existing):
            continue
        if anchor >= trail_cutoff:
            destination = frame_path(root, domain, output_layer, anchor)
            metadata = metadata_path(root, domain, output_layer, anchor)
            expected_sources = {
                f"age{index * 10}": format_utc(value)
                for index, value in enumerate(selected)
                if value is not None
            }
            current_sources: dict[str, str] = {}
            current_version: int | None = None
            if metadata.is_file():
                try:
                    payload = json.loads(metadata.read_text())
                    current_sources = payload.get("sourceTimes", {})
                    current_version = payload.get("renderVersion")
                except (OSError, json.JSONDecodeError):
                    pass
            if not (
                destination.is_file()
                and current_sources == expected_sources
                and current_version == GLM_LIGHTNING_TRAIL_RENDER_VERSION
            ):
                lightning_trail(
                    existing,
                    destination,
                    output_width=render_output_width,
                    symbol_reference_width=render_symbol_reference_width,
                    blur_glow=render_blur_glow,
                )
                write_metadata(
                    root,
                    domain,
                    output_layer,
                    anchor,
                    destination,
                    {
                        f"age{index * 10}": value
                        for index, value in enumerate(selected)
                        if value is not None
                    },
                    source="NOAA GOES-18",
                    source_layer="GLM-L2-LCFA 30-minute age trail",
                    extra={
                        "renderVersion": GLM_LIGHTNING_TRAIL_RENDER_VERSION,
                        **(
                            {
                                "outputWidth": render_output_width,
                                "symbolReferenceWidth": render_symbol_reference_width,
                                "blurGlow": render_blur_glow,
                            }
                            if broad_view
                            else {}
                        ),
                    },
                )
        if anchor.minute == 0 and anchor >= hour_cutoff:
            hour_selected = [
                value if value in source_set else None
                for value in (
                    anchor,
                    anchor - dt.timedelta(minutes=10),
                    anchor - dt.timedelta(minutes=20),
                    anchor - dt.timedelta(minutes=30),
                    anchor - dt.timedelta(minutes=40),
                    anchor - dt.timedelta(minutes=50),
                )
            ]
            hour_paths = [
                frame_path(root, domain, source_layer, value) if value is not None else None
                for value in hour_selected
            ]
            hour_existing = [
                path if path is not None and path.is_file() else None
                for path in hour_paths
            ]
            if any(hour_existing):
                hour_destination = frame_path(root, domain, hour_layer, anchor)
                hour_metadata = metadata_path(root, domain, hour_layer, anchor)
                hour_sources = {
                    f"age{index * 10}": format_utc(value)
                    for index, value in enumerate(hour_selected)
                    if value is not None
                }
                current_hour_sources: dict[str, str] = {}
                current_hour_version: int | None = None
                if hour_metadata.is_file():
                    try:
                        hour_payload = json.loads(hour_metadata.read_text())
                        current_hour_sources = hour_payload.get("sourceTimes", {})
                        current_hour_version = hour_payload.get("renderVersion")
                    except (OSError, json.JSONDecodeError):
                        pass
                if not (
                    hour_destination.is_file()
                    and current_hour_sources == hour_sources
                    and current_hour_version == GLM_LIGHTNING_HOUR_RENDER_VERSION
                ):
                    lightning_trail(
                        hour_existing,
                        hour_destination,
                        output_width=render_output_width,
                        symbol_reference_width=render_symbol_reference_width,
                        blur_glow=render_blur_glow,
                        new_strike_halo=False,
                    )
                    write_metadata(
                        root,
                        domain,
                        hour_layer,
                        anchor,
                        hour_destination,
                        {
                            f"age{index * 10}": value
                            for index, value in enumerate(hour_selected)
                            if value is not None
                        },
                        source="NOAA GOES-18",
                        source_layer="GLM-L2-LCFA hourly aggregate",
                        extra={
                            "renderVersion": GLM_LIGHTNING_HOUR_RENDER_VERSION,
                            "aggregateWindowMinutes": 60,
                            **(
                                {
                                    "outputWidth": render_output_width,
                                    "symbolReferenceWidth": render_symbol_reference_width,
                                    "blurGlow": render_blur_glow,
                                }
                                if broad_view
                                else {}
                            ),
                        },
                    )
        frame_path(root, domain, flash_layer, anchor).unlink(missing_ok=True)
        metadata_path(root, domain, flash_layer, anchor).unlink(missing_ok=True)


def ingest_goes_smoke_archive(
    root: Path,
    domain_ids: Iterable[str],
    now: dt.datetime | None = None,
    *,
    client: object | None = None,
    lookback_hours: float = 24.0,
    max_scans: int = 150,
    max_download_bytes: int = 600_000_000,
    max_object_bytes: int | None = None,
) -> dict[str, object]:
    """Catch up bounded GOES-18 ADPF history, committing one scan at a time.

    Completed image/metadata pairs are the restart boundary. Discovery and
    rendering proceed oldest-to-newest, existing pairs at the current render
    version are skipped, and a failed scan or domain does not roll back older
    successes. A fully unavailable nighttime scan is still a valid archived
    frame: its transparent PNG and availability metadata distinguish it from a
    missing source scan.
    """
    from .goes_hazards import (
        ADP_ARCHIVE_HOURS,
        ADP_MAX_DISCOVERY_BYTES,
        ADP_MAX_SCANS,
        GoesHazardClient,
        decode_smoke_product,
        render_smoke_overlay,
        ten_minute_clock,
    )
    from .raw_satellite import PublicObject, clear_downloads

    selected = [DOMAINS[domain_id] for domain_id in domain_ids if domain_id in DOMAINS]
    if not selected:
        return {"status": "disabled", "domains": [], "warnings": []}
    if not 0 < lookback_hours <= ADP_ARCHIVE_HOURS:
        raise ValueError(f"Smoke lookback_hours must be in (0, {ADP_ARCHIVE_HOURS}]")
    if not 0 < max_scans <= ADP_MAX_SCANS:
        raise ValueError(f"Smoke max_scans must be in [1, {ADP_MAX_SCANS}]")
    if not 0 < max_download_bytes <= ADP_MAX_DISCOVERY_BYTES:
        raise ValueError(
            f"Smoke max_download_bytes must be in [1, {ADP_MAX_DISCOVERY_BYTES}]"
        )

    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    object_cap = (
        max_object_bytes
        if max_object_bytes is not None
        else int(os.environ.get("RADARSAT_GOES_HAZARD_MAX_BYTES", "100000000"))
    )
    if object_cap <= 0:
        raise ValueError("Smoke max_object_bytes must be positive")

    warnings: list[str] = []
    rendered_domains: set[str] = set()
    rendered_times: dict[str, list[str]] = {domain.id: [] for domain in selected}
    downloaded_bytes = 0
    skipped_frames = 0
    attempted_scans = 0
    owned_client = client is None
    hazard_client = client or GoesHazardClient()
    scans: list[PublicObject] = []
    try:
        try:
            discover_history = getattr(hazard_client, "adp_scans", None)
            if callable(discover_history):
                scans = list(
                    discover_history(
                        current,
                        lookback_hours=lookback_hours,
                        max_scans=max_scans,
                        max_total_bytes=max_download_bytes,
                    )
                )
            else:
                # Retain compatibility with small injected clients that expose
                # only the original latest-scan interface.
                scans = [hazard_client.latest_adp(current)]  # type: ignore[attr-defined]
            if not scans:
                warnings.append(
                    "GOES-18 ADP discovery returned no scans in the requested window"
                )
        except Exception as error:
            warnings.append(
                f"GOES-18 ADP discovery unavailable: {type(error).__name__}: {error}"
            )
            scans = []

        with tempfile.TemporaryDirectory(prefix="radarsat-goes-smoke-") as temporary:
            cache_root = Path(temporary)
            smoke_layer = LAYERS["smoke"]
            # adp_scans guarantees oldest-to-newest order. Sorting here also
            # keeps injected test clients honest without altering selection.
            for adp in sorted(scans, key=lambda item: (item.valid_time, item.key)):
                valid_time = ten_minute_clock(adp.valid_time)
                needed = [
                    domain
                    for domain in selected
                    if not _rendered_frame_ready(
                        root,
                        domain,
                        smoke_layer,
                        valid_time,
                        SMOKE_RENDER_VERSION,
                    )
                ]
                skipped_frames += len(selected) - len(needed)
                if not needed:
                    continue
                if downloaded_bytes + adp.size > max_download_bytes:
                    warnings.append(
                        "GOES-18 ADP catch-up stopped at the aggregate download cap: "
                        f"{downloaded_bytes + adp.size:,} > {max_download_bytes:,} bytes"
                    )
                    break

                attempted_scans += 1
                source_path: Path | None = None
                try:
                    source_path = hazard_client.download(  # type: ignore[attr-defined]
                        adp,
                        cache_root,
                        min(object_cap, max_download_bytes - downloaded_bytes),
                    )
                    downloaded_bytes += adp.size
                    product = decode_smoke_product(source_path)
                    # NetCDF input is no longer needed after decode. Delete it
                    # before any potentially slow per-domain rendering.
                    source_path.unlink(missing_ok=True)
                    source_path = None
                except Exception as error:
                    warnings.append(
                        "GOES-18 ADP scan ingest unavailable at "
                        f"{format_utc(valid_time)}: {type(error).__name__}: {error}"
                    )
                    continue
                finally:
                    if source_path is not None:
                        source_path.unlink(missing_ok=True)
                    clear_downloads(cache_root)

                for domain in needed:
                    destination = frame_path(root, domain, smoke_layer, valid_time)
                    try:
                        summary = render_smoke_overlay(product, domain, destination)
                        write_metadata(
                            root,
                            domain,
                            smoke_layer,
                            valid_time,
                            destination,
                            {"GOES-18 ADP": product.start_time},
                            source="NOAA GOES-18",
                            source_layer="ABI-L2-ADPF",
                            extra={
                                **summary,
                                "scanEnd": format_utc(product.end_time),
                                "sourceFile": Path(adp.key).name,
                                "renderVersion": SMOKE_RENDER_VERSION,
                            },
                        )
                    except Exception as error:
                        warnings.append(
                            "GOES-18 ADP frame render unavailable for "
                            f"{domain.id} at {format_utc(valid_time)}: "
                            f"{type(error).__name__}: {error}"
                        )
                        continue
                    rendered_domains.add(domain.id)
                    rendered_times[domain.id].append(format_utc(valid_time))
    finally:
        if owned_client:
            hazard_client.close()  # type: ignore[attr-defined]

    frames_rendered = sum(len(values) for values in rendered_times.values())
    return {
        "status": "warning" if warnings else "rendered" if frames_rendered else "unchanged",
        "domains": sorted(rendered_domains),
        "renderedTimes": rendered_times,
        "scansDiscovered": len(scans),
        "scansAttempted": attempted_scans,
        "framesRendered": frames_rendered,
        "framesSkipped": skipped_frames,
        "downloadBytes": downloaded_bytes,
        "downloadCapBytes": max_download_bytes,
        "scanCap": max_scans,
        "lookbackHours": lookback_hours,
        "warnings": warnings,
    }


def ingest_goes_hazards(
    root: Path,
    domain_ids: Iterable[str],
    now: dt.datetime | None = None,
    *,
    client: object | None = None,
) -> dict[str, object]:
    """Catch up ADPF smoke history and ingest one complete GLM window.

    NOAA NetCDF inputs exist only inside temporary directories and are removed
    immediately after their display data is decoded.
    """
    from .goes_hazards import (
        GoesHazardClient,
        combine_glm_flashes,
        read_glm_flashes,
        render_glm_bins,
    )
    from .raw_satellite import clear_downloads

    selected = [DOMAINS[domain_id] for domain_id in domain_ids if domain_id in DOMAINS]
    if not selected:
        return {"status": "disabled", "domains": []}
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    max_bytes = int(os.environ.get("RADARSAT_GOES_HAZARD_MAX_BYTES", "100000000"))
    warnings: list[str] = []
    rendered: dict[str, list[str]] = {
        "smoke": [],
        "glm-lightning": [],
        "glm-lightning-points": [],
    }
    downloaded_bytes = 0
    owned_client = client is None
    hazard_client = client or GoesHazardClient()
    smoke_status: dict[str, object] = {}
    glm_window = None
    try:
        try:
            smoke_status = ingest_goes_smoke_archive(
                root,
                [domain.id for domain in selected],
                current,
                client=hazard_client,
                max_object_bytes=max_bytes,
            )
            smoke_domains = smoke_status.get("domains", [])
            if isinstance(smoke_domains, list):
                rendered["smoke"] = [str(value) for value in smoke_domains]
            downloaded_bytes += int(smoke_status.get("downloadBytes", 0))
            smoke_warnings = smoke_status.get("warnings", [])
            if isinstance(smoke_warnings, list):
                warnings.extend(str(value) for value in smoke_warnings)
        except Exception as error:
            warnings.append(
                f"GOES-18 ADP catch-up unavailable: {type(error).__name__}: {error}"
            )
            smoke_status = {
                "status": "warning",
                "error": f"{type(error).__name__}: {error}",
            }
        try:
            glm_window = hazard_client.latest_complete_glm_window(current)  # type: ignore[attr-defined]
        except Exception as error:
            warnings.append(f"GOES-18 GLM discovery unavailable: {type(error).__name__}: {error}")

        with tempfile.TemporaryDirectory(prefix="radarsat-goes-hazards-") as temporary:
            cache_root = Path(temporary)
            if glm_window is not None:
                lightning_layer = LAYERS["glm-lightning"]
                point_layer = LAYERS["glm-lightning-points"]
                needed = [
                    domain
                    for domain in selected
                    if (
                        not _rendered_frame_ready(
                            root,
                            domain,
                            lightning_layer,
                            glm_window.start_time,
                            GLM_LIGHTNING_RENDER_VERSION,
                        )
                        or not _rendered_frame_ready(
                            root,
                            domain,
                            point_layer,
                            glm_window.end_time,
                            GLM_LIGHTNING_POINT_RENDER_VERSION,
                        )
                    )
                ]
                if needed:
                    source_path: Path | None = None
                    decoded = []
                    try:
                        for item in glm_window.objects:
                            source_path = hazard_client.download(item, cache_root, max_bytes)  # type: ignore[attr-defined]
                            downloaded_bytes += item.size
                            decoded.append(
                                read_glm_flashes(
                                    source_path,
                                    item.valid_time + dt.timedelta(seconds=10),
                                )
                            )
                            source_path.unlink(missing_ok=True)
                            source_path = None
                        flashes = combine_glm_flashes(decoded)
                        for domain in needed:
                            destination = frame_path(
                                root, domain, lightning_layer, glm_window.start_time
                            )
                            summary = render_glm_bins(flashes, domain, destination)
                            points, point_summary = glm_point_rows(
                                flashes.latitudes,
                                flashes.longitudes,
                                flashes.observation_epochs,
                                domain,
                                glm_window.end_time,
                            )
                            point_destination = frame_path(
                                root,
                                domain,
                                point_layer,
                                glm_window.end_time,
                            )
                            write_point_frame(
                                point_destination,
                                layer=point_layer.id,
                                domain=domain,
                                valid_time=glm_window.end_time,
                                window_start=glm_window.start_time,
                                window_end=glm_window.end_time,
                                age_reference_time=glm_window.end_time,
                                point_schema=point_layer.point_schema,
                                points=points,
                                age_mode=str(point_summary["ageMode"]),
                                age_precision_seconds=int(point_summary["agePrecisionSeconds"]),
                            )
                            write_metadata(
                                root,
                                domain,
                                lightning_layer,
                                glm_window.start_time,
                                destination,
                                {"GOES-18 GLM": glm_window.start_time},
                                source="NOAA GOES-18",
                                source_layer="GLM-L2-LCFA",
                                extra={
                                    **summary,
                                    "windowEnd": format_utc(glm_window.end_time),
                                    "sourceFileCount": len(glm_window.objects),
                                    "firstSourceFile": Path(glm_window.objects[0].key).name,
                                    "lastSourceFile": Path(glm_window.objects[-1].key).name,
                                    "renderVersion": GLM_LIGHTNING_RENDER_VERSION,
                                },
                            )
                            write_metadata(
                                root,
                                domain,
                                point_layer,
                                glm_window.end_time,
                                point_destination,
                                {"GOES-18 GLM": glm_window.start_time},
                                source="NOAA GOES-18",
                                source_layer="GLM-L2-LCFA",
                                extra={
                                    **point_frame_metadata(
                                        points=points,
                                        point_schema=point_layer.point_schema,
                                        window_start=glm_window.start_time,
                                        window_end=glm_window.end_time,
                                        age_reference_time=glm_window.end_time,
                                        age_mode=str(point_summary["ageMode"]),
                                        age_precision_seconds=int(
                                            point_summary["agePrecisionSeconds"]
                                        ),
                                        render_version=GLM_LIGHTNING_POINT_RENDER_VERSION,
                                    ),
                                    "observedFlashCount": flashes.observed_count,
                                    "qualityControlledFlashCount": flashes.good_count,
                                    "mappedFlashCount": point_summary["mappedFlashCount"],
                                    "maximumLatitude": point_summary["maximumLatitude"],
                                    "binSizeMetres": point_summary["binSizeMetres"],
                                    "sourceFileCount": len(glm_window.objects),
                                },
                            )
                            rendered["glm-lightning"].append(domain.id)
                            rendered["glm-lightning-points"].append(domain.id)
                    except Exception as error:
                        warnings.append(f"GOES-18 GLM ingest unavailable: {type(error).__name__}: {error}")
                    finally:
                        if source_path is not None:
                            source_path.unlink(missing_ok=True)
                        clear_downloads(cache_root)
    finally:
        if owned_client:
            hazard_client.close()  # type: ignore[attr-defined]

    legacy_trails: list[str] = []
    for domain in selected:
        if radarsat_product_uses_layer(domain.id, "glm-lightning-trail"):
            derive_glm_lightning_trails(root, domain, hours=LIGHTNING_ARCHIVE_HOURS)
            legacy_trails.append(domain.id)
    rendered_any = any(rendered.values())
    return {
        "status": "warning" if warnings else "rendered" if rendered_any else "unchanged",
        "domains": rendered,
        "legacyTrailDomains": legacy_trails,
        "downloadBytes": downloaded_bytes,
        "cacheCapBytes": max_bytes,
        "smokeCatchup": smoke_status,
        "warnings": warnings,
    }


def ingest_glm_live(
    root: Path,
    domain_ids: Iterable[str],
    now: dt.datetime | None = None,
    *,
    client: object | None = None,
) -> dict[str, object]:
    """Render a rolling one-minute GLM layer for the live edge only.

    This intentionally does not touch smoke or the ten-minute historical GLM
    archive.  It downloads only the newest three consecutive 20-second files,
    renders every requested domain from that shared decode, and lets the small
    live-edge publisher expose the result immediately.
    """
    from .goes_hazards import (
        GoesHazardClient,
        combine_glm_flashes,
        read_glm_flashes,
        render_glm_bins,
    )
    from .raw_satellite import clear_downloads

    selected = [DOMAINS[value] for value in domain_ids if value in DOMAINS]
    if not selected:
        return {"status": "disabled", "domains": []}
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    max_bytes = int(os.environ.get("RADARSAT_GLM_LIVE_MAX_BYTES", "12000000"))
    owned_client = client is None
    hazard_client = client or GoesHazardClient()
    rendered: list[str] = []
    downloaded_bytes = 0
    try:
        batch = hazard_client.latest_glm_batch(current, file_count=3)  # type: ignore[attr-defined]
        valid_time = batch.end_time.replace(second=0, microsecond=0)
        with tempfile.TemporaryDirectory(prefix="radarsat-glm-live-") as temporary:
            cache_root = Path(temporary)
            decoded = []
            source_path: Path | None = None
            try:
                for item in batch.objects:
                    source_path = hazard_client.download(item, cache_root, max_bytes)  # type: ignore[attr-defined]
                    downloaded_bytes += item.size
                    decoded.append(
                        read_glm_flashes(
                            source_path,
                            item.valid_time + dt.timedelta(seconds=10),
                        )
                    )
                    source_path.unlink(missing_ok=True)
                    source_path = None
                flashes = combine_glm_flashes(decoded)
                for domain in selected:
                    layer = LAYERS["glm-lightning-live"]
                    destination = frame_path(root, domain, layer, valid_time)
                    ready = _rendered_frame_ready(
                        root,
                        domain,
                        layer,
                        valid_time,
                        GLM_LIGHTNING_LIVE_RENDER_VERSION,
                    )
                    if domain.id == "bc":
                        ready = ready and all(
                            _rendered_frame_ready(
                                root,
                                domain,
                                LAYERS[regional_layer_id("glm-lightning-live", region_id)],
                                valid_time,
                                GLM_LIGHTNING_LIVE_RENDER_VERSION,
                            )
                            for region_id in VIEWPORTS
                        )
                    if ready:
                        rendered.append(domain.id)
                        continue
                    raw_markers = cache_root / f"{domain.id}-markers.png"
                    summary = render_glm_bins(flashes, domain, raw_markers)
                    lightning_trail(
                        [raw_markers],
                        destination,
                        output_width=(domain.width * BROAD_HAZARD_SCALE if domain.tier == "broad" else domain.width),
                        symbol_reference_width=(BROAD_FIRE_SYMBOL_REFERENCE_WIDTH if domain.tier == "broad" else 1440),
                        blur_glow=True,
                    )
                    write_metadata(
                        root,
                        domain,
                        layer,
                        valid_time,
                        destination,
                        {"GOES-18 GLM": batch.end_time},
                        source="NOAA GOES-18",
                        source_layer="GLM-L2-LCFA rolling one-minute batch",
                        extra={
                            **summary,
                            "windowStart": format_utc(batch.start_time),
                            "windowEnd": format_utc(batch.end_time),
                            "sourceFileCount": len(batch.objects),
                            "renderVersion": GLM_LIGHTNING_LIVE_RENDER_VERSION,
                        },
                    )
                    if domain.id == "bc":
                        for region_id, viewport in VIEWPORTS.items():
                            regional_layer = LAYERS[
                                regional_layer_id("glm-lightning-live", region_id)
                            ]
                            regional_destination = frame_path(
                                root, domain, regional_layer, valid_time
                            )
                            lightning_trail(
                                [raw_markers],
                                regional_destination,
                                viewport=viewport,
                                output_width=1920,
                                symbol_reference_width=DETAILED_REGIONAL_SYMBOL_REFERENCE_WIDTH,
                                blur_glow=True,
                            )
                            write_metadata(
                                root,
                                domain,
                                regional_layer,
                                valid_time,
                                regional_destination,
                                {"GOES-18 GLM": batch.end_time},
                                source="NOAA GOES-18",
                                source_layer="GLM-L2-LCFA rolling one-minute batch",
                                extra={
                                    **summary,
                                    "windowStart": format_utc(batch.start_time),
                                    "windowEnd": format_utc(batch.end_time),
                                    "sourceFileCount": len(batch.objects),
                                    "renderVersion": GLM_LIGHTNING_LIVE_RENDER_VERSION,
                                    "regionalViewport": viewport,
                                    "outputWidth": 1920,
                                },
                            )
                    rendered.append(domain.id)
            finally:
                if source_path is not None:
                    source_path.unlink(missing_ok=True)
                clear_downloads(cache_root)
    finally:
        if owned_client:
            hazard_client.close()  # type: ignore[attr-defined]
    return {
        "status": "rendered" if rendered else "unchanged",
        "domains": rendered,
        "validTime": format_utc(valid_time),
        "downloadBytes": downloaded_bytes,
    }


def _raw_products_ready(root: Path, domain: Domain, valid_time: dt.datetime) -> bool:
    for layer_id in ("raw-ir",):
        layer = LAYERS[layer_id]
        image = frame_path(root, domain, layer, valid_time)
        metadata = metadata_path(root, domain, layer, valid_time)
        if not image.exists() or not metadata.exists():
            return False
        try:
            payload = json.loads(metadata.read_text())
            if payload.get("renderVersion") != RAW_SATELLITE_RENDER_VERSION:
                return False
            if domain.id == "north-pacific" and "Himawari-9" not in str(payload.get("source", "")):
                return False
        except (OSError, json.JSONDecodeError):
            return False
    visir = LAYERS["raw-visir"]
    visir_image = frame_path(root, domain, visir, valid_time)
    visir_metadata = metadata_path(root, domain, visir, valid_time)
    if not visir_image.is_file() or not visir_metadata.is_file():
        return False
    try:
        if json.loads(visir_metadata.read_text()).get("renderVersion") != RAW_VISIR_RENDER_VERSION:
            return False
    except (OSError, json.JSONDecodeError):
        return False
    return True


def _write_raw_metadata(
    root: Path,
    domain: Domain,
    valid_time: dt.datetime,
    source: str,
    source_layer: str,
    source_times: dict[str, dt.datetime],
    visir_details: dict[str, object],
) -> None:
    _write_raw_ir_metadata(
        root,
        domain,
        valid_time,
        source,
        source_layer,
        source_times,
    )
    visir = LAYERS["raw-visir"]
    write_metadata(
        root,
        domain,
        visir,
        valid_time,
        frame_path(root, domain, visir, valid_time),
        source_times,
        source=source,
        source_layer=f"{source_layer} solar visible/IR blend",
        extra={**visir_details, "renderVersion": RAW_VISIR_RENDER_VERSION},
    )


def _write_raw_ir_metadata(
    root: Path,
    domain: Domain,
    valid_time: dt.datetime,
    source: str,
    source_layer: str,
    source_times: dict[str, dt.datetime],
) -> None:
    for layer_id in ("raw-ir",):
        layer = LAYERS[layer_id]
        write_metadata(
            root,
            domain,
            layer,
            valid_time,
            frame_path(root, domain, layer, valid_time),
            source_times,
            source=source,
            source_layer=source_layer,
            extra={"renderVersion": RAW_SATELLITE_RENDER_VERSION},
        )


def _preferred_pacific_visir_exists(
    root: Path,
    domain: Domain,
    valid_time: dt.datetime,
) -> bool:
    layer = LAYERS["raw-visir"]
    image = frame_path(root, domain, layer, valid_time)
    metadata = metadata_path(root, domain, layer, valid_time)
    if not image.is_file() or not metadata.is_file():
        return False
    try:
        payload = json.loads(metadata.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("source") == "NOAA/NESDIS/STAR"
        and payload.get("renderVersion") == RAW_VISIR_RENDER_VERSION
    )


def _parse_source_times(payload: dict[str, object], fallback: dt.datetime) -> dict[str, dt.datetime]:
    parsed: dict[str, dt.datetime] = {}
    values = payload.get("sourceTimes")
    if isinstance(values, dict):
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            try:
                parsed[key] = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    return parsed or {"archived visible/IR": fallback}


def derive_raw_visir_archive(
    root: Path,
    domain_ids: Iterable[str],
    *,
    valid_times: set[dt.datetime] | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    """Derive ``raw-visir`` from existing raw frame pairs without downloading.

    Existing ``raw-visible`` and ``raw-ir`` frames and metadata remain byte-for-
    byte untouched. The archived false-colour IR is inverted through its known
    palette into an approximate neutral temperature image before solar blending.
    """
    from .raw_satellite import compose_visible_infrared, neutralize_archived_infrared

    requested = {
        value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        for value in valid_times or set()
    }
    rendered: dict[str, int] = {}
    skipped: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="radarsat-raw-visir-") as temporary:
        temporary_root = Path(temporary)
        for domain_id in domain_ids:
            domain = DOMAINS.get(domain_id)
            if domain is None:
                continue
            visible_metadata_root = root / "metadata" / domain.id / "raw-visible"
            infrared_metadata_root = root / "metadata" / domain.id / "raw-ir"
            if not visible_metadata_root.exists() or not infrared_metadata_root.exists():
                continue
            infrared_by_stamp = {path.stem: path for path in infrared_metadata_root.rglob("*.json")}
            rendered_count = 0
            skipped_count = 0
            for visible_metadata_path in sorted(visible_metadata_root.rglob("*.json")):
                infrared_metadata_path = infrared_by_stamp.get(visible_metadata_path.stem)
                if infrared_metadata_path is None:
                    continue
                try:
                    visible_payload = json.loads(visible_metadata_path.read_text())
                    infrared_payload = json.loads(infrared_metadata_path.read_text())
                    valid_time = dt.datetime.fromisoformat(
                        str(visible_payload["validTime"]).replace("Z", "+00:00")
                    ).astimezone(UTC)
                    if requested and valid_time not in requested:
                        continue
                    if str(infrared_payload.get("validTime")) != str(visible_payload.get("validTime")):
                        continue
                    visible_path = safe_archive_path(root, str(visible_payload["path"]))
                    infrared_path = safe_archive_path(root, str(infrared_payload["path"]))
                except (OSError, KeyError, ValueError, json.JSONDecodeError):
                    continue
                if not visible_path.is_file() or not infrared_path.is_file():
                    continue
                visir_layer = LAYERS["raw-visir"]
                destination = frame_path(root, domain, visir_layer, valid_time)
                destination_metadata = metadata_path(root, domain, visir_layer, valid_time)
                if not overwrite and destination.is_file() and destination_metadata.is_file():
                    try:
                        current = json.loads(destination_metadata.read_text())
                        if current.get("renderVersion") == RAW_VISIR_RENDER_VERSION:
                            skipped_count += 1
                            continue
                    except (OSError, json.JSONDecodeError):
                        pass
                neutral_path = temporary_root / f"{domain.id}-{frame_stamp(valid_time)}-neutral-ir.png"
                neutralize_archived_infrared(infrared_path, neutral_path)
                details = compose_visible_infrared(
                    visible_path,
                    neutral_path,
                    domain,
                    valid_time,
                    destination,
                )
                neutral_path.unlink(missing_ok=True)
                source_times = _parse_source_times(visible_payload, valid_time)
                write_metadata(
                    root,
                    domain,
                    visir_layer,
                    valid_time,
                    destination,
                    source_times,
                    source=str(visible_payload.get("source") or "NOAA Open Data"),
                    source_layer=f"{visible_payload.get('sourceLayer') or 'raw-visible/raw-ir'} archive blend",
                    extra={
                        **details,
                        "derivedFromArchivedFrames": True,
                        "renderVersion": RAW_VISIR_RENDER_VERSION,
                    },
                )
                rendered_count += 1
            if rendered_count:
                rendered[domain.id] = rendered_count
            if skipped_count:
                skipped[domain.id] = skipped_count
    return {
        "status": "rendered" if rendered else "unchanged",
        "rendered": rendered,
        "skipped": skipped,
    }


def ingest_raw_satellite(
    root: Path,
    domain_ids: Iterable[str],
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Ingest one bounded half-hourly calibrated legacy satellite frame.

    Large source files are downloaded one satellite at a time and removed in a
    ``finally`` block. Only compact WebP display rasters and Satpy's small
    auxiliary tables persist locally.
    """
    from .raw_satellite import (
        PublicSatelliteClient,
        blend_satellites,
        clear_downloads,
        compose_visible_infrared,
        install_render,
        normalized_frame_time,
        render_satpy_domain_isolated,
    )

    selected = [DOMAINS[domain_id] for domain_id in domain_ids if domain_id in DOMAINS]
    selected = [domain for domain in selected if domain.id in {"bc", "north-america", "north-pacific"}]
    rapid_bc_enabled = os.environ.get(
        "RADARSAT_WESTWX_SATELLITE_ENABLED", "0"
    ).lower() in {"1", "true", "yes"}
    if rapid_bc_enabled:
        # The dedicated GOES-18 path reuses each ten-minute download for BC.
        # Avoid a duplicate, normalized half-hour BC frame from this legacy path.
        selected = [domain for domain in selected if domain.id != "bc"]
    if not selected:
        return {"status": "disabled", "domains": []}
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    project_root = Path(__file__).resolve().parents[1]
    configured_cache = Path(os.environ.get("RADARSAT_RAW_SAT_CACHE_ROOT", "var/cache/raw-satellite")).expanduser()
    cache_root = configured_cache if configured_cache.is_absolute() else project_root / configured_cache
    max_bytes = int(os.environ.get("RADARSAT_RAW_SAT_MAX_BYTES", "900000000"))
    warnings: list[str] = []
    downloaded_bytes = 0
    rendered_domains: list[str] = []
    render_root = cache_root / "renders"
    clear_downloads(cache_root)
    shutil.rmtree(render_root, ignore_errors=True)

    with PublicSatelliteClient() as client:
        goes18 = client.latest_goes("G18", current)
        valid_time = normalized_frame_time(goes18.valid_time)
        needed = [domain for domain in selected if not _raw_products_ready(root, domain, valid_time)]
        if not needed:
            return {
                "status": "unchanged",
                "validTime": format_utc(valid_time),
                "sourceTimes": {"GOES-18": format_utc(goes18.valid_time)},
                "downloadBytes": 0,
                "cacheCapBytes": max_bytes,
                "domains": [domain.id for domain in selected],
                "warnings": [],
            }
        try:
            goes18_path = client.download(goes18, cache_root, max_bytes)
            downloaded_bytes += goes18.size
            rendered18 = {
                domain.id: render_satpy_domain_isolated(
                    [goes18_path], "abi_l2_nc", "C13", domain, cache_root, f"g18-{frame_stamp(valid_time)}"
                )
                for domain in needed
            }
            clear_downloads(cache_root)

            bc = next((domain for domain in needed if domain.id == "bc"), None)
            if bc is not None:
                gray_destination = render_root / f"combined-{bc.id}-{frame_stamp(valid_time)}-ir-gray.webp"
                visible_destination = render_root / f"combined-{bc.id}-{frame_stamp(valid_time)}-visible.webp"
                install_render(
                    rendered18[bc.id],
                    visible_destination,
                    frame_path(root, bc, LAYERS["raw-ir"], valid_time),
                    gray_destination,
                )
                visir_details = compose_visible_infrared(
                    visible_destination,
                    gray_destination,
                    bc,
                    valid_time,
                    frame_path(root, bc, LAYERS["raw-visir"], valid_time),
                )
                _write_raw_metadata(
                    root,
                    bc,
                    valid_time,
                    "NOAA GOES-18",
                    "ABI-L2-MCMIPF",
                    {"GOES-18": goes18.valid_time},
                    visir_details,
                )
                rendered_domains.append(bc.id)

            north_america = next((domain for domain in needed if domain.id == "north-america"), None)
            if north_america is not None:
                source = "NOAA GOES-18"
                source_times = {"GOES-18": goes18.valid_time}
                gray_destination = (
                    render_root / f"combined-{north_america.id}-{frame_stamp(valid_time)}-ir-gray.webp"
                )
                visible_destination = (
                    render_root / f"combined-{north_america.id}-{frame_stamp(valid_time)}-visible.webp"
                )
                try:
                    goes19 = client.latest_goes("G19", current)
                    goes19_path = client.download(goes19, cache_root, max_bytes)
                    downloaded_bytes += goes19.size
                    rendered19 = render_satpy_domain_isolated(
                        [goes19_path], "abi_l2_nc", "C13", north_america, cache_root, f"g19-{frame_stamp(valid_time)}"
                    )
                    blend_satellites(
                        rendered18[north_america.id],
                        rendered19,
                        north_america,
                        (-112.0, -96.0),
                        visible_destination,
                        frame_path(root, north_america, LAYERS["raw-ir"], valid_time),
                        gray_destination,
                    )
                    source = "NOAA GOES-18 + GOES-19"
                    source_times["GOES-19"] = goes19.valid_time
                except Exception as error:
                    warnings.append(f"GOES-19 blend unavailable; using GOES-18: {type(error).__name__}: {error}")
                    install_render(
                        rendered18[north_america.id],
                        visible_destination,
                        frame_path(root, north_america, LAYERS["raw-ir"], valid_time),
                        gray_destination,
                    )
                finally:
                    clear_downloads(cache_root)
                visir_details = compose_visible_infrared(
                    visible_destination,
                    gray_destination,
                    north_america,
                    valid_time,
                    frame_path(root, north_america, LAYERS["raw-visir"], valid_time),
                )
                _write_raw_metadata(
                    root,
                    north_america,
                    valid_time,
                    source,
                    "ABI-L2-MCMIPF",
                    source_times,
                    visir_details,
                )
                rendered_domains.append(north_america.id)

            north_pacific = next((domain for domain in needed if domain.id == "north-pacific"), None)
            if north_pacific is not None:
                source = "NOAA GOES-18"
                source_layer = "ABI-L2-MCMIPF"
                source_times = {"GOES-18": goes18.valid_time}
                gray_destination = (
                    render_root / f"combined-{north_pacific.id}-{frame_stamp(valid_time)}-ir-gray.webp"
                )
                visible_destination = (
                    render_root / f"combined-{north_pacific.id}-{frame_stamp(valid_time)}-visible.webp"
                )
                try:
                    himawari = client.latest_himawari(current)
                    himawari_paths = [client.download(item, cache_root, max_bytes) for item in himawari]
                    downloaded_bytes += sum(item.size for item in himawari)
                    rendered_himawari = render_satpy_domain_isolated(
                        himawari_paths,
                        "ahi_hsd",
                        "B13",
                        north_pacific,
                        cache_root,
                        f"h09-{frame_stamp(valid_time)}",
                    )
                    blend_satellites(
                        rendered_himawari,
                        rendered18[north_pacific.id],
                        north_pacific,
                        (185.0, 205.0),
                        visible_destination,
                        frame_path(root, north_pacific, LAYERS["raw-ir"], valid_time),
                        gray_destination,
                        unwrap_longitudes=True,
                    )
                    source = "NOAA Himawari-9 + GOES-18"
                    source_layer = "AHI-L1b-FLDK + ABI-L2-MCMIPF"
                    source_times["Himawari-9"] = himawari[0].valid_time
                except Exception as error:
                    warnings.append(f"Himawari-9 blend unavailable; using GOES-18: {type(error).__name__}: {error}")
                    install_render(
                        rendered18[north_pacific.id],
                        visible_destination,
                        frame_path(root, north_pacific, LAYERS["raw-ir"], valid_time),
                        gray_destination,
                    )
                finally:
                    clear_downloads(cache_root)
                if _preferred_pacific_visir_exists(root, north_pacific, valid_time):
                    from .noaa_star_geocolor import blend_pacific_geocolor

                    legacy_visir_destination = (
                        render_root
                        / f"combined-{north_pacific.id}-{frame_stamp(valid_time)}-legacy-visir.webp"
                    )
                    compose_visible_infrared(
                        visible_destination,
                        gray_destination,
                        north_pacific,
                        valid_time,
                        legacy_visir_destination,
                    )
                    preferred_destination = frame_path(
                        root,
                        north_pacific,
                        LAYERS["raw-visir"],
                        valid_time,
                    )
                    blend_pacific_geocolor(
                        preferred_destination,
                        legacy_visir_destination,
                        north_pacific,
                        preferred_destination,
                    )
                    preferred_metadata = metadata_path(
                        root,
                        north_pacific,
                        LAYERS["raw-visir"],
                        valid_time,
                    )
                    try:
                        payload = json.loads(preferred_metadata.read_text())
                        payload["sourceTimes"] = {
                            **dict(payload.get("sourceTimes") or {}),
                            **{
                                label: format_utc(value)
                                for label, value in source_times.items()
                            },
                        }
                        payload["westFallbackSource"] = source
                        payload["westFallbackValidTime"] = format_utc(valid_time)
                        payload["bytes"] = preferred_destination.stat().st_size
                        temporary = preferred_metadata.with_name(
                            f"{preferred_metadata.name}.{os.getpid()}.tmp"
                        )
                        temporary.write_text(json.dumps(payload, indent=2) + "\n")
                        temporary.replace(preferred_metadata)
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        pass
                    _write_raw_ir_metadata(
                        root,
                        north_pacific,
                        valid_time,
                        source,
                        source_layer,
                        source_times,
                    )
                else:
                    visir_details = compose_visible_infrared(
                        visible_destination,
                        gray_destination,
                        north_pacific,
                        valid_time,
                        frame_path(root, north_pacific, LAYERS["raw-visir"], valid_time),
                    )
                    _write_raw_metadata(
                        root,
                        north_pacific,
                        valid_time,
                        source,
                        source_layer,
                        source_times,
                        visir_details,
                    )
                rendered_domains.append(north_pacific.id)
        finally:
            clear_downloads(cache_root)
            shutil.rmtree(render_root, ignore_errors=True)

    return {
        "status": "warning" if warnings else "rendered",
        "validTime": format_utc(valid_time),
        "sourceTimes": {"GOES-18": format_utc(goes18.valid_time)},
        "downloadBytes": downloaded_bytes,
        "cacheCapBytes": max_bytes,
        "domains": rendered_domains,
        "warnings": warnings,
    }


def prune(root: Path, now: dt.datetime) -> int:
    removed = 0
    for domain in DOMAINS.values():
        metadata_root = root / "metadata" / domain.id
        if not metadata_root.exists():
            continue
        for meta_path in metadata_root.rglob("*.json"):
            try:
                payload = json.loads(meta_path.read_text())
                valid_time = dt.datetime.fromisoformat(payload["validTime"].replace("Z", "+00:00"))
                image_path = safe_archive_path(root, str(payload["path"]))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
            layer_id = meta_path.relative_to(metadata_root).parts[0]
            if layer_id in {"natural", "raw-visible", "westwx-visible"}:
                image_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                removed += 1
                continue
            if layer_id in {"raw-visir-native", "raw-visir-5min"} and now - valid_time > dt.timedelta(hours=24):
                image_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                removed += 1
                continue
            if layer_id.endswith("coverage") and payload.get("renderVersion") != COVERAGE_RENDER_VERSION:
                image_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                removed += 1
                continue
            if keep_layer_frame(valid_time, now, domain.tier, layer_id):
                continue
            image_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            removed += 1
    return removed


def run(
    output_root: Path,
    domain_ids: list[str],
    hours: float,
    latest_only: bool,
    spool_root: Path | None = None,
    spool_mode: str = "auto",
    spool_hours: float = 12.0,
) -> Path:
    if spool_mode not in {"auto", "off", "only"}:
        raise ValueError(f"Unsupported spool mode: {spool_mode!r}")
    if spool_hours <= 0:
        raise ValueError("spool_hours must be positive")
    from .spool import NATIVE_LAYER_IDS, SpoolIngestResult, ingest_spool

    spool_root = (spool_root or Path.home() / ".local/share/radar-sat/spool/eccc").expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    native_status: dict[str, object] = {}
    auxiliary_warnings: list[str] = []
    hotspot_status: dict[str, object] = {}
    active_fire_status: dict[str, object] = {}
    fire_overlay_status: dict[str, object] = {}
    hybrid_radar_status: dict[str, object] = {}
    raw_satellite_status: dict[str, object] = {}
    goes_hazard_status: dict[str, object] = {}

    # Fires are operationally useful even when an unrelated GeoMet radar
    # request fails. Refresh all requested fire domains before entering that
    # failure-prone network path, and let each source fail independently.
    for domain_id in domain_ids:
        if domain_id not in {"bc", "north-america", "north-pacific"}:
            continue
        domain = DOMAINS[domain_id]
        try:
            hotspot_status[domain.id] = ingest_hotspot_snapshot(output_root, domain)
        except Exception as error:
            auxiliary_warnings.append(
                f"CWFIS wildfire hotspots unavailable: {type(error).__name__}: {error}"
            )
        try:
            active_fire_status[domain.id] = ingest_active_fire_snapshot(output_root, domain)
            warnings = active_fire_status[domain.id].get("warnings", [])
            if isinstance(warnings, list):
                auxiliary_warnings.extend(str(value) for value in warnings)
        except Exception as error:
            auxiliary_warnings.append(
                "Agency-reported active fires unavailable: "
                f"{type(error).__name__}: {error}"
            )
        try:
            fire_overlay_status[domain.id] = derive_fire_overlays(output_root, domain)
        except Exception as error:
            auxiliary_warnings.append(
                "Wildfire display overlay unavailable: "
                f"{type(error).__name__}: {error}"
            )

    with GeoMetClient() as client:
        for domain_id in domain_ids:
            domain = DOMAINS[domain_id]
            try:
                ensure_static_assets(client, output_root, domain)
            except Exception as error:
                # Legends and base maps are immutable after first install; a
                # transient GeoMet failure must not block dynamic observations.
                auxiliary_warnings.append(
                    f"{domain.id} static assets unavailable: {type(error).__name__}: {error}"
                )
            native_result = SpoolIngestResult()
            # Native products are currently consumed only on the operational BC
            # grid. Broad domains retain the lower-rate GeoMet bootstrap path.
            if domain.id == "bc" and spool_mode != "off":
                native_result = ingest_spool(
                    spool_root,
                    output_root,
                    domain,
                    spool_hours,
                    latest_only,
                )
                native_status[domain.id] = native_result.status()
            elif domain.id != "bc" and spool_mode != "off":
                native_result = ingest_spool(
                    spool_root,
                    output_root,
                    domain,
                    max(hours, LIGHTNING_TRAIL_HOURS),
                    latest_only,
                    include_layers=("lightning",),
                )
                native_status[domain.id] = native_result.status()
            excluded = (
                set(NATIVE_LAYER_IDS)
                if domain.id == "bc" and spool_mode == "only"
                else set()
            )
            try:
                timelines = ingest_geomet(
                    client,
                    output_root,
                    domain,
                    hours,
                    latest_only,
                    exclude_layers=excluded,
                    include_layers=None
                    if domain.id == "bc"
                    else (
                        "radar-rain",
                        "radar-coverage",
                        "ptype",
                        "ptype-coverage",
                    ),
                    continue_on_error=True,
                    errors=auxiliary_warnings,
                )
            except Exception as error:
                # Keep unrelated domains and locally staged observations
                # moving even if an unexpected GeoMet decoder error escapes
                # the normal per-layer isolation above.
                timelines = {}
                auxiliary_warnings.append(
                    f"{domain.id} GeoMet ingest unavailable: {type(error).__name__}: {error}"
                )
            if (
                domain.id == "bc"
                and os.environ.get("RADARSAT_NEXRAD_HYBRID_ENABLED", "1").lower()
                not in {"0", "false", "no"}
            ):
                try:
                    from .nexrad_hybrid import derive_south_coast_hybrid_radar

                    hybrid_radar_status[domain.id] = derive_south_coast_hybrid_radar(
                        output_root,
                        hours=hours,
                        latest_only=latest_only,
                    )
                    warnings = hybrid_radar_status[domain.id].get("warnings", [])
                    if isinstance(warnings, list):
                        auxiliary_warnings.extend(str(value) for value in warnings)
                except Exception as error:
                    auxiliary_warnings.append(
                        "South Coast NOAA/ECCC hybrid radar unavailable: "
                        f"{type(error).__name__}: {error}"
                    )
            if domain.id != "bc":
                try:
                    timelines.update(ingest_geomet(
                        client,
                        output_root,
                        domain,
                        hours,
                        latest_only,
                        include_layers=("lightning",),
                        continue_on_error=True,
                        errors=auxiliary_warnings,
                    ))
                except Exception as error:
                    # CLDN supplements GLM over Canada, but must not prevent
                    # satellite/radar publication when GeoMet is unavailable.
                    auxiliary_warnings.append(
                        "ECCC broad-domain lightning unavailable: "
                        f"{type(error).__name__}: {error}"
                    )
            derive_lightning_trails(
                output_root,
                domain,
                timelines,
                LIGHTNING_ARCHIVE_HOURS,
            )
        if os.environ.get("RADARSAT_RAW_SAT_ENABLED", "1").lower() not in {"0", "false", "no"}:
            try:
                raw_satellite_status = ingest_raw_satellite(output_root, domain_ids)
                auxiliary_warnings.extend(str(value) for value in raw_satellite_status.get("warnings", []))
            except Exception as error:
                auxiliary_warnings.append(
                    f"Raw NOAA satellite ingest unavailable: {type(error).__name__}: {error}"
                )
                raw_satellite_status = {
                    "status": "warning",
                    "error": f"{type(error).__name__}: {error}",
                }
        if os.environ.get("RADARSAT_GOES_HAZARDS_ENABLED", "1").lower() not in {"0", "false", "no"}:
            try:
                goes_hazard_status = ingest_goes_hazards(output_root, domain_ids)
                auxiliary_warnings.extend(
                    str(value) for value in goes_hazard_status.get("warnings", [])
                )
            except Exception as error:
                auxiliary_warnings.append(
                    f"GOES-18 smoke/lightning ingest unavailable: {type(error).__name__}: {error}"
                )
                goes_hazard_status = {
                    "status": "warning",
                    "error": f"{type(error).__name__}: {error}",
                }
    prune(output_root, dt.datetime.now(UTC))
    catalog = write_catalog(output_root)
    has_native_rejections = any(
        bool(value.get("rejected"))
        for value in native_status.values()
        if isinstance(value, dict)
    )
    write_status(
        output_root / "status" / "ingest.json",
        {
            "status": "warning" if has_native_rejections or auxiliary_warnings else "ok",
            "updatedAt": format_utc(dt.datetime.now(UTC)),
            "catalog": catalog.relative_to(output_root).as_posix(),
            "domains": domain_ids,
            "spool": {
                "mode": spool_mode,
                "root": str(spool_root),
                "ingestHours": spool_hours,
                "domains": native_status,
            },
            "hotspots": hotspot_status,
            "activeFires": active_fire_status,
            "fireOverlays": fire_overlay_status,
            "hybridRadar": hybrid_radar_status,
            "rawSatellite": raw_satellite_status,
            "goesHazards": goes_hazard_status,
            "warnings": auxiliary_warnings,
        },
    )
    return catalog


def write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest and render Radar-Sat observational layers.")
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    parser.add_argument("--domain", action="append", choices=sorted(DOMAINS), default=[])
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument(
        "--spool-root",
        type=Path,
        default=Path.home() / ".local/share/radar-sat/spool/eccc",
        help="root containing completed satellite/, lightning/, and radar/ feed files",
    )
    parser.add_argument(
        "--spool-mode",
        choices=("auto", "off", "only"),
        default="auto",
        help=(
            "auto prefers native files and fills gaps from GeoMet; off ignores the spool; "
            "only disables GeoMet fallback for native-capable satellite/lightning layers"
        ),
    )
    parser.add_argument(
        "--spool-hours",
        type=float,
        default=12.0,
        help="native backlog window to render (independent of the shorter GeoMet window)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    domain_ids = args.domain or ["bc"]
    try:
        catalog = run(
            args.output_root,
            domain_ids,
            args.hours,
            args.latest_only,
            args.spool_root,
            args.spool_mode,
            args.spool_hours,
        )
    except Exception as error:
        write_status(
            args.output_root / "status" / "ingest.json",
            {
                "status": "error",
                "updatedAt": format_utc(dt.datetime.now(UTC)),
                "error": f"{type(error).__name__}: {error}",
                "domains": domain_ids,
                "spool": {
                    "mode": args.spool_mode,
                    "root": str(args.spool_root),
                    "ingestHours": args.spool_hours,
                },
            },
        )
        raise
    else:
        print(f"Radar-Sat catalog written to {catalog}", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
