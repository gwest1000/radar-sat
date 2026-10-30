"""Small, projected RDPS pressure-track catalogue for the experimental viewer layer."""
from __future__ import annotations

import datetime as dt
import math
import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform as transform_geometry

from .config import DOMAINS
from .geomet import projected_bbox, projection_longitude

UTC = dt.timezone.utc
ROOT = "https://dd.weather.gc.ca"


@lru_cache(maxsize=1)
def storm_region():
    return shape(json.loads((Path(__file__).resolve().parents[1] / "config" /
                            "bc-storm-region.json").read_text())["geometry"])


def first_approach(points: list[dict]) -> str | None:
    """First entry into BC+300 km, including crossings between native samples."""
    project = Transformer.from_crs(4326, 3005, always_xy=True)
    coordinates = [project.transform(p["lon"], p["lat"]) for p in points]
    region = storm_region()
    for index, coordinate in enumerate(coordinates):
        if region.covers(Point(coordinate)):
            return points[index]["time"]
        if index + 1 < len(points) and region.intersects(LineString(coordinates[index:index + 2])):
            # Ordering precision is the source's native three-hour interval.
            return points[index + 1]["time"]
    return None


def assemble_tracks(collections: list[dict], reference: str) -> dict:
    """Use points, not cumulative lines: each point supplies its own time/pressure."""
    tracks: dict[str, dict] = {}
    for collection in collections:
        if collection.get("reference_datetime") != reference:
            raise ValueError("Mixed forecast runs in storm-track input")
        for feature in collection.get("features", []):
            if feature.get("geometry", {}).get("type") != "Point":
                continue
            props = feature["properties"]
            lon, lat = feature["geometry"]["coordinates"][:2]
            pressure = float(props["pressure"])
            if not all(map(math.isfinite, (lon, lat, pressure))):
                continue
            identifier = props["disturbance_id"]
            track = tracks.setdefault(identifier, {"id": identifier, "points": {}})
            time = props["forecast_datetime"]
            track["points"][time] = {"time": time, "lon": lon, "lat": lat, "pressure": pressure}
    result = []
    for track in tracks.values():
        points = [p for _, p in sorted(track["points"].items())]
        approach = first_approach(points)
        if not approach or len(points) < 2:
            continue
        result.append({"id": track["id"], "approachTime": approach, "points": points})
    result.sort(key=lambda t: (t["approachTime"], t["id"]))
    domains = {}
    boundaries = {}
    unproject = Transformer.from_crs(3005, 4326, always_xy=True).transform
    geographic_region = transform_geometry(unproject, storm_region())
    for domain_id, domain in DOMAINS.items():
        transform = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True, force_over=True)
        xmin, ymin, xmax, ymax = projected_bbox(domain)
        rings = []
        polygons = [geographic_region] if geographic_region.geom_type == "Polygon" else geographic_region.geoms
        for polygon in polygons:
            ring = []
            for lon, lat in polygon.exterior.coords:
                x, y = transform.transform(projection_longitude(lon, domain), lat)
                ring.append([round((x - xmin) / (xmax - xmin), 7), round((ymax - y) / (ymax - ymin), 7)])
            rings.append(ring)
        boundaries[domain_id] = rings
        projected = []
        for number, track in enumerate(result, 1):
            points = []
            for point in track["points"]:
                x, y = transform.transform(projection_longitude(point["lon"], domain), point["lat"])
                if not math.isfinite(x) or not math.isfinite(y):
                    continue
                points.append({"time": point["time"], "pressure": point["pressure"],
                               "x": round((x - xmin) / (xmax - xmin), 7),
                               "y": round((ymax - y) / (ymax - ymin), 7)})
            projected.append({"id": track["id"], "number": number, "points": points})
        domains[domain_id] = projected
    return {"schemaVersion": 1, "model": "RDPS", "kind": "Pressure-MSL",
            "referenceTime": reference, "numbering": "run-local-experimental", "domains": domains,
            "selection": {"bufferKm": 300, "region": "British Columbia including islands"},
            "boundaries": boundaries}


def fetch_latest(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(UTC)
    anchor = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    errors = []
    for offset in range(4):
        run = anchor - dt.timedelta(hours=6 * offset)
        stamp = run.strftime("%Y%m%dT%HZ")
        base = f"{ROOT}/{run:%Y%m%d}/WXO-DD/model_rdps/storm-tracks/{run:%H}"

        def fetch(hour):
            url = f"{base}/{hour:03}/{stamp}_MSC_RDPS-StormTracks_Pressure-MSL_PT{hour:03}H.json"
            response = requests.get(url, timeout=(10, 30))
            response.raise_for_status()
            return response.json()

        try:
            # Check the last lead first so an incomplete new run cannot replace a complete one.
            end = fetch(84)
            with ThreadPoolExecutor(max_workers=4) as pool:
                collections = list(pool.map(fetch, range(0, 84, 3)))
            payload = assemble_tracks([*collections, end], run.isoformat().replace("+00:00", "Z"))
            payload["generatedAt"] = now.isoformat()
            return payload
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(f"{stamp}: {exc}")
    raise RuntimeError("No complete RDPS storm-track run available: " + "; ".join(errors))
