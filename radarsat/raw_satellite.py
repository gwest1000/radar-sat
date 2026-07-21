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
        return f"https://{self.bucket}.s3.amazonaws.com/{quote(self.key, safe='/')}"


@dataclass(frozen=True)
class RenderedSatellite:
    visible: Path
    infrared: Path
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
        response = self.session.get(item.url, stream=True, timeout=self.timeout)
        response.raise_for_status()
        written = 0
        try:
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    written += len(chunk)
            if written != item.size:
                raise RuntimeError(f"Truncated satellite download for {item.key}: {written:,} != {item.size:,}")
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
            response.close()
        return destination


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
    Image.fromarray((np.isfinite(values) * 255).astype(np.uint8)).save(valid_mask, "PNG", optimize=True)
    visible_png.unlink(missing_ok=True)
    rendered = RenderedSatellite(visible, infrared, valid_mask)
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
    *,
    unwrap_longitudes: bool = False,
) -> None:
    """Blend west/east satellites and always prefer the one with valid data."""
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

    for left_path, right_path, destination in (
        (first.visible, second.visible, visible_destination),
        (first.infrared, second.infrared, infrared_destination),
    ):
        left = np.asarray(Image.open(left_path).convert("RGB"), dtype=np.float32)
        right = np.asarray(Image.open(right_path).convert("RGB"), dtype=np.float32)
        output = (left * (1 - weights[..., None]) + right * weights[..., None]).astype(np.uint8)
        output[~first_mask & ~second_mask] = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            Image.fromarray(output).save(temporary, "WEBP", quality=88, method=6)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def install_render(rendered: RenderedSatellite, visible_destination: Path, infrared_destination: Path) -> None:
    for source, destination in ((rendered.visible, visible_destination), (rendered.infrared, infrared_destination)):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            Image.open(source).convert("RGB").save(temporary, "WEBP", quality=88, method=6)
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
