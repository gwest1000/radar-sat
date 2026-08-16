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

from .config import DOMAINS, LAYERS, VIEWPORTS, Domain, Layer, regional_layer_id
from .geomet import projected_bbox
from .pipeline import frame_path, metadata_path, write_metadata
from .paths import sibling_project_path


UTC = dt.timezone.utc
RUN_RE = re.compile(r"^\d{8}T(?:00|06|12|18)Z$")
BASE_URL = "https://dd.weather.gc.ca/today/model_hrdps/continental/2.5km"
GRID_TAG = "RLatLon0.0225"
RENDER_VERSION = 4
DEFAULT_DATA_ROOT = Path(
    os.environ.get(
        "RADARSAT_HRDPS_DATA_ROOT",
        sibling_project_path("fcstGraphics", "data", "hrdps_continental"),
    )
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
    text_scale: float = 1.0,
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
        fontsize=style.label_size * output_scale * text_scale,
        colors=[_colour(style, float(level)) for level in levels],
        manual=positions,
    )


def render_contours(
    values: np.ndarray,
    domain: Domain,
    style: FieldStyle,
    destination: Path,
    *,
    line_scale_override: float | None = None,
    label_scale_override: float | None = None,
    centre_scale_override: float | None = None,
) -> dict[str, object]:
    pixel_km = _pixel_km(domain, domain.width, domain.height)
    scaled = _smooth_nan(
        values * style.scale,
        max(0.55, style.contour_smooth_km / pixel_km),
    )
    ecmwf_overview = style.layer_id.startswith("ecmwf-")
    output_scale = 2
    line_scale = 1.0
    if ecmwf_overview:
        line_scale = 0.5625 if style.kind == "hgt500" else 0.90
    if line_scale_override is not None:
        line_scale = line_scale_override
    label_scale = 0.45 if ecmwf_overview else 1.0
    if label_scale_override is not None:
        label_scale = label_scale_override
    centre_scale = 0.50 if ecmwf_overview else 1.0
    if centre_scale_override is not None:
        centre_scale = centre_scale_override
    rendered_linewidth = style.linewidth * line_scale
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
            linewidths=rendered_linewidth * output_scale,
            antialiased=True,
        )
        labels = _label_every_contour(
            ax,
            contours,
            levels,
            style,
            output_scale,
            label_scale,
        )
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
            fontsize=style.centre_size * 2.0 * output_scale * centre_scale,
            fontweight=style.centre_weight,
            zorder=20,
        )
        magnitude = ax.annotate(
            f"{int(round(centre.value))}",
            (centre.x, centre.y),
            xytext=(0, -1.42 * style.centre_size * output_scale * centre_scale),
            textcoords="offset points",
            ha="center",
            va="center",
            color=colour,
            fontsize=style.centre_size * output_scale * centre_scale,
            fontweight=style.centre_weight,
            zorder=20,
        )
        centre_effects = [
            path_effects.Stroke(
                linewidth=(3.0 if style.kind == "hgt500" else 2.2) * output_scale * centre_scale,
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
        "lineWidth": rendered_linewidth,
        "labelScale": label_scale,
        "centreScale": centre_scale,
        "units": style.units,
    }


def crop_field_to_viewport(
    values: np.ndarray,
    domain: Domain,
    viewport: dict[str, float],
    region_id: str,
) -> tuple[np.ndarray, Domain]:
    """Crop an aligned field and retain exact projected bounds for rendering."""
    x0 = max(0, min(domain.width - 1, round(viewport["left"] * domain.width)))
    y0 = max(0, min(domain.height - 1, round(viewport["top"] * domain.height)))
    x1 = max(
        x0 + 1,
        min(domain.width, round((viewport["left"] + viewport["width"]) * domain.width)),
    )
    y1 = max(
        y0 + 1,
        min(domain.height, round((viewport["top"] + viewport["height"]) * domain.height)),
    )
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    x_span = xmax - xmin
    y_span = ymax - ymin
    region_bounds = (
        xmin + x0 / domain.width * x_span,
        ymax - y1 / domain.height * y_span,
        xmin + x1 / domain.width * x_span,
        ymax - y0 / domain.height * y_span,
    )
    cropped = values[y0:y1, x0:x1]
    return cropped, Domain(
        id=f"{domain.id}-region-{region_id}",
        title=f"{domain.title} · {region_id}",
        west=domain.west,
        south=domain.south,
        east=domain.east,
        north=domain.north,
        crs=domain.crs,
        width=x1 - x0,
        height=y1 - y0,
        tier=domain.tier,
        projected_bounds=region_bounds,
    )


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
                outputs: list[tuple[Layer, str | None]] = [
                    (LAYERS[style.layer_id], None)
                ]
                if domain.id == "bc":
                    outputs.extend(
                        (
                            LAYERS[regional_layer_id(style.layer_id, region_id)],
                            region_id,
                        )
                        for region_id in VIEWPORTS
                    )
                pending: list[tuple[Layer, str | None]] = []
                for output_layer, region_id in outputs:
                    destination = frame_path(output_root, domain, output_layer, valid)
                    metadata = metadata_path(output_root, domain, output_layer, valid)
                    current = False
                    if destination.is_file() and metadata.is_file():
                        try:
                            existing = json.loads(metadata.read_text())
                            current = (
                                existing.get("modelInitTime")
                                == init_time.isoformat().replace("+00:00", "Z")
                                and existing.get("forecastHour") == fhour
                                and existing.get("renderVersion") == RENDER_VERSION
                                and existing.get("regionalViewportId") == region_id
                                and existing.get("regionalViewport")
                                == (VIEWPORTS[region_id] if region_id is not None else None)
                            )
                        except (OSError, json.JSONDecodeError):
                            pass
                    if not current:
                        pending.append((output_layer, region_id))
                if not pending:
                    continue
                values = reproject_field(paths[style.layer_id], domain)
                for output_layer, region_id in pending:
                    destination = frame_path(output_root, domain, output_layer, valid)
                    render_values = values
                    render_domain = domain
                    line_scale_override: float | None = None
                    if region_id is not None:
                        render_values, render_domain = crop_field_to_viewport(
                            values,
                            domain,
                            VIEWPORTS[region_id],
                            region_id,
                        )
                        if style.kind == "hgt500":
                            line_scale_override = 0.80 if region_id == "small" else 0.50
                    summary = render_contours(
                        render_values,
                        render_domain,
                        style,
                        destination,
                        line_scale_override=line_scale_override,
                    )
                    write_metadata(
                        output_root,
                        domain,
                        output_layer,
                        valid,
                        destination,
                        {f"HRDPS {stamp} F{fhour:03d}": valid},
                        source="ECCC HRDPS Continental 2.5 km",
                        source_layer=f"{style.variable} {style.level_tag}",
                        extra={
                            **summary,
                            "modelInitTime": init_time.isoformat().replace("+00:00", "Z"),
                            "forecastHour": fhour,
                            "regionalViewportId": region_id,
                            "regionalViewport": (
                                VIEWPORTS[region_id]
                                if region_id is not None
                                else None
                            ),
                        },
                    )
                    rendered.append(f"{domain_id}/{output_layer.id}")
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
