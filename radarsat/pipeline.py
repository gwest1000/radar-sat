from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable

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
from .config import DOMAINS, LAYERS, Domain, Layer
from .geomet import GeoMetClient, at_or_before, format_utc, frame_stamp
from .hotspots import CWFIS_HOTSPOT_LAYER, fetch_hotspots, project_hotspots, render_hotspots
from .images import (
    lightning_trail,
    reproject_overlay,
    render_static_maps,
    render_watershed_overlay,
    save_coverage,
    save_overlay,
    save_satellite,
)
from .retention import keep_frame
from .point_frames import (
    glm_point_rows,
    normalized_pixel,
    point_frame_metadata,
    points_from_lightning_density_png,
    radarsat_product_uses_layer,
    write_point_frame,
)


UTC = dt.timezone.utc
LIGHTNING_TRAIL_RENDER_VERSION = 4
LIGHTNING_POINT_RENDER_VERSION = 1
HOTSPOT_RENDER_VERSION = 4
HOTSPOT_POINT_RENDER_VERSION = 2
ACTIVE_FIRE_POINT_RENDER_VERSION = 3
RAW_SATELLITE_RENDER_VERSION = 1
RAW_VISIR_RENDER_VERSION = 4
SMOKE_RENDER_VERSION = 2
GLM_LIGHTNING_RENDER_VERSION = 2
GLM_LIGHTNING_TRAIL_RENDER_VERSION = 4
GLM_LIGHTNING_POINT_RENDER_VERSION = 2
COVERAGE_RENDER_VERSION = 2
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
    temporary = destination.with_suffix(".json.tmp")
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
    base = root / "static" / domain.id / "base-dark.png"
    boundaries = root / "static" / domain.id / "boundaries.png"
    if not base.exists() or not boundaries.exists():
        render_static_maps(domain, base, boundaries)
    watersheds = root / "static" / domain.id / "bch-watersheds.png"
    if domain.id == "bc" and not watersheds.exists():
        render_watershed_overlay(domain, watersheds)

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
) -> dict[str, list[dt.datetime]]:
    timelines: dict[str, list[dt.datetime]] = {}
    for layer_id in include_layers or DEFAULT_SOURCE_LAYERS:
        if exclude_layers and layer_id in exclude_layers:
            continue
        layer = LAYERS[layer_id]
        if layer.source_layer is None:
            continue
        timeline = client.timeline(layer.source_layer)
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
            if destination.exists() and meta.exists():
                if not layer_id.endswith("coverage"):
                    if layer_id == "lightning":
                        derive_eccc_lightning_points(root, domain, valid_time)
                    continue
                try:
                    if json.loads(meta.read_text()).get("renderVersion") == COVERAGE_RENDER_VERSION:
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            try:
                request_domain = domain
                if domain.id == "north-pacific":
                    request_domain = Domain(
                        id="north-pacific-radar-source",
                        title="North Pacific radar source",
                        west=-165.0,
                        south=5.0,
                        east=-50.0,
                        north=75.0,
                        crs="EPSG:3857",
                        width=1000,
                        height=900,
                        tier="broad",
                    )
                content = client.get_map(layer, request_domain, valid_time)
                if request_domain is not domain:
                    content = reproject_overlay(
                        content,
                        request_domain,
                        domain,
                        outside_no_coverage=layer_id.endswith("coverage"),
                    )
            except Exception:
                # A blank or temporarily unavailable qualitative satellite
                # frame must not abort radar, fire, and hazard ingest for every
                # later domain in the operational cycle.
                if layer.daylight_only or layer.role == "background":
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
                extra={"renderVersion": COVERAGE_RENDER_VERSION} if layer_id.endswith("coverage") else None,
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
    cutoff = max(lightning_times) - dt.timedelta(hours=hours)
    all_anchors = set(radar_times or lightning_times)
    if radar_times:
        # Normal six-minute composite scans remain the display clock. During a
        # workstation outage, however, GeoMet cannot backfill the full native
        # queue window. Add a ten-minute lightning anchor only where there is a
        # real gap in the local radar archive, so recovered lightning is not
        # silently stranded as an unused source frame.
        for lightning_time in lightning_times:
            has_nearby_radar = any(
                abs((radar_time - lightning_time).total_seconds()) <= 6 * 60
                for radar_time in radar_times
            )
            if not has_nearby_radar:
                all_anchors.add(lightning_time)
    anchors = sorted(value for value in all_anchors if value >= cutoff)
    output_layer = LAYERS["lightning-trail"]
    source_layer = LAYERS["lightning"]

    # A previous capability-timeline implementation could manufacture derived
    # frames for anchors whose source radar frame was never downloaded.  Keep
    # only anchors represented in the local archive.  Older, legitimately
    # retained anchors remain valid because ``all_anchors`` is not hour-limited.
    valid_anchor_stamps = {frame_stamp(value) for value in all_anchors}
    output_metadata_root = root / "metadata" / domain.id / output_layer.id
    if output_metadata_root.exists():
        for path in output_metadata_root.rglob("*.json"):
            if path.stem in valid_anchor_stamps:
                continue
            try:
                payload = json.loads(path.read_text())
                image_path = safe_archive_path(root, str(payload.get("path", "")))
                if image_path.is_file():
                    image_path.unlink()
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            path.unlink(missing_ok=True)
    output_frame_root = root / "frames" / domain.id / output_layer.id
    if output_frame_root.exists():
        for path in output_frame_root.rglob(f"*.{output_layer.extension}"):
            if path.stem not in valid_anchor_stamps:
                path.unlink(missing_ok=True)

    for anchor in anchors:
        source_times: list[dt.datetime | None] = []
        used: set[dt.datetime] = set()
        for offset in (0, 10, 20):
            target = anchor - dt.timedelta(minutes=offset)
            selected = at_or_before(lightning_times, target)
            if selected is not None and target - selected > dt.timedelta(minutes=10):
                selected = None
            if selected in used:
                selected = None
            if selected is not None:
                used.add(selected)
            source_times.append(selected)
        paths = [frame_path(root, domain, source_layer, value) if value else None for value in source_times]
        existing = [path if path and path.exists() else None for path in paths]
        destination = frame_path(root, domain, output_layer, anchor)
        meta = metadata_path(root, domain, output_layer, anchor)
        if not any(existing):
            destination.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            continue
        expected_sources = {
            f"age{index * 10}": format_utc(value)
            for index, value in enumerate(source_times)
            if value
        }
        current_sources: dict[str, str] = {}
        current_render_version: int | None = None
        if meta.exists():
            try:
                current_metadata = json.loads(meta.read_text())
                current_sources = current_metadata.get("sourceTimes", {})
                current_render_version = current_metadata.get("renderVersion")
            except (OSError, json.JSONDecodeError):
                current_sources = {}
        if (
            not destination.exists()
            or current_sources != expected_sources
            or current_render_version != LIGHTNING_TRAIL_RENDER_VERSION
        ):
            lightning_trail(existing, destination)
            write_metadata(
                root,
                domain,
                output_layer,
                anchor,
                destination,
                {f"age{index * 10}": value for index, value in enumerate(source_times) if value},
                extra={"renderVersion": LIGHTNING_TRAIL_RENDER_VERSION},
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


def derive_glm_lightning_trails(root: Path, domain: Domain, hours: float = 24.0) -> None:
    """Turn exact ten-minute GLM bins into the common fading bolt symbols."""
    source_layer = LAYERS["glm-lightning"]
    output_layer = LAYERS["glm-lightning-trail"]
    source_times = _archived_layer_times(root, domain, source_layer)
    if not source_times:
        return
    cutoff = max(source_times) - dt.timedelta(hours=hours)
    anchors = [value for value in source_times if value >= cutoff]
    source_set = set(source_times)
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
        if (
            destination.is_file()
            and current_sources == expected_sources
            and current_version == GLM_LIGHTNING_TRAIL_RENDER_VERSION
        ):
            continue
        lightning_trail(existing, destination)
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
            extra={"renderVersion": GLM_LIGHTNING_TRAIL_RENDER_VERSION},
        )


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
            derive_glm_lightning_trails(root, domain)
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
            if keep_frame(valid_time, now, domain.tier):
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
    raw_satellite_status: dict[str, object] = {}
    goes_hazard_status: dict[str, object] = {}
    with GeoMetClient() as client:
        for domain_id in domain_ids:
            domain = DOMAINS[domain_id]
            ensure_static_assets(client, output_root, domain)
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
            excluded = (
                set(NATIVE_LAYER_IDS)
                if domain.id == "bc" and spool_mode == "only"
                else set()
            )
            timelines = ingest_geomet(
                client,
                output_root,
                domain,
                hours,
                latest_only,
                exclude_layers=excluded,
                include_layers=None
                if domain.id == "bc"
                else ("radar-rain", "radar-coverage", "ptype", "ptype-coverage"),
            )
            if domain.id == "bc":
                trail_hours = spool_hours if spool_mode != "off" else hours
                derive_lightning_trails(output_root, domain, timelines, max(hours, trail_hours))
            if domain.id in {"bc", "north-america", "north-pacific"}:
                try:
                    hotspot_status[domain.id] = ingest_hotspot_snapshot(output_root, domain)
                except Exception as error:
                    auxiliary_warnings.append(
                        f"CWFIS wildfire hotspots unavailable: {type(error).__name__}: {error}"
                    )
            if domain.id in {"bc", "north-america", "north-pacific"}:
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
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
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
