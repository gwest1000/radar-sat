from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Protocol
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import DOMAINS, LAYERS, VIEWPORTS, Domain
from .geomet import format_utc, projected_bbox
from .pipeline import frame_path, metadata_path, write_metadata


UTC = dt.timezone.utc
NEXRAD_LEVEL3_URL = "https://unidata-nexrad-level3.s3.amazonaws.com"
SOUTH_COAST_LAYER_ID = "radar-rain-region-south-coast"
SOUTH_COAST_WIDTH = 1920
SOUTH_COAST_RENDER_VERSION = 1
NEXRAD_MAX_AGE = dt.timedelta(minutes=8)
ECCC_MAX_AGE = dt.timedelta(minutes=12)
NEXRAD_CACHE_DAYS = 8

# DPR is product 176: 250-m range bins, one-degree radials, 230-km range.
DPR_FIRST_GATE_METRES = 125.0
DPR_GATE_WIDTH_METRES = 250.0
DPR_GATE_COUNT = 920
DPR_RADIAL_COUNT = 360

NEXRAD_SITES: dict[str, tuple[float, float]] = {
    "ATX": (48.1949997, -122.4960022),
    "LGX": (47.1160011, -124.1070023),
}

_OBJECT_RE = re.compile(
    r"^(?P<site>ATX|LGX)_DPR_(?P<stamp>\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})$"
)

# The stops and colours are sampled from ECCC's operational
# RADARURPPRECIPR14-LINEAR legend.  Keeping the same physical quantity and
# colour ramp makes the higher-resolution U.S. insert readable without a
# misleading seam or a second legend.
RAIN_RATE_STOPS = np.asarray(
    [0.1, 1.0, 2.0, 4.0, 8.0, 12.0, 16.0, 24.0, 32.0, 50.0, 64.0, 100.0, 125.0, 200.0],
    dtype=np.float32,
)
RAIN_RATE_COLOURS = np.asarray(
    [
        (152, 203, 254),
        (0, 152, 254),
        (0, 250, 93),
        (0, 195, 0),
        (0, 140, 0),
        (85, 152, 0),
        (254, 233, 0),
        (254, 178, 0),
        (254, 123, 0),
        (254, 34, 0),
        (254, 1, 114),
        (169, 43, 195),
        (106, 4, 157),
        (51, 0, 77),
    ],
    dtype=np.float32,
)


@dataclass(frozen=True)
class NexradObject:
    site: str
    key: str
    valid_time: dt.datetime
    size: int = 0


@dataclass(frozen=True)
class LocalRadarFrame:
    valid_time: dt.datetime
    image_path: Path
    metadata_path: Path


class NexradSource(Protocol):
    def objects_between(
        self,
        sites: Iterable[str],
        start: dt.datetime,
        end: dt.datetime,
    ) -> list[NexradObject]: ...

    def fetch(self, item: NexradObject, cache_root: Path) -> Path: ...


def parse_nexrad_object(key: str, *, size: int = 0) -> NexradObject | None:
    match = _OBJECT_RE.fullmatch(key)
    if match is None:
        return None
    valid = dt.datetime.strptime(match.group("stamp"), "%Y_%m_%d_%H_%M_%S").replace(tzinfo=UTC)
    return NexradObject(match.group("site"), key, valid, size)


class NexradLevel3Client:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self.session.mount(
            "https://",
            HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=6),
        )
        self.session.headers.update(
            {"User-Agent": "Radar-Sat/0.1 (+https://github.com/gwest1000/radar-sat)"}
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "NexradLevel3Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _day_objects(self, site: str, day: dt.date) -> list[NexradObject]:
        prefix = f"{site}_DPR_{day:%Y_%m_%d}"
        response = self.session.get(
            NEXRAD_LEVEL3_URL,
            params={"list-type": "2", "prefix": prefix, "max-keys": "1000"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        values: list[NexradObject] = []
        for node in root.findall(".//{*}Contents"):
            key_node = node.find("{*}Key")
            size_node = node.find("{*}Size")
            if key_node is None or not key_node.text:
                continue
            item = parse_nexrad_object(
                key_node.text,
                size=int(size_node.text) if size_node is not None and size_node.text else 0,
            )
            if item is not None:
                values.append(item)
        return values

    def objects_between(
        self,
        sites: Iterable[str],
        start: dt.datetime,
        end: dt.datetime,
    ) -> list[NexradObject]:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        days = []
        current = start.date()
        while current <= end.date():
            days.append(current)
            current += dt.timedelta(days=1)
        tasks = [(site, day) for site in sites for day in days]
        objects: list[NexradObject] = []
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(tasks)))) as executor:
            futures = [executor.submit(self._day_objects, site, day) for site, day in tasks]
            for future in as_completed(futures):
                objects.extend(future.result())
        return sorted(
            (item for item in objects if start <= item.valid_time <= end),
            key=lambda item: (item.valid_time, item.site, item.key),
        )

    def fetch(self, item: NexradObject, cache_root: Path) -> Path:
        if parse_nexrad_object(item.key) is None:
            raise ValueError(f"Unsafe or unsupported NEXRAD object key: {item.key!r}")
        destination = cache_root / item.site / item.valid_time.strftime("%Y/%m/%d") / item.key
        if destination.is_file() and (item.size <= 0 or destination.stat().st_size == item.size):
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self.session.get(f"{NEXRAD_LEVEL3_URL}/{item.key}", timeout=self.timeout)
        response.raise_for_status()
        if item.size > 0 and len(response.content) != item.size:
            raise RuntimeError(
                f"NEXRAD object {item.key} has {len(response.content)} bytes; expected {item.size}"
            )
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_bytes(response.content)
        temporary.replace(destination)
        return destination


def _parse_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _local_radar_frames(root: Path) -> list[LocalRadarFrame]:
    directory = root / "metadata" / "bc" / "radar-rain"
    frames: list[LocalRadarFrame] = []
    if not directory.is_dir():
        return frames
    for path in directory.rglob("*.json"):
        try:
            payload = json.loads(path.read_text())
            valid = _parse_utc(str(payload["validTime"]))
            relative = Path(str(payload["path"]))
            image = root / relative
            if relative.is_absolute() or ".." in relative.parts or not image.is_file():
                continue
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        frames.append(LocalRadarFrame(valid, image, path))
    return sorted(frames, key=lambda item: item.valid_time)


def _floor_time(value: dt.datetime, minutes: int) -> dt.datetime:
    value = value.astimezone(UTC).replace(second=0, microsecond=0)
    return value - dt.timedelta(minutes=value.minute % minutes)


def _ceil_time(value: dt.datetime, minutes: int) -> dt.datetime:
    floored = _floor_time(value, minutes)
    return floored if floored == value.replace(second=0, microsecond=0) else floored + dt.timedelta(minutes=minutes)


def _anchor_times(
    frames: list[LocalRadarFrame],
    hours: float,
    latest_only: bool,
    now: dt.datetime,
) -> list[dt.datetime]:
    if not frames:
        return []
    latest = frames[-1].valid_time
    if latest_only:
        return [latest]
    cutoff = max(frames[0].valid_time, now - dt.timedelta(hours=hours))
    values: list[dt.datetime] = []
    current = _ceil_time(cutoff, 10 if now - cutoff <= dt.timedelta(hours=24) else 60)
    while current <= _floor_time(latest, 10):
        age = now - current
        if age <= dt.timedelta(hours=24) or current.minute == 0:
            values.append(current)
        current += dt.timedelta(minutes=10)
    # Preserve the true newest radar time for the independently published live
    # edge; historical/video tracks continue to use the regular anchor clock.
    if not values or values[-1] != latest:
        values.append(latest)
    return sorted(set(values))


def _latest_frame(
    frames: list[LocalRadarFrame],
    target: dt.datetime,
    max_age: dt.timedelta,
) -> LocalRadarFrame | None:
    for item in reversed(frames):
        if item.valid_time <= target:
            return item if target - item.valid_time <= max_age else None
    return None


def _latest_object(
    objects: list[NexradObject],
    site: str,
    target: dt.datetime,
) -> NexradObject | None:
    for item in reversed(objects):
        if item.site == site and item.valid_time <= target:
            return item if target - item.valid_time <= NEXRAD_MAX_AGE else None
    return None


def _south_coast_height(domain: Domain) -> int:
    viewport = VIEWPORTS["south-coast"]
    raw = (
        SOUTH_COAST_WIDTH
        * domain.height
        / domain.width
        * viewport["height"]
        / viewport["width"]
    )
    rounded = max(2, round(raw))
    return rounded if rounded % 2 == 0 else rounded + 1


@lru_cache(maxsize=2)
def _sampling_map(
    site: str,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    domain = DOMAINS["bc"]
    viewport = VIEWPORTS["south-coast"]
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    span_x = xmax - xmin
    span_y = ymax - ymin
    west = xmin + viewport["left"] * span_x
    east = xmin + (viewport["left"] + viewport["width"]) * span_x
    north = ymax - viewport["top"] * span_y
    south = ymax - (viewport["top"] + viewport["height"]) * span_y
    xs = np.linspace(west, east, width, endpoint=False, dtype=np.float64)
    xs += (east - west) / width / 2
    ys = np.linspace(north, south, height, endpoint=False, dtype=np.float64)
    ys -= (north - south) / height / 2

    latitude, longitude = NEXRAD_SITES[site]
    radar_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={latitude:.8f} +lon_0={longitude:.8f} "
        "+datum=WGS84 +units=m +no_defs"
    )
    transformer = Transformer.from_crs(domain.crs, radar_crs, always_xy=True)
    radial_index = np.zeros((height, width), dtype=np.uint16)
    gate_index = np.zeros((height, width), dtype=np.uint16)
    valid = np.zeros((height, width), dtype=bool)
    for row in range(0, height, 96):
        stop = min(height, row + 96)
        xx = np.broadcast_to(xs, (stop - row, width))
        yy = np.broadcast_to(ys[row:stop, None], (stop - row, width))
        radar_x, radar_y = transformer.transform(xx, yy)
        distance = np.hypot(radar_x, radar_y)
        azimuth = np.mod(np.degrees(np.arctan2(radar_x, radar_y)), 360.0)
        gates = np.floor((distance - DPR_FIRST_GATE_METRES) / DPR_GATE_WIDTH_METRES).astype(np.int32)
        block_valid = (gates >= 0) & (gates < DPR_GATE_COUNT)
        radial_index[row:stop] = np.floor(azimuth).astype(np.uint16) % DPR_RADIAL_COUNT
        gate_index[row:stop] = np.clip(gates, 0, DPR_GATE_COUNT - 1).astype(np.uint16)
        valid[row:stop] = block_valid
    return radial_index, gate_index, valid


def _decode_dpr(path: Path) -> tuple[np.ndarray, dt.datetime, str]:
    # Lazy import keeps the regular ingest/browser test path light when this
    # optional regional feed is disabled or unavailable.
    from metpy.io import Level3File

    product = Level3File(path)
    packet = next(
        (
            item
            for page in product.sym_block
            for item in page
            if isinstance(item, dict) and item.get("code") == 176 and "components" in item
        ),
        None,
    )
    if packet is None:
        raise ValueError(f"{path.name} is not a DPR product 176")
    component = packet["components"]
    if (
        not math.isclose(float(component.first_gate), DPR_FIRST_GATE_METRES)
        or not math.isclose(float(component.gate_width), DPR_GATE_WIDTH_METRES)
        or len(component.radials) != DPR_RADIAL_COUNT
    ):
        raise ValueError(f"Unexpected DPR polar geometry in {path.name}")
    values = np.zeros((DPR_RADIAL_COUNT, DPR_GATE_COUNT), dtype=np.float32)
    for radial in component.radials:
        row = int(round(float(radial.azimuth))) % DPR_RADIAL_COUNT
        data = np.asarray(radial.data, dtype=np.float32)
        count = min(DPR_GATE_COUNT, int(radial.num_bins), data.size)
        # Product 176 values are thousandths of an inch per hour.
        values[row, :count] = data[:count] * 0.0254
    valid = product.metadata["vol_time"]
    if valid.tzinfo is None:
        valid = valid.replace(tzinfo=UTC)
    else:
        valid = valid.astimezone(UTC)
    return values, valid, str(packet.get("radar_name", path.name[:3]))


def _colourize_rate(rate: np.ndarray) -> Image.Image:
    rgba = np.zeros((*rate.shape, 4), dtype=np.uint8)
    positive = np.isfinite(rate) & (rate >= RAIN_RATE_STOPS[0])
    if np.any(positive):
        values = np.clip(rate[positive], RAIN_RATE_STOPS[0], RAIN_RATE_STOPS[-1])
        for channel in range(3):
            rgba[..., channel][positive] = np.interp(
                values,
                RAIN_RATE_STOPS,
                RAIN_RATE_COLOURS[:, channel],
            ).astype(np.uint8)
        rgba[..., 3][positive] = 255
    return Image.fromarray(rgba)


def _base_crop(source: Path, width: int, height: int) -> Image.Image:
    viewport = VIEWPORTS["south-coast"]
    with Image.open(source) as opened:
        rgba = opened.convert("RGBA")
    left = round(viewport["left"] * rgba.width)
    top = round(viewport["top"] * rgba.height)
    right = round((viewport["left"] + viewport["width"]) * rgba.width)
    bottom = round((viewport["top"] + viewport["height"]) * rgba.height)
    return rgba.crop((left, top, right, bottom)).resize(
        (width, height),
        Image.Resampling.NEAREST,
    )


def _write_png(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.png")
    image.save(temporary, format="PNG", optimize=True, compress_level=6)
    temporary.replace(destination)


def _prune_source_cache(cache_root: Path, now: dt.datetime) -> int:
    removed = 0
    if not cache_root.is_dir():
        return removed
    cutoff = now - dt.timedelta(days=NEXRAD_CACHE_DAYS)
    for path in cache_root.rglob("*_DPR_*"):
        item = parse_nexrad_object(path.name)
        if item is not None and item.valid_time < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def derive_south_coast_hybrid_radar(
    output_root: Path,
    *,
    hours: float = 3.0,
    latest_only: bool = False,
    source: NexradSource | None = None,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Render an ECCC/NOAA rain-rate mosaic aligned to the South Coast stage.

    ECCC's 1-km continental composite remains the complete base.  Positive DPR
    estimates from KATX and KLGX replace it inside their useful 230-km range;
    zeros never erase the ECCC field, which avoids artificial circular holes
    from beam blockage or a temporarily sparse U.S. product.
    """

    if hours <= 0:
        raise ValueError("hours must be positive")
    root = output_root.resolve()
    domain = DOMAINS["bc"]
    layer = LAYERS[SOUTH_COAST_LAYER_ID]
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    base_frames = _local_radar_frames(root)
    anchors = _anchor_times(base_frames, hours, latest_only, current)
    if not anchors:
        return {"status": "unavailable", "rendered": 0, "warnings": ["No local ECCC radar frames"]}

    warnings: list[str] = []
    owned_source = source is None
    client: NexradSource = source or NexradLevel3Client()
    try:
        try:
            objects = client.objects_between(
                NEXRAD_SITES,
                anchors[0] - NEXRAD_MAX_AGE,
                current if latest_only else anchors[-1],
            )
        except Exception as error:
            objects = []
            warnings.append(f"NOAA NEXRAD listing unavailable: {type(error).__name__}: {error}")

        if latest_only and objects:
            newest_nexrad = max(
                (
                    item.valid_time
                    for item in objects
                    if item.valid_time <= current
                    and current - item.valid_time <= NEXRAD_MAX_AGE
                ),
                default=anchors[-1],
            )
            # The live edge is allowed to pair a slightly older ECCC mosaic
            # with the newest U.S. volume.  Its valid time remains truthful and
            # historical/video anchors stay on the regular ten-minute clock.
            anchors = [max(anchors[-1], newest_nexrad)]

        selections: dict[dt.datetime, tuple[LocalRadarFrame, dict[str, NexradObject]]] = {}
        for anchor in anchors:
            base = _latest_frame(base_frames, anchor, ECCC_MAX_AGE)
            if base is None:
                continue
            site_objects = {
                site: item
                for site in NEXRAD_SITES
                if (item := _latest_object(objects, site, anchor)) is not None
            }
            selections[anchor] = (base, site_objects)

        cache_root = root.parent / "source-cache" / "nexrad-level3"
        unique = {
            item.key: item
            for _, site_objects in selections.values()
            for item in site_objects.values()
        }
        local_objects: dict[str, Path] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(unique)))) as executor:
            futures = {
                executor.submit(client.fetch, item, cache_root): item
                for item in unique.values()
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    local_objects[item.key] = future.result()
                except Exception as error:
                    warnings.append(
                        f"NOAA NEXRAD {item.key} unavailable: {type(error).__name__}: {error}"
                    )

        width = SOUTH_COAST_WIDTH
        height = _south_coast_height(domain)
        decoded: dict[str, tuple[np.ndarray, dt.datetime, str]] = {}
        rendered = 0
        skipped = 0
        for anchor, (base, site_objects) in sorted(selections.items()):
            available = {
                site: item
                for site, item in site_objects.items()
                if item.key in local_objects
            }
            source_keys = [available[site].key for site in sorted(available)]
            destination = frame_path(root, domain, layer, anchor)
            metadata = metadata_path(root, domain, layer, anchor)
            expected = {
                "renderVersion": SOUTH_COAST_RENDER_VERSION,
                "basePath": base.image_path.relative_to(root).as_posix(),
                "nexradObjects": source_keys,
                "regionalViewport": VIEWPORTS["south-coast"],
                "outputWidth": width,
                "outputHeight": height,
            }
            if destination.is_file() and metadata.is_file():
                try:
                    payload = json.loads(metadata.read_text())
                    if all(payload.get(key) == value for key, value in expected.items()):
                        skipped += 1
                        continue
                except (OSError, json.JSONDecodeError):
                    pass

            image = _base_crop(base.image_path, width, height)
            combined_rate = np.zeros((height, width), dtype=np.float32)
            source_times = {"ECCC GeoMet RADAR_1KM_RRAI": base.valid_time}
            decoded_sites: list[str] = []
            for site, item in sorted(available.items()):
                try:
                    if item.key not in decoded:
                        decoded[item.key] = _decode_dpr(local_objects[item.key])
                    rate, source_time, radar_name = decoded[item.key]
                    radial, gate, coverage = _sampling_map(site, width, height)
                    sampled = rate[radial, gate]
                    np.maximum(combined_rate, np.where(coverage, sampled, 0.0), out=combined_rate)
                    source_times[f"NOAA NEXRAD {radar_name} DPR"] = source_time
                    decoded_sites.append(site)
                except Exception as error:
                    warnings.append(
                        f"NOAA NEXRAD {item.key} decode unavailable: {type(error).__name__}: {error}"
                    )
            if np.any(combined_rate >= RAIN_RATE_STOPS[0]):
                image = Image.alpha_composite(image, _colourize_rate(combined_rate))
            _write_png(image, destination)
            write_metadata(
                root,
                domain,
                layer,
                anchor,
                destination,
                source_times,
                source="ECCC GeoMet + NOAA NEXRAD Level III",
                source_layer="RADAR_1KM_RRAI + KATX/KLGX DPR",
                extra={**expected, "nexradSites": decoded_sites},
            )
            rendered += 1
        removed_cache = _prune_source_cache(cache_root, current)
    finally:
        if owned_source and isinstance(client, NexradLevel3Client):
            client.close()

    return {
        "status": "warning" if warnings else "ok",
        "rendered": rendered,
        "skipped": skipped,
        "anchors": len(selections),
        "latest": format_utc(max(selections)) if selections else None,
        "nexradObjects": len(unique),
        "cacheObjectsRemoved": removed_cache,
        "warnings": warnings,
    }
