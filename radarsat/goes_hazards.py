from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import h5netcdf
import numpy as np
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject

from .config import Domain
from .geomet import projected_bbox
from .raw_satellite import GOES_BUCKETS, GOES_FILENAME, PublicObject, PublicSatelliteClient


UTC = dt.timezone.utc
ADP_PRODUCT = "ABI-L2-ADPF"
GLM_PRODUCT = "GLM-L2-LCFA"
GLM_MAXIMUM_LATITUDE = 52.0
EXPECTED_GLM_FILES_PER_WINDOW = 30


def ten_minute_clock(value: dt.datetime) -> dt.datetime:
    value = value.astimezone(UTC)
    return value.replace(minute=(value.minute // 10) * 10, second=0, microsecond=0)


@dataclass(frozen=True)
class GLMWindow:
    start_time: dt.datetime
    objects: tuple[PublicObject, ...]

    @property
    def end_time(self) -> dt.datetime:
        return self.start_time + dt.timedelta(minutes=10)


@dataclass(frozen=True)
class SmokeProduct:
    # 255 means unavailable, 0 means no medium/high-confidence detection,
    # 1 means medium confidence and 2 means high confidence.
    classes: np.ndarray
    transform: Affine
    crs: CRS
    start_time: dt.datetime
    end_time: dt.datetime


@dataclass(frozen=True)
class GLMFlashes:
    latitudes: np.ndarray
    longitudes: np.ndarray
    observed_count: int
    good_count: int
    observation_epochs: np.ndarray | None = None


class GoesHazardClient(PublicSatelliteClient):
    """Bounded discovery for GOES-18 ADP and raw GLM LCFA products."""

    def _objects(
        self,
        product: str,
        now: dt.datetime,
        *,
        satellite: str = "G18",
        lookback_hours: int = 3,
    ) -> list[PublicObject]:
        bucket = GOES_BUCKETS[satellite]
        current = now.astimezone(UTC)
        objects: dict[tuple[str, dt.datetime], PublicObject] = {}
        for hour_offset in range(lookback_hours):
            hour = current.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=hour_offset)
            prefix = f"{product}/{hour:%Y}/{hour:%j}/{hour:%H}/"
            for key, size in self.list_prefix(bucket, prefix):
                match = GOES_FILENAME.search(key)
                if not match:
                    continue
                valid_time = dt.datetime.strptime(
                    f"{match.group('year')}{match.group('day')}{match.group('hour')}"
                    f"{match.group('minute')}{match.group('second')}",
                    "%Y%j%H%M%S",
                ).replace(tzinfo=UTC)
                if valid_time > current:
                    continue
                item = PublicObject(bucket, key, size, valid_time)
                objects[(key, valid_time)] = item
        return sorted(objects.values(), key=lambda item: (item.valid_time, item.key))

    def latest_adp(self, now: dt.datetime | None = None) -> PublicObject:
        current = (now or dt.datetime.now(UTC)).astimezone(UTC)
        objects = self._objects(ADP_PRODUCT, current)
        if not objects:
            raise RuntimeError("No completed GOES-18 ABI ADP full-disk scan was found")
        return max(objects, key=lambda item: item.valid_time)

    def latest_complete_glm_window(self, now: dt.datetime | None = None) -> GLMWindow:
        current = (now or dt.datetime.now(UTC)).astimezone(UTC)
        objects = self._objects(GLM_PRODUCT, current)
        grouped: dict[dt.datetime, dict[dt.datetime, PublicObject]] = {}
        for item in objects:
            grouped.setdefault(ten_minute_clock(item.valid_time), {})[item.valid_time] = item
        for start_time in sorted(grouped, reverse=True):
            expected = tuple(
                start_time + dt.timedelta(seconds=20 * index)
                for index in range(EXPECTED_GLM_FILES_PER_WINDOW)
            )
            by_time = grouped[start_time]
            if all(value in by_time for value in expected):
                return GLMWindow(start_time, tuple(by_time[value] for value in expected))
        raise RuntimeError("No complete 30-file GOES-18 GLM ten-minute window was found")


def classify_smoke(
    smoke: np.ndarray,
    dqf: np.ndarray,
    cloud: np.ndarray | None = None,
    pqi2: np.ndarray | None = None,
) -> np.ndarray:
    """Classify current Enterprise ADP smoke without treating missing data as clear."""
    smoke_values = np.asarray(smoke)
    quality = np.asarray(dqf, dtype=np.uint16)
    if smoke_values.shape != quality.shape:
        raise ValueError("Smoke and DQF arrays do not share a grid")
    confidence = quality & 0x0C
    # The Enterprise ADP confidence field uses 0 for high and 4 for medium.
    # Low (8) and bad/unavailable (12) pixels must stay unavailable rather
    # than being presented as a confident "no smoke" observation.
    valid = (confidence == 0x00) | (confidence == 0x04)
    if cloud is not None:
        cloud_values = np.asarray(cloud)
        if cloud_values.shape != smoke_values.shape:
            raise ValueError("Cloud and Smoke arrays do not share a grid")
        valid &= cloud_values == 0
    if pqi2 is not None:
        pqi2_values = np.asarray(pqi2, dtype=np.uint16)
        if pqi2_values.shape != smoke_values.shape:
            raise ValueError("PQI2 and Smoke arrays do not share a grid")
        # Enterprise PQI2 bit 3 is the day/night diagnostic (0 day, 1 night).
        valid &= (pqi2_values & 0x08) == 0

    classes = np.full(smoke_values.shape, 255, dtype=np.uint8)
    classes[valid] = 0
    detected = valid & (smoke_values == 1)
    classes[detected & (confidence == 0x04)] = 1
    classes[detected & (confidence == 0x00)] = 2
    return classes


def _decoded_coordinate(variable: h5netcdf.Variable) -> np.ndarray:
    values = np.asarray(variable[:], dtype=np.float64)
    scale = float(variable.attrs.get("scale_factor", 1.0))
    offset = float(variable.attrs.get("add_offset", 0.0))
    return values * scale + offset


def _parse_iso_time(value: object) -> dt.datetime:
    text = str(value)
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def decode_smoke_product(path: Path) -> SmokeProduct:
    """Read the small set of ADPF fields needed for a confidence overlay."""
    with h5netcdf.File(path, "r") as dataset:
        required = {"Smoke", "DQF", "x", "y", "goes_imager_projection"}
        missing = required.difference(dataset.variables)
        if missing:
            raise ValueError(f"ADPF file is missing variables: {sorted(missing)}")
        smoke = np.asarray(dataset.variables["Smoke"][:])
        dqf = np.asarray(dataset.variables["DQF"][:])
        cloud_variable = dataset.variables.get("Cloud")
        pqi2_variable = dataset.variables.get("PQI2")
        cloud = np.asarray(cloud_variable[:]) if cloud_variable is not None else None
        pqi2 = np.asarray(pqi2_variable[:]) if pqi2_variable is not None else None
        classes = classify_smoke(smoke, dqf, cloud, pqi2)

        x = _decoded_coordinate(dataset.variables["x"])
        y = _decoded_coordinate(dataset.variables["y"])
        if len(x) != classes.shape[1] or len(y) != classes.shape[0] or len(x) < 2 or len(y) < 2:
            raise ValueError("ADPF coordinates do not match the Smoke grid")
        projection = dataset.variables["goes_imager_projection"].attrs
        height = float(projection["perspective_point_height"])
        sweep = projection.get("sweep_angle_axis", "x")
        if isinstance(sweep, (bytes, np.bytes_)):
            sweep = sweep.decode("ascii")
        source_crs = CRS.from_proj4(
            "+proj=geos "
            f"+h={height} "
            f"+lon_0={float(projection['longitude_of_projection_origin'])} "
            f"+a={float(projection['semi_major_axis'])} "
            f"+b={float(projection['semi_minor_axis'])} "
            f"+sweep={sweep} +units=m +no_defs"
        )
        x_metres = x * height
        y_metres = y * height
        if x_metres[0] > x_metres[-1]:
            x_metres = x_metres[::-1]
            classes = classes[:, ::-1]
        if y_metres[0] < y_metres[-1]:
            y_metres = y_metres[::-1]
            classes = classes[::-1, :]
        dx = float(np.median(np.abs(np.diff(x_metres))))
        dy = float(np.median(np.abs(np.diff(y_metres))))
        transform = from_bounds(
            float(x_metres.min() - dx / 2),
            float(y_metres.min() - dy / 2),
            float(x_metres.max() + dx / 2),
            float(y_metres.max() + dy / 2),
            len(x_metres),
            len(y_metres),
        )
        start_time = _parse_iso_time(dataset.attrs["time_coverage_start"])
        end_time = _parse_iso_time(dataset.attrs["time_coverage_end"])
    return SmokeProduct(classes, transform, source_crs, start_time, end_time)


def render_smoke_overlay(product: SmokeProduct, domain: Domain, destination: Path) -> dict[str, object]:
    target = np.full((domain.height, domain.width), 255, dtype=np.uint8)
    reproject(
        source=product.classes,
        destination=target,
        src_transform=product.transform,
        src_crs=product.crs,
        src_nodata=255,
        dst_transform=from_bounds(*projected_bbox(domain), domain.width, domain.height),
        dst_crs=domain.crs,
        dst_nodata=255,
        resampling=Resampling.nearest,
        num_threads=2,
    )
    rgba = np.zeros((domain.height, domain.width, 4), dtype=np.uint8)
    medium = target == 1
    high = target == 2
    # Pale neutral smoke tint: enough to reveal a plume while leaving the
    # underlying true-colour texture visible. Confidence changes opacity, not
    # implied aerosol concentration.
    rgba[medium] = np.array((188, 204, 205, 92), dtype=np.uint8)
    rgba[high] = np.array((244, 220, 174, 166), dtype=np.uint8)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        Image.fromarray(rgba).save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    valid_pixels = int(np.count_nonzero(target != 255))
    return {
        "availability": "daylight" if valid_pixels else "unavailable",
        "validPixelCount": valid_pixels,
        "mediumConfidencePixels": int(np.count_nonzero(medium)),
        "highConfidencePixels": int(np.count_nonzero(high)),
    }


def read_glm_flashes(
    path: Path,
    observation_time: dt.datetime | None = None,
) -> GLMFlashes:
    with h5netcdf.File(path, "r") as dataset:
        required = {"flash_lat", "flash_lon", "flash_quality_flag"}
        missing = required.difference(dataset.variables)
        if missing:
            raise ValueError(f"GLM file is missing variables: {sorted(missing)}")
        latitudes = np.asarray(dataset.variables["flash_lat"][:], dtype=np.float64)
        longitudes = np.asarray(dataset.variables["flash_lon"][:], dtype=np.float64)
        quality = np.asarray(dataset.variables["flash_quality_flag"][:])
        if observation_time is None:
            start_value = dataset.attrs.get("time_coverage_start")
            end_value = dataset.attrs.get("time_coverage_end")
            if start_value is not None and end_value is not None:
                start = _parse_iso_time(start_value)
                end = _parse_iso_time(end_value)
                observation_time = start + (end - start) / 2
    if latitudes.shape != longitudes.shape or latitudes.shape != quality.shape:
        raise ValueError("GLM flash coordinates and quality flags do not share a shape")
    observed_count = int(latitudes.size)
    good = (quality == 0) & np.isfinite(latitudes) & np.isfinite(longitudes)
    good_count = int(np.count_nonzero(good))
    observation_epochs = (
        np.full(good_count, observation_time.astimezone(UTC).timestamp(), dtype=np.float64)
        if observation_time is not None
        else None
    )
    return GLMFlashes(
        latitudes[good],
        longitudes[good],
        observed_count,
        good_count,
        observation_epochs,
    )


def combine_glm_flashes(values: Iterable[GLMFlashes]) -> GLMFlashes:
    items = list(values)
    if not items:
        return GLMFlashes(np.empty(0), np.empty(0), 0, 0)
    latitudes = np.concatenate([item.latitudes for item in items])
    longitudes = np.concatenate([item.longitudes for item in items])
    epochs = (
        np.concatenate([item.observation_epochs for item in items if item.observation_epochs is not None])
        if all(item.observation_epochs is not None for item in items)
        else None
    )
    return GLMFlashes(
        latitudes,
        longitudes,
        sum(item.observed_count for item in items),
        sum(item.good_count for item in items),
        epochs,
    )


def render_glm_bins(
    flashes: GLMFlashes,
    domain: Domain,
    destination: Path,
    *,
    maximum_latitude: float = GLM_MAXIMUM_LATITUDE,
) -> dict[str, object]:
    latitude_mask = flashes.latitudes <= maximum_latitude
    latitudes = flashes.latitudes[latitude_mask]
    longitudes = flashes.longitudes[latitude_mask]
    transformer = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True, force_over=True)
    if len(longitudes):
        xs, ys = transformer.transform(longitudes.tolist(), latitudes.tolist())
        xs = np.asarray(xs, dtype=np.float64)
        ys = np.asarray(ys, dtype=np.float64)
    else:
        xs = np.empty(0, dtype=np.float64)
        ys = np.empty(0, dtype=np.float64)
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    inside = (
        np.isfinite(xs)
        & np.isfinite(ys)
        & (xs >= xmin)
        & (xs < xmax)
        & (ys >= ymin)
        & (ys < ymax)
    )
    xs = xs[inside]
    ys = ys[inside]
    columns = np.floor((xs - xmin) / (xmax - xmin) * domain.width).astype(np.int64)
    rows = np.floor((ymax - ys) / (ymax - ymin) * domain.height).astype(np.int64)

    # Do not imply sub-sensor precision on high-resolution regional grids.
    # Collapse flash centroids to approximately ten-kilometre display bins.
    pixel_size = max((xmax - xmin) / domain.width, (ymax - ymin) / domain.height)
    bin_pixels = max(1, int(round(10_000 / pixel_size)))
    if len(columns):
        columns = np.clip((columns // bin_pixels) * bin_pixels + bin_pixels // 2, 0, domain.width - 1)
        rows = np.clip((rows // bin_pixels) * bin_pixels + bin_pixels // 2, 0, domain.height - 1)
        markers = np.unique(np.stack((rows, columns), axis=1), axis=0)
    else:
        markers = np.empty((0, 2), dtype=np.int64)

    rgba = np.zeros((domain.height, domain.width, 4), dtype=np.uint8)
    if len(markers):
        rgba[markers[:, 0], markers[:, 1]] = np.array((255, 255, 255, 255), dtype=np.uint8)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        Image.fromarray(rgba).save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "observedFlashCount": flashes.observed_count,
        "qualityControlledFlashCount": flashes.good_count,
        "mappedFlashCount": int(np.count_nonzero(inside)),
        "markerCount": int(len(markers)),
        "maximumLatitude": maximum_latitude,
        "binSizeMetres": int(round(bin_pixels * pixel_size)),
    }
