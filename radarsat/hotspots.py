from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Any, Callable, Sequence

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pyproj import Transformer

from .config import Domain
from .geomet import projected_bbox, projection_longitude


UTC = dt.timezone.utc
CWFIS_WFS_URL = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/ows"
CWFIS_HOTSPOT_LAYER = "public:hotspots_24h"


@dataclass(frozen=True)
class HotspotPoint:
    x: int
    y: int
    detected: dt.datetime
    age_minutes: float
    frp: float
    count: int


@dataclass
class FireDisplayPoint:
    x: float
    y: float
    kind: str
    notable: bool
    highlight: int
    signal: float
    age: int
    count: int = 1


def fetch_hotspots(
    domain: Domain,
    *,
    request_get: Callable[..., Any] = requests.get,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Fetch current satellite thermal detections from the public CWFIS WFS."""
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": CWFIS_HOTSPOT_LAYER,
        "srsName": "EPSG:4326",
        "maxFeatures": "20000" if domain.id == "north-america" else "10000",
        "outputFormat": "application/json",
    }
    # Preserve the compact BC feed used by the legacy Radar-Sat product. The
    # WestWX North America layer keeps all CWFIS detections that project inside
    # its shared map domain.
    if domain.id == "bc":
        params["CQL_FILTER"] = "agency='BC'"
    response = request_get(
        CWFIS_WFS_URL,
        params=params,
        headers={"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"},
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("CWFIS returned a non-JSON hotspot response") from error
    features = payload.get("features")
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise RuntimeError("CWFIS returned an invalid hotspot feature collection")
    return [feature for feature in features if isinstance(feature, dict)]


def parse_detection_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def project_hotspots(
    features: list[dict[str, Any]],
    domain: Domain,
    snapshot_time: dt.datetime,
) -> list[HotspotPoint]:
    """Project and collapse CWFIS detections onto the aligned display grid."""
    snapshot = snapshot_time.astimezone(UTC)
    transformer = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True)
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    clusters: dict[tuple[int, int], dict[str, object]] = {}

    for feature in features:
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        detected = parse_detection_time(properties.get("rep_date"))
        if detected is None:
            continue
        age_hours = (snapshot - detected).total_seconds() / 3600
        if age_hours < -0.25 or age_hours > 24:
            continue
        try:
            lon = float(properties["lon"])
            lat = float(properties["lat"])
            frp = max(0.0, float(properties.get("frp") or 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        x, y = transformer.transform(projection_longitude(lon, domain), lat)
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            continue
        px = round((x - xmin) / (xmax - xmin) * (domain.width - 1))
        py = round((ymax - y) / (ymax - ymin) * (domain.height - 1))
        key = (px, py)
        current = clusters.get(key)
        if current is None or detected > current["detected"]:
            clusters[key] = {
                "detected": detected,
                "age": age_hours,
                "frp": frp,
                "count": int(current["count"]) + 1 if current is not None else 1,
            }
        else:
            current["frp"] = max(float(current["frp"]), frp)
            current["count"] = int(current["count"]) + 1

    return [
        HotspotPoint(
            x=x,
            y=y,
            detected=item["detected"],
            age_minutes=float(item["age"]) * 60.0,
            frp=float(item["frp"]),
            count=int(item["count"]),
        )
        for (x, y), item in sorted(clusters.items(), key=lambda item: item[1]["detected"])
    ]


def render_hotspots(
    features: list[dict[str, Any]],
    domain: Domain,
    destination: Path,
    snapshot_time: dt.datetime,
) -> dict[str, object]:
    """Render a 24-hour, age-coloured hotspot snapshot on the aligned map grid."""
    points = project_hotspots(features, domain, snapshot_time)

    image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    newest: dt.datetime | None = None

    def diamond(x: int, y: int, radius: int) -> list[tuple[int, int]]:
        return [(x, y - radius), (x + radius, y), (x, y + radius), (x - radius, y)]

    for point in points:
        x = point.x
        y = point.y
        detected = point.detected
        age = point.age_minutes / 60.0
        frp = point.frp
        newest = detected if newest is None or detected > newest else newest
        # These are point detections rather than fire perimeters. Use a larger
        # high-contrast symbol so isolated detections remain legible on the BC
        # Large view and above bright satellite cloud or high reflectivity.
        radius = 6 + min(3, int(math.log10(frp + 1)))
        if age <= 6:
            fill = (255, 229, 92, 255)
        elif age <= 12:
            fill = (255, 148, 31, 230)
        else:
            fill = (217, 75, 61, 195)
        draw.polygon(diamond(x, y, radius + 3), fill=(2, 7, 11, 230))
        draw.polygon(diamond(x, y, radius + 1), fill=(255, 255, 255, min(245, fill[3])))
        draw.polygon(diamond(x, y, radius), fill=fill)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        image.save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "detectionCount": len(points),
        "newestDetectionTime": newest.isoformat().replace("+00:00", "Z") if newest else None,
        "windowHours": 24,
    }


def _flame_outline(size: float) -> list[tuple[float, float]]:
    """Sample the browser flame's cubic path into a compact polygon."""

    def cubic(
        start: tuple[float, float],
        control_a: tuple[float, float],
        control_b: tuple[float, float],
        end: tuple[float, float],
    ) -> list[tuple[float, float]]:
        values: list[tuple[float, float]] = []
        for index in range(1, 7):
            amount = index / 6
            inverse = 1 - amount
            values.append((
                inverse ** 3 * start[0]
                + 3 * inverse ** 2 * amount * control_a[0]
                + 3 * inverse * amount ** 2 * control_b[0]
                + amount ** 3 * end[0],
                inverse ** 3 * start[1]
                + 3 * inverse ** 2 * amount * control_a[1]
                + 3 * inverse * amount ** 2 * control_b[1]
                + amount ** 3 * end[1],
            ))
        return values

    segments = [
        ((12, 3), (13, 7), (16, 7), (17, 10)),
        ((17, 10), (21, 13), (20, 19), (16, 21)),
        ((16, 21), (11, 24), (5, 21), (5, 16)),
        ((5, 16), (5, 14), (6, 12), (7, 11)),
        ((7, 11), (8, 14), (11, 14), (11, 11)),
        ((11, 11), (11, 8), (9.5, 7), (9.5, 5)),
        ((9.5, 5), (9.5, 4), (10.5, 3.5), (12, 3)),
    ]
    points = [(12.0, 3.0)]
    for segment in segments:
        points.extend(cubic(*segment))
    return [
        ((x - 12) * size / 24, (y - 12) * size / 24)
        for x, y in points
    ]


def _cluster_notable_fires(
    markers: list[FireDisplayPoint],
    domain: Domain,
) -> list[FireDisplayPoint]:
    regular = [marker for marker in markers if not marker.notable]
    remaining = [marker for marker in markers if marker.notable]
    clustered: list[FireDisplayPoint] = []
    cluster_distance = 0.008 if domain.id == "bc" else 0.0035
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        for index in range(len(remaining) - 1, -1, -1):
            candidate = remaining[index]
            if (
                candidate.highlight == seed.highlight
                and math.hypot(candidate.x - seed.x, candidate.y - seed.y) < cluster_distance
            ):
                group.append(candidate)
                remaining.pop(index)
        if len(group) == 1:
            clustered.append(seed)
            continue
        clustered.append(FireDisplayPoint(
            x=sum(marker.x for marker in group) / len(group),
            y=sum(marker.y for marker in group) / len(group),
            kind="active",
            notable=True,
            highlight=seed.highlight,
            signal=max(marker.signal for marker in group),
            age=0,
            count=sum(marker.count for marker in group),
        ))
    return [*regular, *clustered]


def render_fire_overlay(
    hotspot_rows: Sequence[Sequence[float | int | None]],
    active_rows: Sequence[Sequence[float | int | None]],
    domain: Domain,
    destination: Path,
    *,
    hotspot_age_offset_minutes: float = 0,
    viewport: dict[str, float] | None = None,
    output_width: int | None = None,
    supersample: int = 1,
) -> dict[str, int]:
    """Render browser-equivalent wildfire flames into a transparent PNG."""
    if (viewport is None) != (output_width is None):
        raise ValueError("Regional fire renders require both viewport and output width")
    if supersample < 1:
        raise ValueError("Fire-overlay supersampling must be at least one")
    overview = domain.id in {"north-america", "north-pacific"}
    active_markers: list[FireDisplayPoint] = []
    for row in active_rows:
        if len(row) < 6:
            continue
        try:
            x, y = float(row[0]), float(row[1])
            size_hectares = float(row[3] or 0)
            highlight = int(row[5] or 0)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1):
            continue
        if overview and highlight == 0:
            continue
        active_markers.append(FireDisplayPoint(
            x=x,
            y=y,
            kind="active",
            notable=highlight > 0,
            highlight=highlight,
            signal=size_hectares,
            age=0,
        ))
    active_markers = _cluster_notable_fires(active_markers, domain)

    hotspot_markers: list[FireDisplayPoint] = []
    duplicate_distance = 0.012 if domain.id == "bc" else 0.0035
    for row in hotspot_rows:
        if len(row) < 4:
            continue
        try:
            x, y = float(row[0]), float(row[1])
            point_age = float(row[2] or 0)
            frp = float(row[3] or 0)
        except (TypeError, ValueError):
            continue
        if not (
            math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(point_age)
            and 0 <= x <= 1
            and 0 <= y <= 1
        ):
            continue
        if overview and frp < 100:
            continue
        if any(
            math.hypot(x - active.x, y - active.y) < duplicate_distance
            for active in active_markers
        ):
            continue
        total_age = max(0, hotspot_age_offset_minutes + point_age)
        hotspot_markers.append(FireDisplayPoint(
            x=x,
            y=y,
            kind="hotspot",
            notable=False,
            highlight=0,
            signal=frp,
            age=0 if total_age <= 6 * 60 else 1 if total_age <= 12 * 60 else 2,
        ))

    markers = sorted(
        [*hotspot_markers, *active_markers],
        key=lambda marker: (marker.notable, marker.signal),
    )
    if viewport is not None and output_width is not None:
        regional_markers: list[FireDisplayPoint] = []
        margin = 0.025
        for marker in markers:
            relative_x = (marker.x - viewport["left"]) / viewport["width"]
            relative_y = (marker.y - viewport["top"]) / viewport["height"]
            if -margin <= relative_x <= 1 + margin and -margin <= relative_y <= 1 + margin:
                regional_markers.append(replace(marker, x=relative_x, y=relative_y))
        markers = regional_markers
        crop_width = domain.width * viewport["width"]
        crop_height = domain.height * viewport["height"]
        final_size = (
            output_width,
            max(1, round(output_width * crop_height / crop_width)),
        )
        canvas_size = (
            final_size[0] * supersample,
            final_size[1] * supersample,
        )
        symbol_scale = output_width / 960 * supersample
    else:
        final_size = (domain.width, domain.height)
        canvas_size = final_size
        symbol_scale = domain.width / 1280

    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    symbol_draw = ImageDraw.Draw(canvas, "RGBA")
    hotspot_colours = [
        (255, 176, 93, 255),
        (240, 141, 67, 184),
        (201, 102, 59, 112),
    ]

    for marker in markers:
        desired_size = 21 if marker.notable else 13
        size = max(8, round(desired_size * symbol_scale))
        centre_x = marker.x * (canvas_size[0] - 1)
        centre_y = marker.y * (canvas_size[1] - 1)
        outline = [
            (round(centre_x + x), round(centre_y + y))
            for x, y in _flame_outline(size)
        ]
        closed = [*outline, outline[0]]
        if marker.kind == "active":
            glow_width = max(3, round(size * 0.26))
            glow_draw.line(
                closed,
                fill=(255, 229, 190, 150),
                width=glow_width,
                joint="curve",
            )
            glow_draw.polygon(outline, fill=(255, 188, 141, 95))

    canvas.alpha_composite(
        glow.filter(ImageFilter.GaussianBlur(radius=max(1.5, symbol_scale * 1.6)))
    )
    for marker in markers:
        desired_size = 21 if marker.notable else 13
        size = max(8, round(desired_size * symbol_scale))
        centre_x = marker.x * (canvas_size[0] - 1)
        centre_y = marker.y * (canvas_size[1] - 1)
        outline = [
            (round(centre_x + x), round(centre_y + y))
            for x, y in _flame_outline(size)
        ]
        closed = [*outline, outline[0]]
        if marker.notable:
            symbol_draw.line(
                closed,
                fill=(255, 228, 91, 255),
                width=max(4, round(size * 0.20)),
                joint="curve",
            )
        if marker.kind == "active":
            colour = (255, 121, 86, 255)
            symbol_draw.polygon(outline, fill=colour)
            symbol_draw.line(
                closed,
                fill=colour,
                width=max(2, round(size * 0.10)),
                joint="curve",
            )
        else:
            colour = hotspot_colours[marker.age]
            symbol_draw.line(
                closed,
                fill=(2, 7, 11, min(210, colour[3])),
                width=max(3, round(size * 0.17)),
                joint="curve",
            )
            symbol_draw.line(
                closed,
                fill=colour,
                width=max(2, round(size * 0.11)),
                joint="curve",
            )
        if marker.count > 1:
            font_size = max(8, round(size * 0.43))
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
            except OSError:
                font = ImageFont.load_default(size=font_size)
            label = str(marker.count)
            symbol_draw.text(
                (round(centre_x), round(centre_y + size * 0.055)),
                label,
                fill=(58, 21, 11, 255),
                font=font,
                anchor="mm",
                stroke_width=1,
                stroke_fill=(255, 242, 204, 210),
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        final_canvas = (
            canvas.resize(final_size, Image.Resampling.LANCZOS)
            if canvas.size != final_size
            else canvas
        )
        final_canvas.quantize(
            colors=64,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.NONE,
        ).save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "activeFireDisplayCount": sum(marker.kind == "active" for marker in markers),
        "hotspotDisplayCount": sum(marker.kind == "hotspot" for marker in markers),
    }
