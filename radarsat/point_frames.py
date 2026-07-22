from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image
from pyproj import Transformer

from .config import PRODUCTS, Domain
from .geomet import format_utc, projected_bbox


UTC = dt.timezone.utc
POINT_FRAME_SCHEMA_VERSION = 1
NORMALIZED_COORDINATE_SPACE = {
    "type": "normalized",
    "origin": "top-left",
    "xRange": [0, 1],
    "yRange": [0, 1],
}


def radarsat_product_uses_layer(domain_id: str, layer_id: str) -> bool:
    """Return whether the legacy Radar-Sat viewer still consumes a layer."""
    return any(
        product.get("domain") == domain_id
        and any(layer.get("id") == layer_id for layer in product.get("layers", []))
        for product in PRODUCTS
    )


def normalized_pixel(x: float, y: float, domain: Domain) -> tuple[float, float]:
    """Return stable top-left normalized coordinates for an aligned raster pixel."""
    x_denominator = max(1, domain.width - 1)
    y_denominator = max(1, domain.height - 1)
    return round(float(x) / x_denominator, 6), round(float(y) / y_denominator, 6)


def write_point_frame(
    destination: Path,
    *,
    layer: str,
    domain: Domain,
    valid_time: dt.datetime,
    window_start: dt.datetime,
    window_end: dt.datetime,
    age_reference_time: dt.datetime,
    point_schema: Sequence[str],
    points: Sequence[Sequence[float | int | None]],
    age_mode: str,
    age_precision_seconds: int,
) -> None:
    """Atomically write a compact, self-describing point-frame JSON asset."""
    payload = {
        "schemaVersion": POINT_FRAME_SCHEMA_VERSION,
        "type": "point-frame",
        "layer": layer,
        "domain": domain.id,
        "validTime": format_utc(valid_time),
        "window": {
            "start": format_utc(window_start),
            "end": format_utc(window_end),
        },
        "ageReferenceTime": format_utc(age_reference_time),
        "ageMode": age_mode,
        "agePrecisionSeconds": age_precision_seconds,
        "coordinateSpace": NORMALIZED_COORDINATE_SPACE,
        "pointSchema": list(point_schema),
        "points": [list(point) for point in points],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def glm_point_rows(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    observation_epochs: np.ndarray | None,
    domain: Domain,
    age_reference_time: dt.datetime,
    *,
    maximum_latitude: float = 52.0,
    bin_size_metres: float = 10_000.0,
    fallback_age_minutes: float = 5.0,
) -> tuple[list[list[float | int]], dict[str, object]]:
    """Project GLM flashes into compact roughly-10-km display-point bins.

    A bin retains the newest observation time and a flash count. Fresh ingest
    supplies a midpoint time for each 20-second LCFA file, making point ages
    accurate to about 20 seconds. A caller without those source times receives
    an explicitly labelled ten-minute midpoint estimate instead.
    """
    latitudes = np.asarray(latitudes, dtype=np.float64)
    longitudes = np.asarray(longitudes, dtype=np.float64)
    if latitudes.shape != longitudes.shape:
        raise ValueError("GLM latitude and longitude arrays do not share a shape")
    epochs: np.ndarray | None = None
    if observation_epochs is not None:
        epochs = np.asarray(observation_epochs, dtype=np.float64)
        if epochs.shape != latitudes.shape:
            raise ValueError("GLM observation times do not share the coordinate shape")

    latitude_mask = np.isfinite(latitudes) & np.isfinite(longitudes)
    latitude_mask &= latitudes <= maximum_latitude
    latitudes = latitudes[latitude_mask]
    longitudes = longitudes[latitude_mask]
    if epochs is not None:
        epochs = epochs[latitude_mask]

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
    if epochs is not None:
        epochs = epochs[inside]

    columns = np.floor((xs - xmin) / (xmax - xmin) * domain.width).astype(np.int64)
    rows = np.floor((ymax - ys) / (ymax - ymin) * domain.height).astype(np.int64)
    pixel_size = max((xmax - xmin) / domain.width, (ymax - ymin) / domain.height)
    bin_pixels = max(1, int(round(bin_size_metres / pixel_size)))
    if len(columns):
        columns = np.clip(
            (columns // bin_pixels) * bin_pixels + bin_pixels // 2,
            0,
            domain.width - 1,
        )
        rows = np.clip(
            (rows // bin_pixels) * bin_pixels + bin_pixels // 2,
            0,
            domain.height - 1,
        )

    reference_epoch = age_reference_time.astimezone(UTC).timestamp()
    bins: dict[tuple[int, int], tuple[int, float | None]] = {}
    for index, (row, column) in enumerate(zip(rows.tolist(), columns.tolist(), strict=True)):
        key = (row, column)
        epoch = float(epochs[index]) if epochs is not None and np.isfinite(epochs[index]) else None
        count, newest = bins.get(key, (0, None))
        if epoch is not None and (newest is None or epoch > newest):
            newest = epoch
        bins[key] = (count + 1, newest)

    points: list[list[float | int]] = []
    used_precise_ages = epochs is not None and all(newest is not None for _, newest in bins.values())
    for (row, column), (count, newest) in sorted(bins.items()):
        x, y = normalized_pixel(column, row, domain)
        age = (
            max(0.0, (reference_epoch - newest) / 60.0)
            if newest is not None
            else fallback_age_minutes
        )
        points.append([x, y, round(age, 3), count])

    return points, {
        "pointCount": len(points),
        "mappedFlashCount": int(np.count_nonzero(inside)),
        "maximumLatitude": maximum_latitude,
        "binSizeMetres": int(round(bin_pixels * pixel_size)),
        "pointSchema": ["x", "y", "ageMinutes", "count"],
        "coordinateSpace": "normalized-top-left",
        "ageMode": "source-file-midpoint" if used_precise_ages else "window-midpoint-estimate",
        "agePrecisionSeconds": 20 if used_precise_ages else 600,
    }


def _connected_centres(mask: np.ndarray) -> list[tuple[float, float]]:
    """Return centroids for four-connected True components without SciPy."""
    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    centres: list[tuple[float, float]] = []
    for start_y, start_x in np.argwhere(mask):
        if visited[start_y, start_x]:
            continue
        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        sum_x = 0
        sum_y = 0
        count = 0
        while stack:
            y, x = stack.pop()
            sum_x += x
            sum_y += y
            count += 1
            for neighbour_y, neighbour_x in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if (
                    0 <= neighbour_y < height
                    and 0 <= neighbour_x < width
                    and mask[neighbour_y, neighbour_x]
                    and not visited[neighbour_y, neighbour_x]
                ):
                    visited[neighbour_y, neighbour_x] = True
                    stack.append((neighbour_y, neighbour_x))
        centres.append((sum_x / count, sum_y / count))
    return centres


def points_from_glm_png(path: Path, domain: Domain) -> list[list[float | int]]:
    """Recover legacy GLM marker positions; intra-window age is irretrievable."""
    rgba = np.asarray(Image.open(path).convert("RGBA"))
    if rgba.shape[:2] != (domain.height, domain.width):
        raise ValueError(f"Legacy GLM raster dimensions do not match {domain.id}")
    points: list[list[float | int]] = []
    for y, x in np.argwhere(rgba[:, :, 3] > 0):
        normalized_x, normalized_y = normalized_pixel(int(x), int(y), domain)
        points.append([normalized_x, normalized_y, 5.0, 1])
    return points


HOTSPOT_AGE_COLOURS: tuple[tuple[tuple[int, int, int], float], ...] = (
    ((255, 229, 92), 180.0),
    ((255, 148, 31), 540.0),
    ((217, 75, 61), 1080.0),
)


def points_from_hotspot_png(path: Path, domain: Domain) -> list[list[float | int | None]]:
    """Recover legacy hotspot centres and colour-bucket midpoint ages."""
    rgba = np.asarray(Image.open(path).convert("RGBA"))
    if rgba.shape[:2] != (domain.height, domain.width):
        raise ValueError(f"Legacy hotspot raster dimensions do not match {domain.id}")
    points: list[list[float | int | None]] = []
    for colour, age_minutes in HOTSPOT_AGE_COLOURS:
        mask = np.all(rgba[:, :, :3] == colour, axis=2) & (rgba[:, :, 3] > 0)
        for x, y in _connected_centres(mask):
            normalized_x, normalized_y = normalized_pixel(x, y, domain)
            points.append([normalized_x, normalized_y, age_minutes, None, 1])
    points.sort(key=lambda point: (float(point[1]), float(point[0])))
    return points


def point_frame_metadata(
    *,
    points: Iterable[Sequence[object]],
    point_schema: Sequence[str],
    window_start: dt.datetime,
    window_end: dt.datetime,
    age_reference_time: dt.datetime,
    age_mode: str,
    age_precision_seconds: int,
    render_version: int,
    migration_source_path: str | None = None,
) -> dict[str, object]:
    values = list(points)
    metadata: dict[str, object] = {
        "pointFrameSchemaVersion": POINT_FRAME_SCHEMA_VERSION,
        "pointSchema": list(point_schema),
        "coordinateSpace": "normalized-top-left",
        "pointCount": len(values),
        "windowStart": format_utc(window_start),
        "windowEnd": format_utc(window_end),
        "ageReferenceTime": format_utc(age_reference_time),
        "ageMode": age_mode,
        "agePrecisionSeconds": age_precision_seconds,
        "renderVersion": render_version,
    }
    if migration_source_path is not None:
        metadata["migrationSourcePath"] = migration_source_path
    return metadata
