from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable

import numpy as np
from pyproj import Transformer
from scipy.interpolate import RegularGridInterpolator

from .config import DOMAINS, LAYERS, Domain
from .geomet import projected_bbox
from .hrdps_contours import FIELD_STYLES, RENDER_VERSION, UTC, FieldStyle, render_contours
from .pipeline import frame_path, metadata_path, write_metadata


DEFAULT_DATA_ROOT = Path("/Volumes/Greg1_2tb/concrete_fcst_data/raw/ecmwf/realtime")
MAX_FORECAST_HOUR = 252
FIELD_SPECS = (
    (replace(FIELD_STYLES[0], layer_id="ecmwf-hgt500", variable="gh", level_tag="ISBL_0500"), "pl_cf.grib2", "isobaricInhPa", 500),
    (replace(FIELD_STYLES[1], layer_id="ecmwf-mslp", variable="msl", level_tag="MSL"), "sfc_cf.grib2", "meanSea", 0),
)


def available_runs(data_root: Path, valid_time: dt.datetime) -> list[tuple[Path, dt.datetime, int]]:
    candidates: list[tuple[Path, dt.datetime, int]] = []
    if not data_root.exists():
        return candidates
    for date_path in data_root.iterdir():
        if not date_path.is_dir() or len(date_path.name) != 8 or not date_path.name.isdigit():
            continue
        for cycle_path in date_path.iterdir():
            if not cycle_path.is_dir() or cycle_path.name not in {"00", "06", "12", "18"}:
                continue
            try:
                init_time = dt.datetime.strptime(
                    f"{date_path.name}{cycle_path.name}", "%Y%m%d%H"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            lead_hours = (valid_time - init_time).total_seconds() / 3600.0
            lead = int(round(lead_hours))
            if (
                init_time <= valid_time
                and abs(lead_hours - lead) < 1e-6
                and 0 <= lead <= MAX_FORECAST_HOUR
                and lead % 3 == 0
                and (cycle_path / "pl_cf.grib2").is_file()
                and (cycle_path / "sfc_cf.grib2").is_file()
            ):
                candidates.append((cycle_path, init_time, lead))
    return sorted(candidates, key=lambda item: item[1], reverse=True)


def read_matching_field(
    path: Path,
    short_name: str,
    level_type: str,
    level: int,
    step: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Read one global regular-lat/lon message from an ECMWF GRIB archive."""
    try:
        from eccodes import (
            codes_get,
            codes_get_array,
            codes_grib_new_from_file,
            codes_release,
        )
    except ImportError as exc:  # pragma: no cover - actionable runtime guard
        raise RuntimeError("ECMWF contours require the Python eccodes package") from exc

    with path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                matches = (
                    str(codes_get(gid, "shortName")).lower() == short_name.lower()
                    and str(codes_get(gid, "typeOfLevel")) == level_type
                    and int(codes_get(gid, "level")) == level
                    and int(codes_get(gid, "step")) == step
                )
                if not matches:
                    continue
                nx = int(codes_get(gid, "Ni"))
                ny = int(codes_get(gid, "Nj"))
                values = codes_get_array(gid, "values").reshape(ny, nx).astype(np.float32)
                latitudes = codes_get_array(gid, "latitudes").reshape(ny, nx).astype(np.float32)
                longitudes = codes_get_array(gid, "longitudes").reshape(ny, nx).astype(np.float32)
                values[np.abs(values) > 1e20] = np.nan
                return values, latitudes[:, 0], longitudes[0, :]
            finally:
                codes_release(gid)
    raise KeyError(f"{short_name}:{level_type}:{level}:F{step:03d} not found in {path}")


def interpolate_global_field(
    values: np.ndarray,
    latitude_axis: np.ndarray,
    longitude_axis: np.ndarray,
    domain: Domain,
) -> np.ndarray:
    """Interpolate the cyclic ECMWF grid directly onto a display projection."""
    latitudes = np.asarray(latitude_axis, dtype=np.float64)
    longitudes = np.mod(np.asarray(longitude_axis, dtype=np.float64), 360.0)
    source = np.asarray(values, dtype=np.float32)
    if latitudes[0] > latitudes[-1]:
        latitudes = latitudes[::-1]
        source = source[::-1, :]
    order = np.argsort(longitudes)
    longitudes = longitudes[order]
    source = source[:, order]
    # Add a duplicate cyclic column so interpolation is continuous at 0/360.
    cyclic_longitudes = np.concatenate((longitudes, [longitudes[0] + 360.0]))
    cyclic_source = np.concatenate((source, source[:, :1]), axis=1)
    interpolator = RegularGridInterpolator(
        (latitudes, cyclic_longitudes),
        cyclic_source,
        method="linear",
        bounds_error=False,
        fill_value=np.nan,
    )

    xmin, ymin, xmax, ymax = projected_bbox(domain)
    xs = np.linspace(xmin, xmax, domain.width, endpoint=False) + (xmax - xmin) / (2 * domain.width)
    ys = np.linspace(ymax, ymin, domain.height, endpoint=False) - (ymax - ymin) / (2 * domain.height)
    target_x, target_y = np.meshgrid(xs, ys)
    transformer = Transformer.from_crs(domain.crs, "EPSG:4326", always_xy=True)
    target_lon, target_lat = transformer.transform(target_x, target_y)
    points = np.column_stack((target_lat.ravel(), np.mod(target_lon.ravel(), 360.0)))
    return interpolator(points).reshape(domain.height, domain.width).astype(np.float32)


def _is_current(
    output_root: Path,
    domain: Domain,
    style: FieldStyle,
    valid: dt.datetime,
    init_time: dt.datetime,
    forecast_hour: int,
) -> bool:
    layer = LAYERS[style.layer_id]
    destination = frame_path(output_root, domain, layer, valid)
    metadata = metadata_path(output_root, domain, layer, valid)
    if not destination.is_file() or not metadata.is_file():
        return False
    try:
        existing = json.loads(metadata.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        existing.get("modelInitTime") == init_time.isoformat().replace("+00:00", "Z")
        and existing.get("forecastHour") == forecast_hour
        and existing.get("renderVersion") == RENDER_VERSION
    )


def render_valid_time(
    output_root: Path,
    data_root: Path,
    valid_time: dt.datetime,
    *,
    domain_ids: Iterable[str] = ("north-america", "north-pacific"),
) -> dict[str, object]:
    valid = valid_time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if valid.hour % 3:
        raise ValueError("ECMWF control overlays must be rendered on three-hour valid times")
    errors: list[str] = []
    domains = [DOMAINS[domain_id] for domain_id in domain_ids]
    for cycle_root, init_time, forecast_hour in available_runs(data_root, valid):
        pending = {
            style.layer_id: [
                domain
                for domain in domains
                if not _is_current(output_root, domain, style, valid, init_time, forecast_hour)
            ]
            for style, _, _, _ in FIELD_SPECS
        }
        if not any(pending.values()):
            return {
                "status": "unchanged",
                "validTime": valid.isoformat().replace("+00:00", "Z"),
                "modelRun": init_time.isoformat().replace("+00:00", "Z"),
                "forecastHour": forecast_hour,
                "rendered": [],
                "errors": errors,
            }
        rendered: list[str] = []
        try:
            for style, filename, level_type, level in FIELD_SPECS:
                if not pending[style.layer_id]:
                    continue
                values, latitudes, longitudes = read_matching_field(
                    cycle_root / filename,
                    style.variable,
                    level_type,
                    level,
                    forecast_hour,
                )
                for domain in pending[style.layer_id]:
                    projected = interpolate_global_field(values, latitudes, longitudes, domain)
                    layer = LAYERS[style.layer_id]
                    destination = frame_path(output_root, domain, layer, valid)
                    summary = render_contours(projected, domain, style, destination)
                    write_metadata(
                        output_root,
                        domain,
                        layer,
                        valid,
                        destination,
                        {f"ECMWF {init_time:%Y%m%dT%HZ} F{forecast_hour:03d}": valid},
                        source="ECMWF IFS Control",
                        source_layer=f"{style.variable} {style.level_tag}",
                        extra={
                            **summary,
                            "modelInitTime": init_time.isoformat().replace("+00:00", "Z"),
                            "forecastHour": forecast_hour,
                            "modelCadenceHours": 3,
                        },
                    )
                    rendered.append(f"{domain.id}/{style.layer_id}")
        except Exception as exc:
            errors.append(f"{init_time:%Y%m%dT%HZ} F{forecast_hour:03d}: {exc}")
            continue
        return {
            "status": "rendered",
            "validTime": valid.isoformat().replace("+00:00", "Z"),
            "modelRun": init_time.isoformat().replace("+00:00", "Z"),
            "forecastHour": forecast_hour,
            "rendered": rendered,
            "errors": errors,
        }
    return {
        "status": "unavailable",
        "validTime": valid.isoformat().replace("+00:00", "Z"),
        "errors": errors or ["No local three-hour ECMWF control run covers this valid time."],
    }


def update_recent(
    output_root: Path,
    data_root: Path,
    *,
    hours: int = 12,
    domain_ids: Iterable[str] = ("north-america", "north-pacific"),
    now: dt.datetime | None = None,
) -> list[dict[str, object]]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    current -= dt.timedelta(hours=current.hour % 3)
    oldest_offset = max(0, hours // 3 * 3)
    return [
        render_valid_time(
            output_root,
            data_root,
            current - dt.timedelta(hours=offset),
            domain_ids=domain_ids,
        )
        for offset in range(oldest_offset, -1, -3)
    ]
