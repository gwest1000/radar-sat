from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")

import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject
from scipy.ndimage import gaussian_filter, maximum_filter, minimum_filter

from .config import DOMAINS, LAYERS, Domain, Layer
from .geomet import projected_bbox
from .pipeline import frame_path, metadata_path, write_metadata


UTC = dt.timezone.utc
RUN_RE = re.compile(r"^\d{8}T(?:00|06|12|18)Z$")
BASE_URL = "https://dd.weather.gc.ca/today/model_hrdps/continental/2.5km"
GRID_TAG = "RLatLon0.0225"
RENDER_VERSION = 2
DEFAULT_DATA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fcstGraphics"
    / "data"
    / "hrdps_continental"
)


@dataclass(frozen=True)
class FieldStyle:
    kind: str
    layer_id: str
    variable: str
    level_tag: str
    units: str
    scale: float
    levels: np.ndarray
    threshold: float
    lower_colour: str
    upper_colour: str
    linewidth: float
    label_size: float
    contour_smooth_km: float
    smooth_km: float
    background_km: float
    search_radius_km: float
    prominence: float
    centre_weight: str
    centre_size: float


FIELD_STYLES = (
    FieldStyle(
        kind="hgt500",
        layer_id="hrdps-hgt500",
        variable="HGT",
        level_tag="ISBL_0500",
        units="dam",
        scale=0.1,
        levels=np.arange(450.0, 660.1, 6.0),
        threshold=570.0,
        lower_colour="#c98735",
        upper_colour="#b95750",
        linewidth=3.7625,
        label_size=9.5,
        contour_smooth_km=18.0,
        smooth_km=90.0,
        background_km=450.0,
        search_radius_km=475.0,
        prominence=3.0,
        centre_weight="bold",
        centre_size=15.0,
    ),
    FieldStyle(
        kind="mslp",
        layer_id="hrdps-mslp",
        variable="PRMSL",
        level_tag="MSL",
        units="hPa",
        scale=0.01,
        levels=np.arange(880.0, 1080.1, 4.0),
        threshold=1024.0,
        lower_colour="#ef4dff",
        upper_colour="#42a5ff",
        linewidth=0.7875,
        label_size=8.5,
        contour_smooth_km=55.0,
        smooth_km=65.0,
        background_km=325.0,
        search_radius_km=350.0,
        prominence=1.8,
        centre_weight="normal",
        centre_size=11.5,
    ),
)


@dataclass(frozen=True)
class Centre:
    kind: str
    x: float
    y: float
    value: float
    prominence: float


def parse_run_stamp(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%dT%HZ").replace(tzinfo=UTC)


def model_filename(stamp: str, fhour: int, style: FieldStyle) -> str:
    return (
        f"{stamp}_MSC_HRDPS_{style.variable}_{style.level_tag}_"
        f"{GRID_TAG}_PT{fhour:03d}H.grib2"
    )


def model_path(data_root: Path, stamp: str, fhour: int, style: FieldStyle) -> Path:
    return data_root / stamp / f"{fhour:03d}" / model_filename(stamp, fhour, style)


def available_runs(data_root: Path, valid_time: dt.datetime) -> list[tuple[str, dt.datetime, int]]:
    candidates: list[tuple[str, dt.datetime, int]] = []
    if not data_root.exists():
        return candidates
    for path in data_root.iterdir():
        if not path.is_dir() or not RUN_RE.fullmatch(path.name):
            continue
        init_time = parse_run_stamp(path.name)
        lead = int((valid_time - init_time).total_seconds() // 3600)
        if 0 <= lead <= 48 and init_time <= valid_time:
            candidates.append((path.name, init_time, lead))
    return sorted(candidates, key=lambda item: item[1], reverse=True)


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
    try:
        for attempt in range(3):
            try:
                with requests.get(url, timeout=90, stream=True) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                handle.write(chunk)
                if temporary.stat().st_size < 1_000:
                    raise RuntimeError(f"Downloaded HRDPS file is unexpectedly small: {url}")
                temporary.replace(destination)
                return
            except requests.HTTPError as exc:
                temporary.unlink(missing_ok=True)
                if exc.response is not None and exc.response.status_code == 404:
                    raise
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
            except Exception:
                temporary.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_fields(
    data_root: Path,
    stamp: str,
    fhour: int,
    *,
    download: bool,
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    cycle = stamp[9:11]
    for style in FIELD_STYLES:
        path = model_path(data_root, stamp, fhour, style)
        if not path.is_file() and download:
            filename = model_filename(stamp, fhour, style)
            _download(f"{BASE_URL}/{cycle}/{fhour:03d}/{filename}", path)
        if not path.is_file():
            raise FileNotFoundError(path)
        paths[style.layer_id] = path
    return paths


def _smooth_nan(values: np.ndarray, sigma: float) -> np.ndarray:
    valid = np.isfinite(values)
    if not np.any(valid):
        return np.full(values.shape, np.nan, dtype=np.float32)
    numerator = gaussian_filter(np.where(valid, values, 0.0), sigma=sigma, mode="nearest")
    denominator = gaussian_filter(valid.astype(np.float32), sigma=sigma, mode="nearest")
    result = numerator / np.where(denominator > 1e-4, denominator, np.nan)
    return result.astype(np.float32)


def _pixel_km(domain: Domain, width: int, height: int) -> float:
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    scale = min(abs(xmax - xmin) / width, abs(ymax - ymin) / height) / 1000.0
    if domain.crs in {"EPSG:3857", "EPSG:3832"}:
        scale *= max(0.25, math.cos(math.radians((domain.south + domain.north) / 2.0)))
    return max(0.25, scale)


def significant_centres(
    values: np.ndarray,
    pixel_km: float,
    *,
    smooth_km: float,
    background_km: float,
    search_radius_km: float,
    prominence: float,
    max_each: int = 7,
) -> list[Centre]:
    """Find conservative synoptic centres using smoothed neighborhood extrema.

    This follows the neighborhood-peak method used by MetPy's high/low example,
    with broad-background prominence and physical-distance suppression added to
    reject shallow gridscale extrema.
    """

    stride = max(1, int(round(18.0 / pixel_km)))
    coarse = values[::stride, ::stride].astype(np.float32)
    coarse_km = pixel_km * stride
    smooth = _smooth_nan(coarse, max(0.6, smooth_km / coarse_km))
    background = _smooth_nan(coarse, max(1.0, background_km / coarse_km))
    anomaly = smooth - background
    finite = np.isfinite(smooth) & np.isfinite(background)
    radius_px = max(2, int(round(search_radius_km / coarse_km)))
    window = radius_px * 2 + 1
    high_filter = maximum_filter(np.where(finite, smooth, -np.inf), size=window, mode="nearest")
    low_filter = minimum_filter(np.where(finite, smooth, np.inf), size=window, mode="nearest")
    interior = np.zeros(coarse.shape, dtype=bool)
    edge = max(2, radius_px // 3)
    if coarse.shape[0] > edge * 2 and coarse.shape[1] > edge * 2:
        interior[edge:-edge, edge:-edge] = True

    candidates: list[Centre] = []
    for kind, mask in (
        ("H", finite & interior & np.isclose(smooth, high_filter, atol=0.02) & (anomaly >= prominence)),
        ("L", finite & interior & np.isclose(smooth, low_filter, atol=0.02) & (anomaly <= -prominence)),
    ):
        rows, cols = np.where(mask)
        ranked = sorted(
            zip(rows, cols, strict=True),
            key=lambda point: abs(float(anomaly[point])),
            reverse=True,
        )
        accepted: list[Centre] = []
        for row, col in ranked:
            x = float(col * stride)
            y = float(row * stride)
            if any(math.hypot(x - item.x, y - item.y) * pixel_km < search_radius_km for item in accepted):
                continue
            accepted.append(
                Centre(
                    kind=kind,
                    x=x,
                    y=y,
                    value=float(smooth[row, col]),
                    prominence=abs(float(anomaly[row, col])),
                )
            )
            if len(accepted) >= max_each:
                break
        candidates.extend(accepted)
    return candidates


def reproject_field(path: Path, domain: Domain) -> np.ndarray:
    target_width = domain.width
    target_height = domain.height
    destination = np.full((target_height, target_width), np.nan, dtype=np.float32)
    with rasterio.open(path) as source:
        values = source.read(1).astype(np.float32)
        values[np.abs(values) > 1e20] = np.nan
        reproject(
            source=values,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=from_bounds(*projected_bbox(domain), target_width, target_height),
            dst_crs=domain.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return destination


def _colour(style: FieldStyle, value: float) -> str:
    return style.lower_colour if value <= style.threshold else style.upper_colour


def _segment_midpoint(segment: np.ndarray) -> tuple[float, float] | None:
    """Return the arc-length midpoint of one disconnected contour line."""
    if segment.ndim != 2 or segment.shape[0] < 2:
        return None
    lengths = np.hypot(np.diff(segment[:, 0]), np.diff(segment[:, 1]))
    total = float(np.sum(lengths))
    if not math.isfinite(total) or total <= 0:
        return None
    target = total / 2.0
    cumulative = np.cumsum(lengths)
    index = int(np.searchsorted(cumulative, target, side="left"))
    before = float(cumulative[index - 1]) if index else 0.0
    fraction = (target - before) / max(float(lengths[index]), 1e-9)
    point = segment[index] + fraction * (segment[index + 1] - segment[index])
    return float(point[0]), float(point[1])


def _label_every_contour(
    ax: plt.Axes,
    contours: object,
    levels: np.ndarray,
    style: FieldStyle,
    output_scale: int,
) -> list[object]:
    """Place one label on every disconnected line, not merely every level."""
    all_segments = getattr(contours, "allsegs", ())
    positions: list[tuple[float, float]] = []
    for index, level in enumerate(levels):
        if index >= len(all_segments):
            continue
        positions.extend(
            point
            for segment in all_segments[index]
            if (point := _segment_midpoint(np.asarray(segment))) is not None
        )
    if not positions:
        return []
    return ax.clabel(
        contours,
        levels=levels,
        fmt=lambda value: f"{int(round(value))}",
        inline=True,
        inline_spacing=4,
        fontsize=style.label_size * output_scale,
        colors=[_colour(style, float(level)) for level in levels],
        manual=positions,
    )


def render_contours(
    values: np.ndarray,
    domain: Domain,
    style: FieldStyle,
    destination: Path,
) -> dict[str, object]:
    pixel_km = _pixel_km(domain, domain.width, domain.height)
    scaled = _smooth_nan(
        values * style.scale,
        max(0.55, style.contour_smooth_km / pixel_km),
    )
    output_scale = 2 if domain.id == "bc" else 1
    output_width = domain.width * output_scale
    output_height = domain.height * output_scale
    dpi = 100
    fig = plt.figure(figsize=(output_width / dpi, output_height / dpi), dpi=dpi)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.set_xlim(0, domain.width - 1)
    ax.set_ylim(domain.height - 1, 0)
    finite = scaled[np.isfinite(scaled)]
    if finite.size:
        levels = style.levels[(style.levels >= np.nanmin(finite)) & (style.levels <= np.nanmax(finite))]
    else:
        levels = np.asarray([], dtype=float)
    if levels.size:
        colours = [_colour(style, float(level)) for level in levels]
        contours = ax.contour(
            np.arange(domain.width),
            np.arange(domain.height),
            scaled,
            levels=levels,
            colors=colours,
            linewidths=style.linewidth * output_scale,
            antialiased=True,
        )
        contours.set_path_effects([
            path_effects.Stroke(linewidth=(style.linewidth + 1.15) * output_scale, foreground="#151822", alpha=0.82),
            path_effects.Normal(),
        ])
        labels = _label_every_contour(ax, contours, levels, style, output_scale)
        for label in labels:
            label.set_path_effects([
                path_effects.Stroke(linewidth=2.2 * output_scale, foreground="#151822", alpha=0.92),
                path_effects.Normal(),
            ])

    centres = significant_centres(
        scaled,
        pixel_km,
        smooth_km=style.smooth_km,
        background_km=style.background_km,
        search_radius_km=style.search_radius_km,
        prominence=style.prominence,
    )
    for centre in centres:
        colour = _colour(style, centre.value)
        letter = ax.annotate(
            centre.kind,
            (centre.x, centre.y),
            xytext=(0, 0),
            textcoords="offset points",
            ha="center",
            va="center",
            color=colour,
            fontsize=style.centre_size * 2.0 * output_scale,
            fontweight=style.centre_weight,
            zorder=20,
        )
        magnitude = ax.annotate(
            f"{int(round(centre.value))}",
            (centre.x, centre.y),
            xytext=(0, -1.42 * style.centre_size * output_scale),
            textcoords="offset points",
            ha="center",
            va="center",
            color=colour,
            fontsize=style.centre_size * output_scale,
            fontweight=style.centre_weight,
            zorder=20,
        )
        centre_effects = [
            path_effects.Stroke(
                linewidth=(3.0 if style.kind == "hgt500" else 2.2) * output_scale,
                foreground="#10131a",
                alpha=0.96,
            ),
            path_effects.Normal(),
        ]
        letter.set_path_effects(centre_effects)
        magnitude.set_path_effects(centre_effects)

    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=dpi, transparent=True, bbox_inches=None, pad_inches=0)
    plt.close(fig)
    with Image.open(destination) as image:
        image.save(destination, "PNG", optimize=True)
    return {
        "renderVersion": RENDER_VERSION,
        "outputWidth": output_width,
        "outputHeight": output_height,
        "centreCount": len(centres),
        "centres": [centre.__dict__ for centre in centres],
        "contourInterval": 6 if style.kind == "hgt500" else 4,
        "units": style.units,
    }


def render_valid_time(
    output_root: Path,
    data_root: Path,
    valid_time: dt.datetime,
    *,
    domain_ids: Iterable[str],
    download: bool = True,
) -> dict[str, object]:
    valid = valid_time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    errors: list[str] = []
    for stamp, init_time, fhour in available_runs(data_root, valid):
        try:
            paths = ensure_fields(data_root, stamp, fhour, download=download)
        except Exception as exc:
            errors.append(f"{stamp} F{fhour:03d}: {exc}")
            continue
        rendered: list[str] = []
        for domain_id in domain_ids:
            domain = DOMAINS[domain_id]
            for style in FIELD_STYLES:
                layer = LAYERS[style.layer_id]
                destination = frame_path(output_root, domain, layer, valid)
                metadata = metadata_path(output_root, domain, layer, valid)
                if destination.is_file() and metadata.is_file():
                    try:
                        existing = json.loads(metadata.read_text())
                        if (
                            existing.get("modelInitTime") == init_time.isoformat().replace("+00:00", "Z")
                            and existing.get("forecastHour") == fhour
                            and existing.get("renderVersion") == RENDER_VERSION
                        ):
                            continue
                    except (OSError, json.JSONDecodeError):
                        pass
                values = reproject_field(paths[style.layer_id], domain)
                summary = render_contours(values, domain, style, destination)
                write_metadata(
                    output_root,
                    domain,
                    layer,
                    valid,
                    destination,
                    {f"HRDPS {stamp} F{fhour:03d}": valid},
                    source="ECCC HRDPS Continental 2.5 km",
                    source_layer=f"{style.variable} {style.level_tag}",
                    extra={
                        **summary,
                        "modelInitTime": init_time.isoformat().replace("+00:00", "Z"),
                        "forecastHour": fhour,
                    },
                )
                rendered.append(f"{domain_id}/{style.layer_id}")
        return {
            "status": "rendered" if rendered else "unchanged",
            "validTime": valid.isoformat().replace("+00:00", "Z"),
            "modelRun": stamp,
            "forecastHour": fhour,
            "rendered": rendered,
            "errors": errors,
        }
    return {
        "status": "unavailable",
        "validTime": valid.isoformat().replace("+00:00", "Z"),
        "errors": errors or ["No local HRDPS run covers this valid time."],
    }


def update_recent(
    output_root: Path,
    data_root: Path,
    *,
    hours: int = 12,
    domain_ids: Iterable[str] = ("bc",),
    now: dt.datetime | None = None,
    download: bool = True,
) -> list[dict[str, object]]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return [
        render_valid_time(
            output_root,
            data_root,
            current - dt.timedelta(hours=offset),
            domain_ids=domain_ids,
            download=download,
        )
        for offset in range(hours, -1, -1)
    ]
