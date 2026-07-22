from __future__ import annotations

import datetime as dt
import gc
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import numpy as np
import requests
from PIL import Image
from pyproj import Transformer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Domain
from .geomet import projected_bbox


UTC = dt.timezone.utc
GOES_BUCKETS = {"G18": "noaa-goes18", "G19": "noaa-goes19"}
PUBLIC_DOWNLOAD_MIRRORS = {
    # Google mirrors the NOAA GOES archives and is often materially faster
    # from western Canada. Keep NOAA's AWS bucket as the automatic fallback.
    "noaa-goes18": ("https://storage.googleapis.com/gcp-public-data-goes-18",),
    "noaa-goes19": ("https://storage.googleapis.com/gcp-public-data-goes-19",),
}
HIMAWARI_BUCKET = "noaa-himawari9"
GOES_FILENAME = re.compile(r"_s(?P<year>\d{4})(?P<day>\d{3})(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})\d_")
HIMAWARI_FILENAME = re.compile(
    r"HS_H09_(?P<date>\d{8})_(?P<time>\d{4})_B(?P<band>01|02|03|13)_"
    r"FLDK_R\d{2}_S(?P<segment>0[1-5])10\.DAT\.bz2$"
)


@dataclass(frozen=True)
class PublicObject:
    bucket: str
    key: str
    size: int
    valid_time: dt.datetime

    @property
    def url(self) -> str:
        return self.urls[0]

    @property
    def urls(self) -> tuple[str, ...]:
        encoded_key = quote(self.key, safe="/")
        mirrors = tuple(
            f"{base}/{encoded_key}" for base in PUBLIC_DOWNLOAD_MIRRORS.get(self.bucket, ())
        )
        return (*mirrors, f"https://{self.bucket}.s3.amazonaws.com/{encoded_key}")


@dataclass(frozen=True)
class RenderedSatellite:
    visible: Path
    infrared: Path
    infrared_gray: Path
    valid_mask: Path


def normalized_frame_time(source_time: dt.datetime) -> dt.datetime:
    """Put 10/40-minute full-disk scans on the site's 00/30 archive clock."""
    value = source_time.astimezone(UTC)
    return value.replace(minute=(value.minute // 30) * 30, second=0, microsecond=0)


class PublicSatelliteClient:
    def __init__(self, timeout: float = 90.0) -> None:
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

    def __enter__(self) -> "PublicSatelliteClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_prefix(self, bucket: str, prefix: str) -> list[tuple[str, int]]:
        response = self.session.get(
            f"https://{bucket}.s3.amazonaws.com",
            params={"list-type": "2", "prefix": prefix},
            timeout=self.timeout,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        results: list[tuple[str, int]] = []
        for item in root.findall("{*}Contents"):
            key = item.findtext("{*}Key")
            size = item.findtext("{*}Size")
            if key and size:
                results.append((key, int(size)))
        return results

    def latest_goes(self, satellite: str, now: dt.datetime | None = None) -> PublicObject:
        bucket = GOES_BUCKETS[satellite]
        current = (now or dt.datetime.now(UTC)).astimezone(UTC)
        candidates: list[PublicObject] = []
        for hour_offset in range(3):
            hour = current.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=hour_offset)
            prefix = f"ABI-L2-MCMIPF/{hour:%Y}/{hour:%j}/{hour:%H}/"
            for key, size in self.list_prefix(bucket, prefix):
                match = GOES_FILENAME.search(key)
                if not match:
                    continue
                valid = dt.datetime.strptime(
                    f"{match.group('year')}{match.group('day')}{match.group('hour')}"
                    f"{match.group('minute')}{match.group('second')}",
                    "%Y%j%H%M%S",
                ).replace(tzinfo=UTC)
                # Full-disk scans begin every ten minutes. Keep :10/:40 so the
                # raw products add only two frames per hour.
                if (valid.minute - 10) % 30 != 0 or valid > current:
                    continue
                candidates.append(PublicObject(bucket, key, size, valid))
        if not candidates:
            raise RuntimeError(f"No completed 30-minute {satellite} ABI MCMIP full-disk scan was found")
        return max(candidates, key=lambda item: item.valid_time)

    def latest_himawari(self, now: dt.datetime | None = None) -> list[PublicObject]:
        current = (now or dt.datetime.now(UTC)).astimezone(UTC)
        # Require an exact, complete northern-segment set. Delaying the probe
        # by 20 minutes avoids selecting a directory still being uploaded.
        target = current - dt.timedelta(minutes=20)
        target = target.replace(minute=(target.minute // 30) * 30, second=0, microsecond=0)
        for step in range(5):
            valid = target - dt.timedelta(minutes=30 * step)
            prefix = f"AHI-L1b-FLDK/{valid:%Y/%m/%d/%H%M}/"
            selected: list[PublicObject] = []
            seen: set[tuple[str, str]] = set()
            for key, size in self.list_prefix(HIMAWARI_BUCKET, prefix):
                match = HIMAWARI_FILENAME.search(key)
                if not match:
                    continue
                identity = (match.group("band"), match.group("segment"))
                if identity in seen:
                    continue
                seen.add(identity)
                selected.append(PublicObject(HIMAWARI_BUCKET, key, size, valid))
            if len(selected) == 20:
                return sorted(selected, key=lambda item: item.key)
        raise RuntimeError("No complete 30-minute Himawari-9 northern full-disk segment set was found")

    def download(self, item: PublicObject, cache_root: Path, max_bytes: int) -> Path:
        downloads = cache_root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        destination = downloads / Path(item.key).name
        if destination.exists() and destination.stat().st_size == item.size:
            return destination
        destination.unlink(missing_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)
        existing = sum(path.stat().st_size for path in downloads.iterdir() if path.is_file())
        if item.size > max_bytes or existing + item.size > max_bytes:
            raise RuntimeError(
                f"Raw satellite cache cap would be exceeded: {existing + item.size:,} > {max_bytes:,} bytes"
            )
        failures: list[str] = []
        for url in item.urls:
            response = None
            written = 0
            partial.unlink(missing_ok=True)
            try:
                response = self.session.get(url, stream=True, timeout=self.timeout)
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        written += len(chunk)
                if written != item.size:
                    raise RuntimeError(
                        f"Truncated satellite download for {item.key}: "
                        f"{written:,} != {item.size:,}"
                    )
                partial.replace(destination)
                return destination
            except (requests.RequestException, RuntimeError) as error:
                failures.append(f"{url}: {type(error).__name__}: {error}")
            finally:
                partial.unlink(missing_ok=True)
                if response is not None:
                    response.close()
        raise RuntimeError(
            f"All public satellite download endpoints failed for {item.key}: "
            + "; ".join(failures)
        )


def clear_downloads(cache_root: Path) -> int:
    removed = 0
    downloads = cache_root / "downloads"
    if not downloads.exists():
        return removed
    for path in downloads.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _infrared_image(values: np.ndarray) -> Image.Image:
    temperatures = np.asarray(values, dtype=np.float32) - 273.15
    finite = np.isfinite(temperatures)
    # Operational enhancement: warm ground/low cloud is grayscale; colder
    # mid/high cloud progresses through cyan, blue, violet, red and yellow.
    stops = np.array([-100, -90, -80, -70, -60, -50, -40, -30, -20, 0, 20, 45], dtype=np.float32)
    colours = np.array(
        [
            (255, 255, 255), (255, 247, 170), (255, 200, 20), (255, 83, 35),
            (220, 44, 116), (126, 66, 190), (52, 136, 235), (141, 220, 249),
            (245, 245, 245), (160, 160, 160), (72, 72, 72), (20, 20, 20),
        ],
        dtype=np.float32,
    )
    rgb = np.zeros((*temperatures.shape, 3), dtype=np.uint8)
    safe = np.where(finite, temperatures, stops[-1])
    for channel in range(3):
        rgb[..., channel] = np.interp(safe, stops, colours[:, channel]).astype(np.uint8)
    rgb[~finite] = 0
    return Image.fromarray(rgb)


def _infrared_gray_image(values: np.ndarray) -> Image.Image:
    """Render 10.3/10.4 µm brightness temperature without false colour.

    The combined visible/IR product intentionally uses a neutral night image.
    Keeping this separate from ``_infrared_image`` prevents the enhanced IR
    colour table from creating a rainbow fringe along the moving terminator.
    """
    temperatures = np.asarray(values, dtype=np.float32) - 273.15
    finite = np.isfinite(temperatures)
    stops = np.array([-100, -90, -80, -70, -60, -50, -40, -25, -10, 5, 20, 40], dtype=np.float32)
    levels = np.array([255, 252, 245, 232, 216, 198, 178, 151, 124, 94, 62, 30], dtype=np.float32)
    safe = np.where(finite, temperatures, stops[-1])
    gray = np.interp(safe, stops, levels).astype(np.uint8)
    gray[~finite] = 0
    return Image.fromarray(gray).convert("RGB")


def _ahi_visible_image(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> Image.Image:
    """Create a bounded calibrated AHI RGB after target-grid resampling."""
    channels = [np.asarray(value, dtype=np.float32) / 100 for value in (red, green, blue)]
    finite = np.logical_and.reduce([np.isfinite(value) for value in channels])
    rgb = np.stack([np.clip(value, 0, 1) for value in channels], axis=-1)
    # A light gamma/contrast enhancement preserves texture without pretending
    # that this is a source-supplied fixed colour table.
    rgb = np.power(rgb, 1 / 2.2)
    rgb = np.clip((rgb - 0.5) * 1.08 + 0.5, 0, 1)
    rgb[~finite] = 0
    return Image.fromarray((rgb * 255).astype(np.uint8))


def render_satpy_domain(
    source_paths: Iterable[Path],
    reader: str,
    infrared_dataset: str,
    domain: Domain,
    work_root: Path,
    stem: str,
) -> RenderedSatellite:
    """Render calibrated true colour and C13/B13 temperature to one grid."""
    from pyresample import create_area_def
    from satpy import Scene

    auxiliary = work_root / "satpy-data"
    auxiliary.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SATPY_DATA_DIR", str(auxiliary))
    os.environ.setdefault("SATPY_DOWNLOAD_AUX", "1")
    render_root = work_root / "renders"
    render_root.mkdir(parents=True, exist_ok=True)
    visible_png = render_root / f"{stem}-{domain.id}-visible.png"
    visible = render_root / f"{stem}-{domain.id}-visible.webp"
    infrared = render_root / f"{stem}-{domain.id}-ir.webp"
    infrared_gray = render_root / f"{stem}-{domain.id}-ir-gray.webp"
    valid_mask = render_root / f"{stem}-{domain.id}-mask.png"

    scene = Scene(filenames=[str(path) for path in source_paths], reader=reader)
    visible_datasets = ["B01", "B02", "B03"] if reader == "ahi_hsd" else ["true_color"]
    scene.load([*visible_datasets, infrared_dataset])
    area = create_area_def(
        domain.id,
        domain.crs,
        area_extent=projected_bbox(domain),
        shape=(domain.height, domain.width),
        units="m",
    )
    local = scene.resample(area, resampler="nearest")
    if reader == "ahi_hsd":
        _ahi_visible_image(
            np.asarray(local["B03"].values),
            np.asarray(local["B02"].values),
            np.asarray(local["B01"].values),
        ).save(visible, "WEBP", quality=88, method=6)
    else:
        local.save_dataset("true_color", filename=str(visible_png), writer="simple_image")
        Image.open(visible_png).convert("RGB").save(visible, "WEBP", quality=88, method=6)
    values = np.asarray(local[infrared_dataset].values)
    _infrared_image(values).save(infrared, "WEBP", quality=88, method=6)
    validity = (np.isfinite(values) * 255).astype(np.uint8)
    neutral_ir = _infrared_gray_image(values).convert("RGBA")
    neutral_ir.putalpha(Image.fromarray(validity))
    neutral_ir.save(infrared_gray, "WEBP", quality=88, method=6, exact=True)
    Image.fromarray(validity).save(valid_mask, "PNG", optimize=True)
    visible_png.unlink(missing_ok=True)
    rendered = RenderedSatellite(visible, infrared, infrared_gray, valid_mask)
    del values, local, scene
    gc.collect()
    return rendered


def render_satpy_domain_isolated(
    source_paths: Iterable[Path],
    reader: str,
    infrared_dataset: str,
    domain: Domain,
    work_root: Path,
    stem: str,
) -> RenderedSatellite:
    """Render in a short-lived worker so decoded full disks cannot accumulate."""
    sources = [Path(path).resolve() for path in source_paths]
    command = [
        sys.executable,
        "-m",
        "radarsat.raw_satellite",
        "render",
        "--reader",
        reader,
        "--infrared-dataset",
        infrared_dataset,
        "--domain",
        domain.id,
        "--work-root",
        str(work_root.resolve()),
        "--stem",
        stem,
    ]
    for source in sources:
        command.extend(("--source", str(source)))
    environment = os.environ.copy()
    project_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (project_root, environment.get("PYTHONPATH", "")) if value
    )
    subprocess.run(command, check=True, env=environment)
    render_root = work_root / "renders"
    rendered = RenderedSatellite(
        render_root / f"{stem}-{domain.id}-visible.webp",
        render_root / f"{stem}-{domain.id}-ir.webp",
        render_root / f"{stem}-{domain.id}-ir-gray.webp",
        render_root / f"{stem}-{domain.id}-mask.png",
    )
    if not all(path.is_file() and path.stat().st_size > 0 for path in rendered.__dict__.values()):
        raise RuntimeError(f"Isolated raw satellite render did not produce all {domain.id} outputs")
    return rendered


def _longitude_axis(domain: Domain) -> np.ndarray:
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    xs = np.linspace(xmin, xmax, domain.width, dtype=np.float64)
    transformer = Transformer.from_crs(domain.crs, "EPSG:4326", always_xy=True)
    longitudes, _ = transformer.transform(xs, np.full_like(xs, (ymin + ymax) / 2))
    return np.asarray(longitudes)


def blend_satellites(
    first: RenderedSatellite,
    second: RenderedSatellite,
    domain: Domain,
    transition: tuple[float, float],
    visible_destination: Path,
    infrared_destination: Path,
    infrared_gray_destination: Path | None = None,
    *,
    unwrap_longitudes: bool = False,
) -> None:
    """Blend west/east satellites and always prefer the one with valid data.

    A bounded colour-distribution match is feathered into only the visible
    overlap. It reduces the GOES-18/19 chromatic join without recolouring the
    uncontested imagery on either side of the seam.
    """
    longitudes = _longitude_axis(domain)
    if unwrap_longitudes:
        longitudes = np.where(longitudes < 0, longitudes + 360, longitudes)
    weights = np.clip((longitudes - transition[0]) / (transition[1] - transition[0]), 0, 1)
    first_mask = np.asarray(Image.open(first.valid_mask).convert("L")) > 0
    second_mask = np.asarray(Image.open(second.valid_mask).convert("L")) > 0
    weights = np.broadcast_to(weights[None, :], first_mask.shape).copy()
    weights[first_mask & ~second_mask] = 0
    weights[~first_mask & second_mask] = 1
    weights[~first_mask & ~second_mask] = 0

    products: list[tuple[Path, Path, Path, bool, bool]] = [
        (first.visible, second.visible, visible_destination, True, False),
        (first.infrared, second.infrared, infrared_destination, False, False),
    ]
    if infrared_gray_destination is not None:
        products.append(
            (first.infrared_gray, second.infrared_gray, infrared_gray_destination, False, True)
        )

    for left_path, right_path, destination, harmonize_visible, transparent_missing in products:
        left = np.asarray(Image.open(left_path).convert("RGB"), dtype=np.float32)
        right = np.asarray(Image.open(right_path).convert("RGB"), dtype=np.float32)
        if harmonize_visible:
            right = _harmonize_visible_overlap(left, right, first_mask, second_mask, weights)
        output = (left * (1 - weights[..., None]) + right * weights[..., None]).astype(np.uint8)
        output[~first_mask & ~second_mask] = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            image = Image.fromarray(output)
            if transparent_missing:
                image = image.convert("RGBA")
                image.putalpha(Image.fromarray(((first_mask | second_mask) * 255).astype(np.uint8)))
            image.save(temporary, "WEBP", quality=88, method=6, exact=transparent_missing)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def _harmonize_visible_overlap(
    left: np.ndarray,
    right: np.ndarray,
    left_mask: np.ndarray,
    right_mask: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    """Gently match RGB distributions where two true-colour disks overlap."""
    luminance_left = np.mean(left, axis=-1)
    luminance_right = np.mean(right, axis=-1)
    sample = (
        left_mask
        & right_mask
        & (weights > 0.05)
        & (weights < 0.95)
        & (luminance_left > 18)
        & (luminance_right > 18)
        & (luminance_left < 245)
        & (luminance_right < 245)
    )
    if np.count_nonzero(sample) < 500:
        return right

    left_values = left[sample]
    right_values = right[sample]
    left_low, left_mid, left_high = np.percentile(left_values, (20, 50, 80), axis=0)
    right_low, right_mid, right_high = np.percentile(right_values, (20, 50, 80), axis=0)
    spread = np.maximum(right_high - right_low, 8)
    scale = np.clip((left_high - left_low) / spread, 0.90, 1.10)
    offset = np.clip(left_mid - right_mid * scale, -12, 12)
    corrected = np.clip(right * scale + offset, 0, 255)
    # Correction is strongest near the left side of the overlap, where right
    # pixels first enter the blend, and reaches exactly zero in pure-right data.
    correction_weight = np.clip(1 - weights, 0, 1)[..., None]
    return right + (corrected - right) * correction_weight


def _projected_lon_lat_grid(domain: Domain) -> tuple[np.ndarray, np.ndarray]:
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    xs = xmin + (np.arange(domain.width, dtype=np.float64) + 0.5) * (xmax - xmin) / domain.width
    ys = ymax - (np.arange(domain.height, dtype=np.float64) + 0.5) * (ymax - ymin) / domain.height
    xx, yy = np.meshgrid(xs, ys)
    transformer = Transformer.from_crs(domain.crs, "EPSG:4326", always_xy=True)
    longitude, latitude = transformer.transform(xx, yy)
    return np.asarray(longitude, dtype=np.float32), np.asarray(latitude, dtype=np.float32)


def solar_daylight_weight(
    latitude: np.ndarray,
    longitude: np.ndarray,
    valid_time: dt.datetime,
    *,
    full_night_elevation: float = -6.0,
    full_day_elevation: float = 8.0,
) -> np.ndarray:
    """Return a smooth 0=IR, 1=true-colour solar-elevation blend weight."""
    if full_day_elevation <= full_night_elevation:
        raise ValueError("full-day elevation must exceed full-night elevation")
    current = valid_time.replace(tzinfo=UTC) if valid_time.tzinfo is None else valid_time.astimezone(UTC)
    fractional_hour = current.hour + current.minute / 60 + current.second / 3600
    days_in_year = 366 if current.year % 4 == 0 and (current.year % 100 != 0 or current.year % 400 == 0) else 365
    gamma = 2 * np.pi / days_in_year * (current.timetuple().tm_yday - 1 + (fractional_hour - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )
    solar_minutes = (fractional_hour * 60 + equation_of_time + 4 * longitude) % 1440
    hour_angle = np.deg2rad(solar_minutes / 4 - 180)
    latitude_radians = np.deg2rad(latitude)
    cosine_zenith = (
        np.sin(latitude_radians) * np.sin(declination)
        + np.cos(latitude_radians) * np.cos(declination) * np.cos(hour_angle)
    )
    elevation = 90 - np.rad2deg(np.arccos(np.clip(cosine_zenith, -1, 1)))
    linear = np.clip(
        (elevation - full_night_elevation) / (full_day_elevation - full_night_elevation),
        0,
        1,
    )
    smooth = linear * linear * (3 - 2 * linear)
    smooth[~np.isfinite(elevation)] = 0
    return np.asarray(smooth, dtype=np.float32)


def compose_visible_infrared(
    visible_path: Path,
    infrared_gray_path: Path,
    domain: Domain,
    valid_time: dt.datetime,
    destination: Path,
) -> dict[str, object]:
    """Write a true-colour day / neutral-IR night WebP on the domain grid."""
    visible = np.asarray(Image.open(visible_path).convert("RGB"), dtype=np.float32)
    infrared_image = Image.open(infrared_gray_path).convert("RGBA")
    infrared = np.asarray(infrared_image, dtype=np.float32)[..., :3]
    infrared_alpha = np.asarray(infrared_image.getchannel("A"))
    if visible.shape != infrared.shape or visible.shape[:2] != (domain.height, domain.width):
        raise ValueError("visible and infrared rasters must match the configured domain grid")
    longitude, latitude = _projected_lon_lat_grid(domain)
    weight = solar_daylight_weight(latitude, longitude, valid_time)
    # Low-angle true-colour composites can develop an artificial red/yellow
    # fringe even when the IR side is neutral. Fade chroma separately before
    # fading luminance, so twilight retains texture without the coloured rim.
    chroma_weight = solar_daylight_weight(
        latitude,
        longitude,
        valid_time,
        full_night_elevation=1.0,
        full_day_elevation=12.0,
    )
    visible_luminance = (
        visible[..., 0] * 0.2126
        + visible[..., 1] * 0.7152
        + visible[..., 2] * 0.0722
    )[..., None]
    safe_visible = visible_luminance + (visible - visible_luminance) * chroma_weight[..., None]
    visible_peak = np.max(visible, axis=-1)
    rows = np.arange(domain.height)[:, None]
    far_northern_edge = rows < max(2, round(domain.height * 0.18))
    visible_valid = (visible_peak > 2) & ~(far_northern_edge & (visible_peak <= 32))
    infrared_valid = (infrared_alpha > 0) & (np.max(infrared, axis=-1) > 2)
    # At the geostationary scan edge, one source can contain tiny missing-data
    # arcs. Fall back to neutral IR where visible is absent and make pixels
    # transparent only when neither observation exists; never interpolate
    # invented cloud or surface detail into those gaps.
    available_day_weight = weight * visible_valid
    output = np.clip(
        safe_visible * available_day_weight[..., None]
        + infrared * (1 - available_day_weight[..., None]),
        0,
        255,
    ).astype(np.uint8)
    alpha = np.where(visible_valid | infrared_valid, 255, 0).astype(np.uint8)
    output = np.dstack((output, alpha))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        Image.fromarray(output).save(temporary, "WEBP", quality=88, method=6, exact=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "blendMethod": "solar-elevation smoothstep",
        "fullNightSunElevationDegrees": -6.0,
        "fullDaySunElevationDegrees": 8.0,
        "fullColourSunElevationDegrees": 12.0,
        "zeroColourSunElevationDegrees": 1.0,
        "nightInfrared": "10.3/10.4 µm brightness temperature, neutral grayscale",
        "missingData": "transparent where visible and infrared are both unavailable",
    }


def neutralize_archived_infrared(source: Path, destination: Path) -> None:
    """Recover a monotonic neutral IR image from the archived colour table.

    Existing ``raw-ir`` WebPs predate the neutral render. Rather than fetching
    hundreds of megabytes of ABI/AHI source data again, quantize each pixel to
    the known enhancement ramp, infer its approximate temperature, and apply
    the new grayscale ramp. The original archive frame is never modified.
    """
    temperatures = np.linspace(-100, 45, 256, dtype=np.float32)
    stops = np.array([-100, -90, -80, -70, -60, -50, -40, -30, -20, 0, 20, 45], dtype=np.float32)
    colours = np.array(
        [
            (255, 255, 255), (255, 247, 170), (255, 200, 20), (255, 83, 35),
            (220, 44, 116), (126, 66, 190), (52, 136, 235), (141, 220, 249),
            (245, 245, 245), (160, 160, 160), (72, 72, 72), (20, 20, 20),
        ],
        dtype=np.float32,
    )
    palette_rgb = np.stack(
        [np.interp(temperatures, stops, colours[:, channel]) for channel in range(3)],
        axis=-1,
    ).astype(np.uint8)
    palette = Image.new("P", (1, 1))
    palette.putpalette(palette_rgb.reshape(-1).tolist())
    original = Image.open(source).convert("RGB")
    indices = np.asarray(
        original.quantize(palette=palette, dither=Image.Dither.NONE),
        dtype=np.uint8,
    )
    inferred_kelvin = temperatures[indices] + 273.15
    neutral = np.asarray(_infrared_gray_image(inferred_kelvin).convert("RGB")).copy()
    # Preserve genuinely missing/outside-disk pixels rather than interpreting
    # their black fill as a very warm surface temperature.
    original_values = np.asarray(original)
    source_peak = np.max(original_values, axis=-1)
    rows = np.arange(original.height)[:, None]
    far_northern_edge = rows < max(2, round(original.height * 0.18))
    missing = (source_peak <= 2) | (far_northern_edge & (source_peak <= 32))
    neutral[missing] = 0
    alpha = np.where(missing, 0, 255).astype(np.uint8)
    destination.parent.mkdir(parents=True, exist_ok=True)
    neutral_image = Image.fromarray(neutral).convert("RGBA")
    neutral_image.putalpha(Image.fromarray(alpha))
    neutral_image.save(destination, "PNG", optimize=True)


def install_render(
    rendered: RenderedSatellite,
    visible_destination: Path,
    infrared_destination: Path,
    infrared_gray_destination: Path | None = None,
) -> None:
    products = [
        (rendered.visible, visible_destination, False),
        (rendered.infrared, infrared_destination, False),
    ]
    if infrared_gray_destination is not None:
        products.append((rendered.infrared_gray, infrared_gray_destination, True))
    for source, destination, preserve_alpha in products:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            mode = "RGBA" if preserve_alpha else "RGB"
            Image.open(source).convert(mode).save(
                temporary,
                "WEBP",
                quality=88,
                method=6,
                exact=preserve_alpha,
            )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import DOMAINS

    parser = argparse.ArgumentParser(description="Isolated Radar-Sat raw satellite renderer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--source", action="append", type=Path, required=True)
    render.add_argument("--reader", required=True)
    render.add_argument("--infrared-dataset", required=True)
    render.add_argument("--domain", choices=sorted(DOMAINS), required=True)
    render.add_argument("--work-root", type=Path, required=True)
    render.add_argument("--stem", required=True)
    args = parser.parse_args(argv)
    if args.command == "render":
        render_satpy_domain(
            args.source,
            args.reader,
            args.infrared_dataset,
            DOMAINS[args.domain],
            args.work_root,
            args.stem,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
