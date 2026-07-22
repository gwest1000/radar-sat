from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image, ImageDraw
from pyproj import Transformer

from .config import Domain
from .geomet import projected_bbox


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


def fetch_hotspots(
    domain: Domain,
    *,
    request_get: Callable[..., Any] = requests.get,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Fetch current BC satellite thermal detections from the public CWFIS WFS."""
    response = request_get(
        CWFIS_WFS_URL,
        params={
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": CWFIS_HOTSPOT_LAYER,
            "srsName": "EPSG:4326",
            "CQL_FILTER": "agency='BC'",
            "maxFeatures": "10000",
            "outputFormat": "application/json",
        },
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
        x, y = transformer.transform(lon, lat)
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
