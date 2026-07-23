from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont, ImageOps
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

from .config import LAYERS, Domain
from .geomet import UTC, format_utc, parse_utc, projected_bbox


NATIVE_SOURCE = "ECCC Datamart"
NATIVE_LAYER_IDS = frozenset(
    {"daynight", "ir", "convective", "snowfog", "lightning"}
)

SATELLITE_PRODUCTS = {
    "DayVis-NightIR": ("daynight", "1km"),
    "NightIR": ("ir", "2km"),
    "VisibleIRSandwich-NightMicrophysicsIR": ("convective", "1km"),
    "SnowFog-NightMicrophysics": ("snowfog", "1km"),
}
SATELLITE_RE = re.compile(
    r"^(?P<time>\d{8}T\d{4}Z)_MSC_GOES-West_(?P<product>"
    + "|".join(re.escape(value) for value in SATELLITE_PRODUCTS)
    + r")_(?P<resolution>1km|2km)\.tif$"
)
LIGHTNING_RE = re.compile(
    r"^(?P<time>\d{8}T\d{4}Z)_MSC_Lightning_2\.5km\.tif$"
)
RADAR_RE = re.compile(
    r"^(?P<time>\d{8}T\d{4}Z)_MSC_Radar-DPQPE_"
    r"(?P<station>CASAG|CASHP|CASSS|CASPG)_"
    r"(?P<phase>Rain|Snow)(?P<contingency>-Contingency)?\.gif$"
)

STATIONS = {
    "CASAG": "Aldergrove",
    "CASHP": "Halfmoon Peak",
    "CASSS": "Silver Star Mountain",
    "CASPG": "Prince George",
}

MAX_TIFF_BYTES = 80 * 1024 * 1024
MAX_GIF_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class NativeFile:
    path: Path
    valid_time: dt.datetime
    layer_id: str
    source_layer: str
    station: str | None = None
    contingency: bool = False


@dataclass
class SpoolIngestResult:
    discovered: dict[str, int] = field(default_factory=dict)
    rendered: dict[str, int] = field(default_factory=dict)
    timelines: dict[str, list[dt.datetime]] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    preserve_files: set[str] = field(default_factory=set)

    def add_discovered(self, layer_id: str, count: int) -> None:
        if count:
            self.discovered[layer_id] = self.discovered.get(layer_id, 0) + count

    def add_rendered(self, layer_id: str) -> None:
        self.rendered[layer_id] = self.rendered.get(layer_id, 0) + 1

    def status(self) -> dict[str, object]:
        return {
            "discovered": dict(sorted(self.discovered.items())),
            "rendered": dict(sorted(self.rendered.items())),
            "rejected": self.rejected,
            "preserveFiles": sorted(self.preserve_files),
        }


def _parse_filename_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%dT%H%MZ").replace(tzinfo=UTC)


def _has_hidden_component(path: Path, directory: Path) -> bool:
    try:
        relative = path.relative_to(directory)
    except ValueError:
        return True
    return any(part.startswith(".") for part in relative.parts)


def _safe_regular_file(
    path: Path,
    directory: Path,
    *,
    maximum_bytes: int,
    magic: tuple[bytes, ...],
) -> str | None:
    """Return a rejection reason, or ``None`` for a completed spool object."""
    if _has_hidden_component(path, directory):
        return "hidden/inflight path"
    if path.is_symlink() or not path.is_file():
        return "not a regular non-symlink file"
    try:
        if not path.resolve().is_relative_to(directory.resolve()):
            return "path escapes feed directory"
        size = path.stat().st_size
        if size <= 8:
            return "file is too small"
        if size > maximum_bytes:
            return f"file exceeds {maximum_bytes} byte safety limit"
        with path.open("rb") as stream:
            header = stream.read(max(len(value) for value in magic))
    except OSError as error:
        return f"cannot inspect file: {error}"
    if not any(header.startswith(value) for value in magic):
        return "unexpected file signature"
    return None


def _scan_matching(
    directory: Path,
    pattern: re.Pattern[str],
    *,
    suffix: str,
    maximum_bytes: int,
    magic: tuple[bytes, ...],
    now: dt.datetime,
    rejected: list[str],
) -> Iterable[tuple[Path, re.Match[str], dt.datetime]]:
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob(f"*{suffix}")):
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        reason = _safe_regular_file(
            path,
            directory,
            maximum_bytes=maximum_bytes,
            magic=magic,
        )
        if reason:
            rejected.append(f"{path.name}: {reason}")
            continue
        valid_time = _parse_filename_time(match.group("time"))
        if valid_time > now + dt.timedelta(minutes=5):
            rejected.append(f"{path.name}: source time is more than five minutes in the future")
            continue
        yield path, match, valid_time


def discover_spool(spool_root: Path, now: dt.datetime | None = None) -> tuple[list[NativeFile], list[str]]:
    """Discover only completed, recognized native products below exact feed roots."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    root = spool_root.expanduser()
    rejected: list[str] = []
    files: list[NativeFile] = []

    satellite_root = root / "satellite"
    for path, match, valid_time in _scan_matching(
        satellite_root,
        SATELLITE_RE,
        suffix=".tif",
        maximum_bytes=MAX_TIFF_BYTES,
        magic=(b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
        now=now,
        rejected=rejected,
    ):
        product = match.group("product")
        layer_id, expected_resolution = SATELLITE_PRODUCTS[product]
        if match.group("resolution") != expected_resolution:
            rejected.append(
                f"{path.name}: expected {expected_resolution} resolution for {product}"
            )
            continue
        files.append(
            NativeFile(
                path=path,
                valid_time=valid_time,
                layer_id=layer_id,
                source_layer=f"satellite/goes/west/{product}",
            )
        )

    lightning_root = root / "lightning"
    for path, _match, valid_time in _scan_matching(
        lightning_root,
        LIGHTNING_RE,
        suffix=".tif",
        maximum_bytes=MAX_TIFF_BYTES,
        magic=(b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
        now=now,
        rejected=rejected,
    ):
        files.append(
            NativeFile(
                path=path,
                valid_time=valid_time,
                layer_id="lightning",
                source_layer="lightning/Lightning_2.5km_Density",
            )
        )

    radar_root = root / "radar"
    for path, match, valid_time in _scan_matching(
        radar_root,
        RADAR_RE,
        suffix=".gif",
        maximum_bytes=MAX_GIF_BYTES,
        magic=(b"GIF87a", b"GIF89a"),
        now=now,
        rejected=rejected,
    ):
        # The montage is a DPQPE rain diagnostic. Snow DPQPE and CAPPI remain
        # in the raw spool for future products but are deliberately not mixed.
        if match.group("phase") != "Rain":
            continue
        station = match.group("station")
        files.append(
            NativeFile(
                path=path,
                valid_time=valid_time,
                layer_id="site-radar",
                source_layer=f"radar/DPQPE/GIF/{station}/Rain",
                station=station,
                contingency=bool(match.group("contingency")),
            )
        )
    return files, rejected


def _selected_times(
    times: Iterable[dt.datetime], hours: float, latest_only: bool
) -> list[dt.datetime]:
    values = sorted(set(times))
    if not values:
        return []
    if latest_only:
        return [values[-1]]
    cutoff = values[-1] - dt.timedelta(hours=hours)
    return [value for value in values if value >= cutoff]


def _native_validity_time(dataset: rasterio.io.DatasetReader) -> dt.datetime | None:
    candidates = [dataset.tags().get("VALIDITY_DATETIME")]
    candidates.extend(dataset.tags(index).get("VALIDITY_DATETIME") for index in range(1, dataset.count + 1))
    for value in candidates:
        if not value:
            continue
        try:
            return parse_utc(value)
        except ValueError:
            continue
    return None


def _validate_raster(dataset: rasterio.io.DatasetReader, native_file: NativeFile) -> dt.datetime:
    if dataset.crs is None:
        raise ValueError("GeoTIFF has no CRS")
    source_time = _native_validity_time(dataset) or native_file.valid_time
    if abs((source_time - native_file.valid_time).total_seconds()) > 120:
        raise ValueError(
            "GeoTIFF validity tag does not agree with its filename "
            f"({format_utc(source_time)} versus {format_utc(native_file.valid_time)})"
        )
    return source_time


def _destination_grid(domain: Domain) -> tuple[Any, int, int]:
    return (
        from_bounds(*projected_bbox(domain), domain.width, domain.height),
        domain.width,
        domain.height,
    )


def render_satellite(native_file: NativeFile, destination: Path, domain: Domain) -> dt.datetime:
    destination_transform, width, height = _destination_grid(domain)
    with rasterio.open(native_file.path) as source:
        if source.count < 3:
            raise ValueError(f"satellite GeoTIFF has {source.count} band(s), expected RGB")
        source_time = _validate_raster(source, native_file)
        output = np.zeros((height, width, 3), dtype=np.uint8)
        for output_index, source_index in enumerate((1, 2, 3)):
            reproject(
                source=rasterio.band(source, source_index),
                destination=output[:, :, output_index],
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=source.nodata,
                dst_transform=destination_transform,
                dst_crs=domain.crs,
                dst_nodata=0,
                resampling=Resampling.bilinear,
                num_threads=2,
            )
    if all(low == high for low, high in Image.fromarray(output).getextrema()):
        raise ValueError("reprojected satellite image is blank")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        Image.fromarray(output).save(temporary, "WEBP", quality=88, method=4)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return source_time


def _lightning_rgba(values: np.ndarray) -> np.ndarray:
    """Render positive flash-density cells; zero and nodata stay transparent."""
    finite = np.isfinite(values)
    positive = finite & (values > 0)
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    if not np.any(positive):
        return rgba

    # Match the public GeoMet Lightning style: dark blue near zero, through
    # cyan/green/yellow, to red at the 2+ flashes km-2 min-1 legend ceiling.
    # Piecewise interpolation keeps the quantitative density panel consistent
    # while nearest-neighbour reprojection preserves isolated source cells.
    stops = np.asarray(
        (0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0),
        dtype=np.float32,
    )
    colours = np.asarray(
        (
            (0, 0, 127),
            (0, 2, 204),
            (0, 54, 253),
            (3, 179, 250),
            (44, 252, 209),
            (148, 252, 105),
            (228, 233, 25),
            (253, 155, 0),
            (253, 51, 0),
            (209, 4, 0),
            (127, 0, 0),
        ),
        dtype=np.float32,
    )
    clipped = np.clip(values[positive], stops[0], stops[-1])
    for channel in range(3):
        rgba[positive, channel] = np.interp(clipped, stops, colours[:, channel]).astype(np.uint8)
    rgba[positive, 3] = 255
    return rgba


def render_lightning(native_file: NativeFile, destination: Path, domain: Domain) -> dt.datetime:
    destination_transform, width, height = _destination_grid(domain)
    with rasterio.open(native_file.path) as source:
        if source.count != 1:
            raise ValueError(f"lightning GeoTIFF has {source.count} bands, expected one")
        source_time = _validate_raster(source, native_file)
        values = np.full((height, width), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(source, 1),
            destination=values,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=destination_transform,
            dst_crs=domain.crs,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
            num_threads=2,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        image = Image.fromarray(_lightning_rgba(values))
        image.quantize(
            colors=32,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.NONE,
        ).save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return source_time


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(filename, size=max(8, size))
    except OSError:
        return ImageFont.load_default()


def render_site_montage(
    station_files: dict[str, NativeFile], destination: Path, domain: Domain, valid_time: dt.datetime
) -> None:
    if set(station_files) != set(STATIONS):
        raise ValueError("site montage requires all four BC radar stations")
    width, height = domain.width, domain.height
    canvas = Image.new("RGB", (width, height), "#071018")
    draw = ImageDraw.Draw(canvas)
    scale = max(1, min(width, height))
    margin = max(4, round(scale * 0.007))
    gap = margin
    global_header = max(24, round(height * 0.042))
    cell_width = (width - 2 * margin - gap) // 2
    cell_height = (height - global_header - 2 * margin - gap) // 2
    panel_header = max(18, min(42, round(cell_height * 0.065)))

    heading = f"DPQPE SITE DIAGNOSTIC  •  {valid_time:%Y-%m-%d %H:%M UTC}  •  SYNCHRONIZED"
    draw.text(
        (margin, max(2, (global_header - max(10, round(height * 0.021))) // 2)),
        heading,
        fill="#eef5f8",
        font=_font(round(height * 0.021), bold=True),
    )

    for index, station in enumerate(STATIONS):
        row, column = divmod(index, 2)
        left = margin + column * (cell_width + gap)
        top = global_header + margin + row * (cell_height + gap)
        right = left + cell_width
        bottom = top + cell_height
        draw.rounded_rectangle((left, top, right, bottom), radius=max(2, margin // 2), fill="#101d25", outline="#50626e", width=1)
        native_file = station_files[station]
        label = f"{STATIONS[station]}  ({station})"
        label_colour = "#ffb24a" if native_file.contingency else "#f4f7f8"
        draw.text(
            (left + margin, top + max(1, panel_header // 7)),
            label,
            fill=label_colour,
            font=_font(max(9, round(panel_header * 0.48)), bold=True),
        )
        if native_file.contingency:
            contingency = "CONTINGENCY"
            text_box = draw.textbbox((0, 0), contingency, font=_font(max(8, round(panel_header * 0.40)), bold=True))
            draw.text(
                (right - margin - (text_box[2] - text_box[0]), top + max(1, panel_header // 6)),
                contingency,
                fill="#ffb24a",
                font=_font(max(8, round(panel_header * 0.40)), bold=True),
            )
        with Image.open(native_file.path) as source:
            source.seek(0)
            panel = source.convert("RGB")
            available = (max(1, cell_width - 2 * margin), max(1, cell_height - panel_header - margin))
            panel = ImageOps.contain(panel, available, Image.Resampling.LANCZOS)
        panel_left = left + (cell_width - panel.width) // 2
        panel_top = top + panel_header + (cell_height - panel_header - panel.height) // 2
        canvas.paste(panel, (panel_left, panel_top))

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        canvas.quantize(
            colors=256,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.NONE,
        ).save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _metadata_source(path: Path) -> str | None:
    try:
        return str(json.loads(path.read_text()).get("source"))
    except (OSError, json.JSONDecodeError):
        return None


def _choose_unique(files: Iterable[NativeFile]) -> dict[dt.datetime, NativeFile]:
    selected: dict[dt.datetime, NativeFile] = {}
    for native_file in files:
        previous = selected.get(native_file.valid_time)
        if previous is None or (previous.contingency and not native_file.contingency):
            selected[native_file.valid_time] = native_file
    return selected


def ingest_spool(
    spool_root: Path,
    output_root: Path,
    domain: Domain,
    hours: float,
    latest_only: bool,
    *,
    now: dt.datetime | None = None,
) -> SpoolIngestResult:
    """Render native BC observations without moving or deleting raw files."""
    # Import archive helpers lazily so ``pipeline`` can call this module without
    # a module-initialization cycle.
    from .pipeline import (
        derive_eccc_lightning_points,
        frame_path,
        metadata_path,
        write_metadata,
    )

    result = SpoolIngestResult()
    files, result.rejected = discover_spool(spool_root, now=now)
    result.preserve_files.update(
        reason.split(":", 1)[0].strip() for reason in result.rejected if reason.strip()
    )
    grouped: dict[str, list[NativeFile]] = {}
    for native_file in files:
        grouped.setdefault(native_file.layer_id, []).append(native_file)

    for layer_id in sorted(NATIVE_LAYER_IDS):
        unique = _choose_unique(grouped.get(layer_id, []))
        result.add_discovered(layer_id, len(unique))
        result.timelines[layer_id] = sorted(unique)
        layer = LAYERS[layer_id]
        for valid_time in _selected_times(unique, hours, latest_only):
            native_file = unique[valid_time]
            destination = frame_path(output_root, domain, layer, valid_time)
            metadata = metadata_path(output_root, domain, layer, valid_time)
            if destination.exists() and _metadata_source(metadata) == NATIVE_SOURCE:
                if layer_id == "lightning":
                    derive_eccc_lightning_points(output_root, domain, valid_time)
                continue
            try:
                if layer_id == "lightning":
                    source_time = render_lightning(native_file, destination, domain)
                else:
                    source_time = render_satellite(native_file, destination, domain)
                write_metadata(
                    output_root,
                    domain,
                    layer,
                    valid_time,
                    destination,
                    {"native": source_time},
                    source=NATIVE_SOURCE,
                    source_layer=native_file.source_layer,
                    extra={
                        "sourceFormat": "GeoTIFF",
                        "sourceFiles": [native_file.path.name],
                    },
                )
                if layer_id == "lightning":
                    derive_eccc_lightning_points(output_root, domain, valid_time)
                result.add_rendered(layer_id)
            except Exception as error:
                result.preserve_files.add(native_file.path.name)
                result.rejected.append(
                    f"{native_file.path.name}: {type(error).__name__}: {error}"
                )

    station_groups: dict[str, dict[dt.datetime, NativeFile]] = {}
    for station in STATIONS:
        station_groups[station] = _choose_unique(
            native_file
            for native_file in grouped.get("site-radar", [])
            if native_file.station == station
        )
    result.add_discovered(
        "site-radar", sum(len(values) for values in station_groups.values())
    )
    common_times: set[dt.datetime] = set(station_groups["CASAG"])
    for station in tuple(STATIONS)[1:]:
        common_times.intersection_update(station_groups[station])
    result.timelines["site-radar"] = sorted(common_times)
    layer = LAYERS["site-radar"]
    for valid_time in _selected_times(common_times, hours, latest_only):
        station_files = {
            station: station_groups[station][valid_time] for station in STATIONS
        }
        destination = frame_path(output_root, domain, layer, valid_time)
        metadata = metadata_path(output_root, domain, layer, valid_time)
        expected_files = [station_files[station].path.name for station in STATIONS]
        current_files: list[str] = []
        if metadata.exists():
            try:
                current_files = json.loads(metadata.read_text()).get("sourceFiles", [])
            except (OSError, json.JSONDecodeError):
                pass
        if (
            destination.exists()
            and _metadata_source(metadata) == NATIVE_SOURCE
            and current_files == expected_files
        ):
            continue
        try:
            render_site_montage(station_files, destination, domain, valid_time)
            source_times = {
                station: station_files[station].valid_time for station in STATIONS
            }
            contingency_sites = [
                station for station in STATIONS if station_files[station].contingency
            ]
            write_metadata(
                output_root,
                domain,
                layer,
                valid_time,
                destination,
                source_times,
                source=NATIVE_SOURCE,
                source_layer="radar/DPQPE/GIF/BC-four-site/Rain",
                extra={
                    "sourceFormat": "GIF montage",
                    "sourceFiles": expected_files,
                    "stations": list(STATIONS),
                    "contingencySites": contingency_sites,
                    "synchronization": "exact source timestamp",
                },
            )
            result.add_rendered("site-radar")
        except Exception as error:
            result.preserve_files.update(expected_files)
            result.rejected.append(
                f"site-radar {format_utc(valid_time)}: {type(error).__name__}: {error}"
            )
    return result
