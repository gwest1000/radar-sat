from __future__ import annotations

import datetime as dt
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Domain, GEOMET_URL, Layer


UTC = dt.timezone.utc


def parse_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(UTC)


def format_utc(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def frame_stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%MZ")


def parse_duration(value: str) -> dt.timedelta:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match:
        raise ValueError(f"Unsupported ISO8601 duration: {value}")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return dt.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def parse_time_dimension(value: str) -> list[dt.datetime]:
    values: list[dt.datetime] = []
    for part in value.strip().split(","):
        fields = part.strip().split("/")
        if len(fields) == 1:
            values.append(parse_utc(fields[0]))
            continue
        if len(fields) != 3:
            raise ValueError(f"Unsupported WMS time dimension: {part}")
        start, end = parse_utc(fields[0]), parse_utc(fields[1])
        step = parse_duration(fields[2])
        if step.total_seconds() <= 0:
            raise ValueError(f"Non-positive WMS time step: {part}")
        current = start
        while current <= end:
            values.append(current)
            current += step
    return sorted(set(values))


def projected_bbox(domain: Domain) -> tuple[float, float, float, float]:
    if domain.projected_bounds is not None:
        return domain.projected_bounds
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True)
    points: list[tuple[float, float]] = []
    for index in range(101):
        fraction = index / 100
        lon = domain.west + (domain.east - domain.west) * fraction
        lat = domain.south + (domain.north - domain.south) * fraction
        points.extend(
            [
                transformer.transform(lon, domain.south),
                transformer.transform(lon, domain.north),
                transformer.transform(domain.west, lat),
                transformer.transform(domain.east, lat),
            ]
        )
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    xmin, ymin, xmax, ymax = min(xs), min(ys), max(xs), max(ys)
    desired_ratio = domain.width / domain.height
    actual_ratio = (xmax - xmin) / (ymax - ymin)
    if actual_ratio < desired_ratio:
        padding = ((ymax - ymin) * desired_ratio - (xmax - xmin)) / 2
        xmin -= padding
        xmax += padding
    else:
        padding = ((xmax - xmin) / desired_ratio - (ymax - ymin)) / 2
        ymin -= padding
        ymax += padding
    return xmin, ymin, xmax, ymax


@dataclass(frozen=True)
class LayerTimeline:
    layer: str
    times: tuple[dt.datetime, ...]
    default: dt.datetime


class GeoMetClient:
    def __init__(self, timeout: float = 45.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
        self.session.headers.update({"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"})

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "GeoMetClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def timeline(self, source_layer: str) -> LayerTimeline:
        response = self.session.get(
            GEOMET_URL,
            params={
                "service": "WMS",
                "version": "1.3.0",
                "request": "GetCapabilities",
                "layer": source_layer,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for layer_node in root.findall(".//{*}Layer"):
            name_node = layer_node.find("{*}Name")
            if name_node is None or name_node.text != source_layer:
                continue
            for dimension in layer_node.findall("{*}Dimension"):
                if dimension.attrib.get("name") != "time" or not dimension.text:
                    continue
                times = tuple(parse_time_dimension(dimension.text))
                default_text = dimension.attrib.get("default")
                default = parse_utc(default_text) if default_text else times[-1]
                return LayerTimeline(source_layer, times, default)
        raise RuntimeError(f"No time dimension found for GeoMet layer {source_layer}")

    def get_map(self, layer: Layer, domain: Domain, valid_time: dt.datetime) -> bytes:
        if layer.source_layer is None:
            raise ValueError(f"Derived layer {layer.id} has no WMS source")
        xmin, ymin, xmax, ymax = projected_bbox(domain)
        response = self.session.get(
            GEOMET_URL,
            params={
                "SERVICE": "WMS",
                "VERSION": "1.3.0",
                "REQUEST": "GetMap",
                "LAYERS": layer.source_layer,
                "STYLES": layer.style,
                "CRS": domain.crs,
                "BBOX": f"{xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}",
                "WIDTH": str(domain.width),
                "HEIGHT": str(domain.height),
                "FORMAT": layer.image_format,
                "TRANSPARENT": "TRUE" if layer.image_format == "image/png" else "FALSE",
                "TIME": format_utc(valid_time),
                "LANG": "en",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        geomet_status = response.headers.get("X-GeoMet-Status", "")
        if geomet_status and not geomet_status.startswith("2"):
            raise RuntimeError(f"GeoMet status {geomet_status} for {layer.id} at {format_utc(valid_time)}")
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            excerpt = response.text[:300]
            raise RuntimeError(f"GeoMet returned {content_type or 'unknown content'} for {layer.id}: {excerpt}")
        try:
            image = Image.open(io.BytesIO(response.content))
            image.load()
        except Exception as error:
            raise RuntimeError(f"GeoMet returned an invalid image for {layer.id}") from error
        if image.size != (domain.width, domain.height):
            raise RuntimeError(f"GeoMet returned {image.size}, expected {(domain.width, domain.height)} for {layer.id}")
        if layer.role == "background":
            extrema = image.convert("RGB").getextrema()
            blank = all(low == high for low, high in extrema)
            if blank:
                raise RuntimeError(f"GeoMet returned a blank satellite image for {layer.id}")
        return response.content

    def get_legend(self, layer: Layer) -> bytes:
        if layer.source_layer is None:
            raise ValueError(f"Derived layer {layer.id} has no WMS source")
        response = self.session.get(
            GEOMET_URL,
            params={
                "VERSION": "1.3.0",
                "SERVICE": "WMS",
                "REQUEST": "GetLegendGraphic",
                "SLD_VERSION": "1.1.0",
                "LAYER": layer.source_layer,
                "FORMAT": "image/png",
                "STYLE": layer.style,
                "LANG": "en",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.content


def at_or_before(times: Iterable[dt.datetime], target: dt.datetime) -> dt.datetime | None:
    eligible = [value for value in times if value <= target]
    return max(eligible) if eligible else None
