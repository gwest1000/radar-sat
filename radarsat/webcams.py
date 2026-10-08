"""Public webcam discovery; image pixels stay on the provider's servers."""
from __future__ import annotations

import datetime as dt
import math
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from pyproj import Transformer

from .config import DOMAINS
from .geomet import projected_bbox, projection_longitude

ARFI_SERVICE = "https://services8.arcgis.com/eGKqjKGEXhAGUD5f/arcgis/rest/services/ARFI_Master_Table/FeatureServer/0"
DRIVEBC_FEED = "https://www.drivebc.ca/api/webcams/"


def public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return None
    # Avoid mixed-content images/links on the HTTPS viewer.
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))


def drivebc_id(url: str | None) -> str | None:
    host = urlsplit(url).hostname if url else None
    if not host or not (host == "drivebc.ca" or host.endswith(".drivebc.ca")):
        return None
    match = re.search(r"/(\d+)\.(?:html|jpg)(?:\?|$)", url)
    return match.group(1) if match else None


def normalize_cameras(arfi: list[dict], drivebc: list[dict]) -> list[dict]:
    cameras: dict[str, dict] = {}
    for feature in arfi:
        a = feature["attributes"]
        links = [{"label": str(a.get(f"URL{i}_name") or "Camera webpage"), "url": url}
                 for i in range(1, 4) if (url := public_url(a.get(f"URL{i}")))]
        if not links:
            continue
        previews = [{"label": str(a.get(f"Thumbnail{i}_name") or "Camera view"), "url": url}
                    for i in range(1, 7) if (url := public_url(a.get(f"Thumbnail{i}")))]
        # The official webpage ID is authoritative; several ARFI thumbnails
        # accidentally point to a different DriveBC camera.
        dbc = drivebc_id(links[0]["url"])
        key = f"drivebc:{dbc}" if dbc else f"arfi:{a['FID']}"
        if dbc:
            links[0] = {"label": "DriveBC camera webpage", "url": f"https://www.drivebc.ca/cameras/{dbc}"}
            previews = [{"label": "Current view", "url": f"https://www.drivebc.ca/images/{dbc}.jpg"}, *previews[1:]]
        geometry = feature.get("geometry", {})
        cameras[key] = {
            "id": key, "name": str(a.get("Name") or "Webcam").strip(),
            "longitude": float(geometry.get("x", a["Long"])),
            "latitude": float(geometry.get("y", a["Lat"])),
            "provider": "Avalanche Canada ARFI", "kind": str(a.get("Type", "Webcam")),
            "links": links, "previews": previews, "credit": "Camera operator; locations from Avalanche Canada ARFI",
        }
    for camera in drivebc:
        key = f"drivebc:{camera['id']}"
        if not camera.get("is_on", True) or not camera.get("should_appear", True):
            cameras.pop(key, None)
            continue
        longitude, latitude = camera["location"]["coordinates"]
        cameras[key] = {
            "id": key, "name": camera.get("name_override") or camera["name"],
            "longitude": longitude, "latitude": latitude, "provider": "DriveBC",
            "kind": "Cams - Highway", "credit": camera.get("credit") or "DriveBC / Province of British Columbia",
            "links": [{"label": "DriveBC camera webpage", "url": f"https://www.drivebc.ca/cameras/{camera['id']}"}],
            "previews": [{"label": camera.get("orientation") or "Current view", "url": f"https://www.drivebc.ca/images/{camera['id']}.jpg"}],
        }
    return sorted(cameras.values(), key=lambda c: c["id"])


def project_cameras(cameras: list[dict]) -> dict[str, list[dict]]:
    result = {}
    for domain_id, domain in DOMAINS.items():
        transformer = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True, force_over=True)
        xmin, ymin, xmax, ymax = projected_bbox(domain)
        points = []
        for camera in cameras:
            x, y = transformer.transform(projection_longitude(camera["longitude"], domain), camera["latitude"])
            x, y = (x - xmin) / (xmax - xmin), (ymax - y) / (ymax - ymin)
            if math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1:
                points.append({"id": camera["id"], "x": round(x, 7), "y": round(y, 7)})
        result[domain_id] = points
    return result


def discover_webcams(session: requests.Session) -> dict:
    features = []
    offset = 0
    while True:
        response = session.get(f"{ARFI_SERVICE}/query", params={
            "f": "json", "where": "Type LIKE 'Cams%'", "outFields": "*", "outSR": 4326,
            "resultOffset": offset, "resultRecordCount": 1000, "orderByFields": "FID",
        }, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError(f"ARFI camera query failed: {payload['error']}")
        page = payload.get("features", [])
        features.extend(page)
        if not payload.get("exceededTransferLimit"):
            break
        if not page:
            raise ValueError("ARFI pagination stopped making progress")
        offset += len(page)
    response = session.get(DRIVEBC_FEED, timeout=45)
    response.raise_for_status()
    cameras = normalize_cameras(features, response.json())
    if not features or not cameras:
        raise ValueError("Refusing to replace the webcam catalogue with an empty feed")
    return {"schemaVersion": 1, "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "cameras": cameras, "domains": project_cameras(cameras)}
