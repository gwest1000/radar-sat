from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import math
from typing import Any, Callable

import requests
from pyproj import Transformer

from .config import Domain
from .geomet import projected_bbox


UTC = dt.timezone.utc
CWFIF_WFS_URL = "https://geoserver.cwfif.nrcan.gc.ca/geoserver/public/ows"
CWFIF_ACTIVE_FIRE_LAYER = "public:cwfif_national_activefires"
NIFC_ACTIVE_FIRE_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
BCWS_ACTIVE_FIRE_URL = (
    "https://services6.arcgis.com/ubm4tcTYICKBpist/ArcGIS/rest/services/"
    "BCWS_ActiveFires_PublicView/FeatureServer/0/query"
)
CANADA_SOURCE_CODE = 1
UNITED_STATES_SOURCE_CODE = 2
STANDARD_FIRE_CODE = 0
CANADA_WILDFIRE_OF_NOTE_CODE = 1
US_LARGE_INCIDENT_CODE = 2


@dataclass(frozen=True)
class ActiveFirePoint:
    x: int
    y: int
    status_age_minutes: float | None
    size_hectares: float
    source_code: int
    highlight_code: int


def _feature_collection(response: Any, label: str) -> list[dict[str, Any]]:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"{label} returned a non-JSON response") from error
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        message = payload["error"].get("message") or "unknown service error"
        raise RuntimeError(f"{label} returned an error: {message}")
    features = payload.get("features") if isinstance(payload, dict) else None
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise RuntimeError(f"{label} returned an invalid feature collection")
    return [feature for feature in features if isinstance(feature, dict)]


def fetch_canadian_active_fires(
    snapshot_time: dt.datetime,
    *,
    request_get: Callable[..., Any] = requests.get,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Fetch the current, agency-reported Canadian wildfire snapshot."""
    current = snapshot_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    response = request_get(
        CWFIF_WFS_URL,
        params={
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": CWFIF_ACTIVE_FIRE_LAYER,
            "srsName": "EPSG:4326",
            # The new CWFIF layer is a versioned archive. Current records have
            # a record_end later than the requested snapshot time.
            "CQL_FILTER": f"record_end > '{current}'",
            "maxFeatures": "5000",
            "outputFormat": "application/json",
        },
        headers={"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"},
        timeout=timeout,
    )
    return _feature_collection(response, "CWFIF")


def fetch_bc_active_fires(
    *,
    request_get: Callable[..., Any] = requests.get,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Fetch current BCWS fires, including the official Fire of Note flag."""
    response = request_get(
        BCWS_ACTIVE_FIRE_URL,
        params={
            "where": "FIRE_STATUS <> 'Out'",
            "outFields": (
                "FIRE_NUMBER,FIRE_STATUS,CURRENT_SIZE,FIRE_OF_NOTE_IND,"
                "LATITUDE,LONGITUDE"
            ),
            "returnGeometry": "true",
            "outSR": "4326",
            "resultRecordCount": "1000",
            "f": "geojson",
        },
        headers={"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"},
        timeout=timeout,
    )
    return _feature_collection(response, "BC Wildfire Service")


def fetch_us_active_fires(
    *,
    request_get: Callable[..., Any] = requests.get,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Fetch current U.S. wildfires from NIFC WFIGS/IRWIN."""
    response = request_get(
        NIFC_ACTIVE_FIRE_URL,
        params={
            "where": "IncidentTypeCategory='WF' AND ActiveFireCandidate=1",
            "outFields": (
                "IncidentSize,ModifiedOnDateTime_dt,InitialLatitude,InitialLongitude,"
                "ICS209ReportDateTime,ICS209ReportStatus"
            ),
            # WestWX initially covers western North America. The spatial filter
            # and attribute-only response sharply reduce load on this heavily
            # used public service while retaining Alaska and the western U.S.
            "geometry": "-180,20,-100,75",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "returnGeometry": "false",
            "outSR": "4326",
            "resultRecordCount": "2000",
            "f": "geojson",
        },
        headers={"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"},
        timeout=timeout,
    )
    return _feature_collection(response, "NIFC WFIGS")


def _parse_time(value: object) -> dt.datetime | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        try:
            return dt.datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _coordinates(feature: dict[str, Any], properties: dict[str, Any]) -> tuple[float, float] | None:
    geometry = feature.get("geometry")
    if isinstance(geometry, dict):
        coordinates = geometry.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            try:
                return float(coordinates[0]), float(coordinates[1])
            except (TypeError, ValueError):
                pass
    for lon_key, lat_key in (
        ("longitude", "latitude"),
        ("InitialLongitude", "InitialLatitude"),
        ("LONGITUDE", "LATITUDE"),
    ):
        try:
            return float(properties[lon_key]), float(properties[lat_key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def project_active_fires(
    canadian_features: list[dict[str, Any]],
    us_features: list[dict[str, Any]],
    domain: Domain,
    snapshot_time: dt.datetime,
    *,
    bc_features: list[dict[str, Any]] | None = None,
) -> list[ActiveFirePoint]:
    """Project confirmed active wildfires onto a shared display grid."""
    snapshot = snapshot_time.astimezone(UTC)
    transformer = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True, force_over=True)
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    points: dict[tuple[int, int, int], ActiveFirePoint] = {}

    # BCWS is the authoritative source for the provincial Fire of Note flag.
    # When it is available, replace CWFIF's BC subset to avoid duplicate points.
    bcws_features = bc_features or []
    cwfif_features = canadian_features
    if bcws_features:
        cwfif_features = [
            feature
            for feature in canadian_features
            if str((feature.get("properties") or {}).get("agency_code", "")).upper() != "BC"
        ]

    for source_code, feature_source, features in (
        (CANADA_SOURCE_CODE, "cwfif", cwfif_features),
        (CANADA_SOURCE_CODE, "bcws", bcws_features),
        (UNITED_STATES_SOURCE_CODE, "nifc", us_features),
    ):
        for feature in features:
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                continue
            highlight_code = STANDARD_FIRE_CODE
            if feature_source == "cwfif":
                prescribed = properties.get("fire_was_prescribed")
                if prescribed not in (None, 0, False, "0", "false", "False"):
                    continue
                updated = _parse_time(
                    properties.get("status_date") or properties.get("situation_report_date")
                )
                size_value = properties.get("fire_size")
                size_multiplier = 1.0
            elif feature_source == "bcws":
                updated = None
                size_value = properties.get("CURRENT_SIZE")
                size_multiplier = 1.0
                if str(properties.get("FIRE_OF_NOTE_IND", "")).upper() == "Y":
                    highlight_code = CANADA_WILDFIRE_OF_NOTE_CODE
            else:
                updated = _parse_time(properties.get("ModifiedOnDateTime_dt"))
                size_value = properties.get("IncidentSize")
                # IRWIN incident size is reported in acres.
                size_multiplier = 0.40468564224
                # NIFC's closest operational equivalent to a Canadian Fire of
                # Note is a large incident with a current initial/update ICS-209.
                # A final report (F) is deliberately not highlighted.
                if str(properties.get("ICS209ReportStatus", "")).upper() in {"I", "U"}:
                    highlight_code = US_LARGE_INCIDENT_CODE

            coordinate = _coordinates(feature, properties)
            if coordinate is None:
                continue
            lon, lat = coordinate
            x, y = transformer.transform(lon, lat)
            if not all(math.isfinite(value) for value in (x, y)):
                continue
            if not (xmin <= x <= xmax and ymin <= y <= ymax):
                continue
            px = round((x - xmin) / (xmax - xmin) * (domain.width - 1))
            py = round((ymax - y) / (ymax - ymin) * (domain.height - 1))
            try:
                size_hectares = max(0.0, float(size_value or 0.0) * size_multiplier)
            except (TypeError, ValueError):
                size_hectares = 0.0
            status_age = None
            if updated is not None:
                status_age = max(0.0, (snapshot - updated).total_seconds() / 60.0)
            candidate = ActiveFirePoint(
                px,
                py,
                status_age,
                size_hectares,
                source_code,
                highlight_code,
            )
            key = (px, py, source_code)
            previous = points.get(key)
            if previous is None or (
                candidate.highlight_code,
                candidate.size_hectares,
            ) > (
                previous.highlight_code,
                previous.size_hectares,
            ):
                points[key] = candidate

    return [points[key] for key in sorted(points)]
