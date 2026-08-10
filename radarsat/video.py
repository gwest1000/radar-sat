from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from PIL import Image

from .catalog import build_catalog
from .config import BROAD_VIEWPORTS, PRODUCTS, VIEWPORTS


UTC = dt.timezone.utc
VIDEO_SCHEMA_VERSION = 1
VIDEO_RENDER_VERSION = 5
PROXY_RENDER_VERSION = 1
METEOROLOGICAL_MINUTE_SECONDS = 0.022
FFCONCAT_TIMEBASE_FPS = 50
LOCAL_GENERATIONS_TO_KEEP = 3
LOCAL_ORPHAN_GRACE_HOURS = 1

SATELLITE_LAYER_IDS = frozenset(
    {
        "raw-visir",
        "raw-visir-5min",
        "raw-visir-native",
        "raw-ir",
        "westwx-visir",
        "westwx-ir",
        "eccc-geocolor",
        "daynight",
        "ir",
        "convective",
        "snowfog",
    }
)
REGIONAL_LAYER_BASES = frozenset(
    {
        "lightning-trail",
        "lightning-hour",
        "hotspots",
        "hrdps-hgt500",
        "hrdps-mslp",
    }
)
REGIONAL_PRODUCT_KEYS = {
    "bc-small-overlay": "small",
    "bc-southwest-overlay": "southwest",
    "bc-southeast-overlay": "southeast",
    "bc-northeast-overlay": "northeast",
}
FULL_VIEWPORT = {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0}
BC_REGIONAL_MEDIA_VIEWPORT = {
    "left": min(VIEWPORTS[key]["left"] for key in REGIONAL_PRODUCT_KEYS.values()),
    "top": min(VIEWPORTS[key]["top"] for key in REGIONAL_PRODUCT_KEYS.values()),
    "width": max(
        VIEWPORTS[key]["left"] + VIEWPORTS[key]["width"]
        for key in REGIONAL_PRODUCT_KEYS.values()
    )
    - min(VIEWPORTS[key]["left"] for key in REGIONAL_PRODUCT_KEYS.values()),
    "height": max(
        VIEWPORTS[key]["top"] + VIEWPORTS[key]["height"]
        for key in REGIONAL_PRODUCT_KEYS.values()
    )
    - min(VIEWPORTS[key]["top"] for key in REGIONAL_PRODUCT_KEYS.values()),
}
SOURCE_MEDIA_SIZES: Mapping[tuple[str, str], tuple[int, int]] = {
    ("bc", "raw-visir"): (1920, 1472),
    ("bc", "raw-visir-5min"): (3840, 2944),
    ("bc", "raw-ir"): (1920, 1472),
    ("bc", "eccc-geocolor"): (3000, 2300),
    ("bc", "daynight"): (1920, 1472),
    ("bc", "ir"): (1920, 1472),
    ("bc", "convective"): (1920, 1472),
    ("bc", "snowfog"): (1920, 1472),
    ("north-america", "westwx-visir"): (1280, 960),
    ("north-america", "westwx-ir"): (1280, 960),
    ("north-pacific", "raw-visir"): (1600, 900),
    ("north-pacific", "raw-ir"): (1600, 900),
}


@dataclass(frozen=True)
class ProfileSpec:
    product_id: str
    domain_id: str
    layer_id: str
    viewport: Mapping[str, float]
    width: int
    height: int
    cadence_minutes: int
    track_name: str = "live"
    media_group: str = "full"
    media_viewport: Mapping[str, float] | None = None
    media_width: int | None = None
    media_height: int | None = None
    crf: int = 18
    preset: str = "medium"

    @property
    def track(self) -> str:
        return self.track_name

    @property
    def resolved_media_viewport(self) -> Mapping[str, float]:
        return self.media_viewport or FULL_VIEWPORT

    @property
    def resolved_media_width(self) -> int:
        return self.media_width or self.width

    @property
    def resolved_media_height(self) -> int:
        return self.media_height or self.height

    @property
    def gop_frames(self) -> int:
        return max(1, round(60 / self.cadence_minutes))


def _even(value: float) -> int:
    rounded = max(2, round(value))
    return rounded if rounded % 2 == 0 else rounded + 1


def _display_size(domain_id: str, viewport: Mapping[str, float]) -> tuple[int, int]:
    source_width, source_height = SOURCE_MEDIA_SIZES[
        (domain_id, "westwx-visir" if domain_id == "north-america" else "raw-visir")
    ]
    width = 1920 if domain_id == "bc" else 1200
    height = _even(
        width
        * source_height
        / source_width
        * float(viewport["height"])
        / float(viewport["width"])
    )
    return width, height


def _media_geometry(
    product_id: str,
    domain_id: str,
    layer_id: str,
    track: str,
) -> tuple[str, Mapping[str, float], int, int]:
    source_width, source_height = SOURCE_MEDIA_SIZES[(domain_id, layer_id)]
    if domain_id == "bc" and layer_id == "raw-visir-5min" and track == "archive":
        source_width, source_height = SOURCE_MEDIA_SIZES[("bc", "raw-visir")]
    if track == "archive" and domain_id == "bc" and source_width > 1600:
        source_height = _even(source_height * 1600 / source_width)
        source_width = 1600
    elif track == "live" and layer_id == "eccc-geocolor" and source_width > 2400:
        source_height = _even(source_height * 2400 / source_width)
        source_width = 2400
    if (
        domain_id == "bc"
        and layer_id == "raw-visir"
        and track == "live"
        and product_id in REGIONAL_PRODUCT_KEYS
    ):
        viewport = dict(
            next(product for product in PRODUCTS if product["id"] == product_id).get(
                "viewport", FULL_VIEWPORT
            )
        )
        width, height = _display_size(domain_id, viewport)
        return "regional-hires", viewport, width, height
    if (
        domain_id == "bc"
        and layer_id == "raw-visir-5min"
        and track == "live"
        and product_id in REGIONAL_PRODUCT_KEYS
    ):
        viewport = BC_REGIONAL_MEDIA_VIEWPORT
        return (
            "regional-hires",
            viewport,
            _even(source_width * float(viewport["width"])),
            _even(source_height * float(viewport["height"])),
        )
    if (
        domain_id == "bc"
        and layer_id == "raw-visir"
        and track == "live"
        and product_id == "bc-large-overlay"
    ):
        viewport = dict(
            next(product for product in PRODUCTS if product["id"] == product_id).get(
                "viewport", FULL_VIEWPORT
            )
        )
        width = 1920
        height = _even(
            width
            * source_height
            / source_width
            * float(viewport["height"])
            / float(viewport["width"])
        )
        return "bc-xl", viewport, width, height
    return "full", FULL_VIEWPORT, source_width, source_height


def _all_profiles() -> tuple[ProfileSpec, ...]:
    profiles: list[ProfileSpec] = []
    for product in PRODUCTS:
        product_id = str(product["id"])
        domain_id = str(product["domain"])
        viewport = dict(product.get("viewport", FULL_VIEWPORT))
        display_width, display_height = _display_size(domain_id, viewport)
        satellite_layers = [
            str(recipe["id"])
            for recipe in product.get("layers", [])
            if str(recipe.get("id", "")) in SATELLITE_LAYER_IDS
        ]
        for layer_id in satellite_layers:
            for track, cadence in (
                ("live", int(product.get("frameIntervalMinutes", 10))),
                ("archive", int(product.get("archiveFrameIntervalMinutes", 60))),
            ):
                media_group, media_viewport, media_width, media_height = _media_geometry(
                    product_id, domain_id, layer_id, track
                )
                profiles.append(
                    ProfileSpec(
                        product_id=product_id,
                        domain_id=domain_id,
                        layer_id=layer_id,
                        viewport=viewport,
                        width=display_width,
                        height=display_height,
                        cadence_minutes=cadence,
                        track_name=track,
                        media_group=media_group,
                        media_viewport=media_viewport,
                        media_width=media_width,
                        media_height=media_height,
                        crf=(
                            21
                            if track == "archive"
                            else 18
                            if layer_id in {"raw-visir", "raw-visir-5min"}
                            else 19
                        ),
                    )
                )
    return tuple(profiles)


VIDEO_PROFILES = _all_profiles()
# Backwards-compatible export for the existing command and tests.
PILOT_PROFILES = VIDEO_PROFILES


@dataclass(frozen=True)
class SelectedFrame:
    valid_time: dt.datetime
    source_valid_time: dt.datetime
    source_times: Mapping[str, str]
    encoded_source_layer: str
    source_path: str
    source_fetched_at: str


@dataclass(frozen=True)
class ProxyLayerSelection:
    recipe_id: str
    rendered_layer_id: str
    source_key: str
    source_path: str
    source_valid_time: dt.datetime | None
    stage_aligned: bool

    def manifest_entry(self) -> Mapping[str, Any]:
        return {
            "id": self.recipe_id,
            "renderId": self.rendered_layer_id,
            "sourceKey": self.source_key,
            "sourceValidTime": (
                _format_time(self.source_valid_time)
                if self.source_valid_time is not None
                else None
            ),
        }


def _parse_time(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_time(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _frame_source_time(frame: Mapping[str, Any]) -> dt.datetime | None:
    source_times = frame.get("sourceTimes")
    if isinstance(source_times, Mapping):
        parsed = [_parse_time(value) for value in source_times.values()]
        values = [value for value in parsed if value is not None]
        if values:
            return max(values)
    return _parse_time(frame.get("validTime"))


def _at_or_before(
    frames: Sequence[Mapping[str, Any]],
    target: dt.datetime,
    max_age_minutes: int | None = None,
) -> Mapping[str, Any] | None:
    selected: Mapping[str, Any] | None = None
    selected_time: dt.datetime | None = None
    for frame in frames:
        frame_time = _parse_time(frame.get("validTime"))
        if frame_time is None:
            continue
        if frame_time <= target and (selected_time is None or frame_time > selected_time):
            selected = frame
            selected_time = frame_time
    if selected is None or selected_time is None:
        return None
    if max_age_minutes is not None and target - selected_time > dt.timedelta(minutes=max_age_minutes):
        return None
    return selected


def _nearest(
    frames: Sequence[Mapping[str, Any]],
    target: dt.datetime,
    tolerance_minutes: int,
) -> Mapping[str, Any] | None:
    candidates = [
        (abs(frame_time - target), frame)
        for frame in frames
        if (frame_time := _parse_time(frame.get("validTime"))) is not None
    ]
    if not candidates:
        return None
    offset, frame = min(candidates, key=lambda item: item[0])
    return frame if offset <= dt.timedelta(minutes=tolerance_minutes) else None


def _at_or_before_source_time(
    frames: Sequence[Mapping[str, Any]],
    target: dt.datetime,
    max_age_minutes: int | None,
) -> Mapping[str, Any] | None:
    selected: Mapping[str, Any] | None = None
    selected_source_time: dt.datetime | None = None
    selected_source_count = -1
    selected_frame_time: dt.datetime | None = None
    for frame in frames:
        frame_time = _parse_time(frame.get("validTime"))
        source_time = _frame_source_time(frame)
        if frame_time is None or source_time is None or frame_time > target or source_time > target:
            continue
        source_times = frame.get("sourceTimes")
        source_count = len(source_times) if isinstance(source_times, Mapping) else 0
        if (
            selected_source_time is None
            or source_time > selected_source_time
            or (
                source_time == selected_source_time
                and (
                    source_count > selected_source_count
                    or (
                        source_count == selected_source_count
                        and (selected_frame_time is None or frame_time > selected_frame_time)
                    )
                )
            )
        ):
            selected = frame
            selected_source_time = source_time
            selected_source_count = source_count
            selected_frame_time = frame_time
    if selected is None or selected_source_time is None:
        return None
    if max_age_minutes is not None and target - selected_source_time > dt.timedelta(minutes=max_age_minutes):
        return None
    return selected


def _timeline(
    frames: Sequence[Mapping[str, Any]],
    cadence_minutes: int,
    hours: float,
) -> list[dt.datetime]:
    parsed = sorted(
        value
        for frame in frames
        if (value := _parse_time(frame.get("validTime"))) is not None
    )
    if not parsed:
        return []
    interval_seconds = cadence_minutes * 60
    newest_epoch = math.floor(parsed[-1].timestamp() / interval_seconds) * interval_seconds
    newest = dt.datetime.fromtimestamp(newest_epoch, UTC)
    requested_start = newest - dt.timedelta(hours=hours)
    # A scan that starts just after a nominal boundary still belongs to that
    # boundary.  Keep the first such slot on the timeline so the same two-minute
    # tolerance used by source selection can actually choose it.
    available_epoch = (
        math.ceil((parsed[0].timestamp() - 120) / interval_seconds)
        * interval_seconds
    )
    current = max(requested_start, dt.datetime.fromtimestamp(available_epoch, UTC))
    values: list[dt.datetime] = []
    while current <= newest:
        values.append(current)
        current += dt.timedelta(minutes=cadence_minutes)
    return values


def _selected_satellite_frames(
    catalog: Mapping[str, Any],
    spec: ProfileSpec,
    hours: float,
) -> list[SelectedFrame]:
    domain = catalog["domains"][spec.domain_id]
    layers = domain["layers"]
    selection_layer_id = (
        "raw-visir"
        if spec.domain_id == "bc"
        and spec.layer_id == "raw-visir-5min"
        and spec.track == "archive"
        else spec.layer_id
    )
    anchor_layer = layers.get(selection_layer_id, {})
    anchor_frames = list(anchor_layer.get("frames", []))
    if not anchor_frames:
        return []
    standard_frames = list(layers.get("raw-visir", {}).get("frames", []))
    native_frames = list(layers.get("raw-visir-native", {}).get("frames", []))
    broad_max_age_value = anchor_layer.get("maxAgeMinutes")
    broad_max_age = (
        int(broad_max_age_value)
        if isinstance(broad_max_age_value, (int, float))
        else None
    )
    selected: list[SelectedFrame] = []
    for valid_time in _timeline(anchor_frames, spec.cadence_minutes, hours):
        if spec.domain_id == "bc" and spec.layer_id == "raw-visir" and spec.track == "live":
            native = _nearest(native_frames, valid_time, 2) or _at_or_before(
                native_frames, valid_time, 25
            )
            standard = _nearest(standard_frames, valid_time, 2) or _at_or_before(
                standard_frames, valid_time, 90
            )
            candidates = (
                (native, "raw-visir-native", 25),
                (standard, "raw-visir", 90),
            )
        else:
            candidates = (
                (
                    # GOES scan-start timestamps are usually a few seconds
                    # after the nominal clock slot.  Prefer that same-slot
                    # image before falling back to an older image; otherwise
                    # sparse hourly archives skip every ``HH:00:2x`` frame and
                    # continuous feeds display the preceding scan.
                    _nearest(anchor_frames, valid_time, 2)
                    or _at_or_before(anchor_frames, valid_time, broad_max_age),
                    selection_layer_id,
                    broad_max_age,
                ),
            )

        chosen: tuple[
            Mapping[str, Any], str, int | None, dt.datetime, str
        ] | None = None
        for frame, source_layer, source_max_age in candidates:
            if frame is None:
                continue
            source_valid_time = _frame_source_time(frame)
            source_path = str(frame.get("path", ""))
            if source_valid_time is None or not source_path:
                continue
            # Native GOES filenames use the nominal ten-minute slot while
            # metadata preserves the scan start (typically :21 seconds). Treat
            # that sub-slot offset as the same frame, not future imagery.
            if source_valid_time - valid_time > dt.timedelta(minutes=2):
                continue
            if (
                source_max_age is not None
                and valid_time - source_valid_time
                > dt.timedelta(minutes=source_max_age)
            ):
                continue
            if selected and source_valid_time < selected[-1].source_valid_time:
                continue
            chosen = (
                frame,
                source_layer,
                source_max_age,
                source_valid_time,
                source_path,
            )
            break

        if chosen is None:
            if not selected:
                continue
            previous = selected[-1]
            previous_max_age = (
                25
                if previous.encoded_source_layer == "raw-visir-native"
                else 90
                if spec.domain_id == "bc" and spec.layer_id == "raw-visir" and spec.track == "live"
                else broad_max_age
            )
            if (
                previous_max_age is None
                or valid_time - previous.source_valid_time
                <= dt.timedelta(minutes=previous_max_age)
            ):
                selected.append(
                    SelectedFrame(
                        valid_time=valid_time,
                        source_valid_time=previous.source_valid_time,
                        source_times=previous.source_times,
                        encoded_source_layer=previous.encoded_source_layer,
                        source_path=previous.source_path,
                        source_fetched_at=previous.source_fetched_at,
                    )
                )
            continue
        frame, source_layer, _, source_valid_time, source_path = chosen
        raw_source_times = frame.get("sourceTimes")
        source_times = (
            {str(key): str(value) for key, value in raw_source_times.items()}
            if isinstance(raw_source_times, Mapping)
            else {}
        )
        selected.append(
            SelectedFrame(
                valid_time=valid_time,
                source_valid_time=source_valid_time,
                source_times=source_times,
                encoded_source_layer=source_layer,
                source_path=source_path,
                source_fetched_at=str(frame.get("fetchedAt", "")),
            )
        )
    return selected


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Unsafe relative asset path: {relative!r}")
    resolved_root = root.resolve()
    candidate = (resolved_root / path).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Asset path escapes source root: {relative!r}")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _source_url_key(frame: Mapping[str, Any]) -> str:
    path = str(frame.get("path", ""))
    return f"{path}?{urlencode({'v': str(frame.get('fetchedAt', ''))})}"


def _static_url_key(layer: Mapping[str, Any]) -> str:
    path = str(layer.get("path", ""))
    return f"{path}?{urlencode({'v': str(layer.get('revision', ''))})}"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_payload(payload: object, length: int = 12) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:length]


def _source_fingerprint(root: Path, frame: SelectedFrame) -> Mapping[str, Any]:
    path = _safe_path(root, frame.source_path)
    stat = path.stat()
    return {
        "validTime": _format_time(frame.valid_time),
        "sourceValidTime": _format_time(frame.source_valid_time),
        "sourcePath": frame.source_path,
        "sourceFetchedAt": frame.source_fetched_at,
        "encodedSourceLayer": frame.encoded_source_layer,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
    }


def _crop_resize(
    image: Image.Image,
    viewport: Mapping[str, float],
    width: int,
    height: int,
    *,
    stage_aligned: bool = False,
) -> Image.Image:
    rgba = image.convert("RGBA")
    if not stage_aligned:
        left = round(float(viewport["left"]) * rgba.width)
        top = round(float(viewport["top"]) * rgba.height)
        right = round((float(viewport["left"]) + float(viewport["width"])) * rgba.width)
        bottom = round((float(viewport["top"]) + float(viewport["height"])) * rgba.height)
        if right <= left or bottom <= top:
            raise ValueError("Viewport produced an empty source crop")
        rgba = rgba.crop((left, top, right, bottom))
    if rgba.size != (width, height):
        rgba = rgba.resize((width, height), Image.Resampling.LANCZOS)
    return rgba


def _render_proxy(
    source_root: Path,
    output_root: Path,
    spec: ProfileSpec,
    rendered_layer_id: str,
    source_key: str,
    source_path: str,
    *,
    stage_aligned: bool,
) -> Mapping[str, Any]:
    source = _safe_path(source_root, source_path)
    stat = source.stat()
    source_fingerprint = _hash_payload(
        {
            "version": PROXY_RENDER_VERSION,
            "productId": spec.product_id,
            "sourceKey": source_key,
            "sourceSize": stat.st_size,
            "sourceMtimeNs": stat.st_mtime_ns,
            "viewport": dict(spec.viewport),
            "width": spec.width,
            "height": spec.height,
            "stageAligned": stage_aligned,
        },
        16,
    )
    alias = (
        output_root
        / "video-proxy-index"
        / spec.product_id
        / rendered_layer_id
        / f"{source_fingerprint}.json"
    )
    try:
        cached = json.loads(alias.read_text())
        cached_relative = str(cached["path"])
        cached_path = _safe_path(output_root, cached_relative)
        if (
            int(cached.get("width", 0)) == spec.width
            and int(cached.get("height", 0)) == spec.height
        ):
            return {
                "path": cached_relative,
                "width": spec.width,
                "height": spec.height,
                "byteLength": cached_path.stat().st_size,
            }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass

    with Image.open(source) as image:
        rendered = _crop_resize(
            image,
            spec.viewport,
            spec.width,
            spec.height,
            stage_aligned=stage_aligned,
        )
    content_hash = hashlib.sha256(
        b"radarsat-lossless-webp-v1\0"
        + spec.width.to_bytes(4, "big")
        + spec.height.to_bytes(4, "big")
        + rendered.tobytes()
    ).hexdigest()[:16]
    destination = (
        output_root
        / "video-proxies"
        / spec.product_id
        / rendered_layer_id
        / f"{content_hash}.webp"
    )
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp.webp")
        try:
            rendered.save(
                temporary,
                "WEBP",
                lossless=True,
                quality=100,
                method=2,
                exact=True,
            )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    entry = {
        "path": destination.relative_to(output_root).as_posix(),
        "width": spec.width,
        "height": spec.height,
        "byteLength": destination.stat().st_size,
    }
    _atomic_json(alias, entry)
    return entry


def _product(product_id: str) -> Mapping[str, Any]:
    return next(product for product in PRODUCTS if product["id"] == product_id)


def _rendered_layer_id(
    recipe_id: str,
    spec: ProfileSpec,
    domain: Mapping[str, Any],
) -> str:
    archive_recipe_id = (
        {
            "lightning-trail": "lightning-hour",
            "glm-lightning-trail": "glm-lightning-hour",
        }.get(recipe_id, recipe_id)
        if spec.track == "archive"
        else recipe_id
    )
    if archive_recipe_id == "model-hgt500":
        base_id = "hrdps-hgt500" if spec.domain_id == "bc" else "ecmwf-hgt500"
    elif archive_recipe_id == "model-mslp":
        base_id = "hrdps-mslp" if spec.domain_id == "bc" else "ecmwf-mslp"
    else:
        base_id = archive_recipe_id
    region = REGIONAL_PRODUCT_KEYS.get(spec.product_id)
    if region and base_id in REGIONAL_LAYER_BASES:
        candidate = f"{base_id}-region-{region}"
        if domain.get("layers", {}).get(candidate, {}).get("frames"):
            return candidate
    return base_id


def _proxy_selections(
    catalog: Mapping[str, Any],
    spec: ProfileSpec,
    selected_frames: Sequence[SelectedFrame],
) -> list[list[ProxyLayerSelection]]:
    """Select each frame's overlays using the viewer's catalog semantics."""
    domain = catalog["domains"][spec.domain_id]
    product = _product(spec.product_id)
    results: list[list[ProxyLayerSelection]] = []
    for selected_frame in selected_frames:
        anchor = selected_frame.valid_time
        frame_results: list[ProxyLayerSelection] = []
        for recipe in product.get("layers", []):
            recipe_id = str(recipe.get("id", ""))
            if (
                not recipe_id
                or recipe_id == "base-dark"
                or recipe_id in SATELLITE_LAYER_IDS
            ):
                continue
            static = domain.get("staticLayers", {}).get(recipe_id)
            if isinstance(static, Mapping) and static.get("path"):
                frame_results.append(
                    ProxyLayerSelection(
                        recipe_id=recipe_id,
                        rendered_layer_id=recipe_id,
                        source_key=_static_url_key(static),
                        source_path=str(static["path"]),
                        source_valid_time=None,
                        stage_aligned=False,
                    )
                )
                continue
            rendered_id = _rendered_layer_id(recipe_id, spec, domain)
            layer = domain.get("layers", {}).get(rendered_id)
            if not isinstance(layer, Mapping):
                continue
            frames = list(layer.get("frames", []))
            max_age = layer.get("maxAgeMinutes")
            max_age_minutes = (
                int(max_age) if isinstance(max_age, (int, float)) else None
            )
            if "lightning" in rendered_id:
                frame = _at_or_before_source_time(frames, anchor, max_age_minutes)
            else:
                frame = _at_or_before(frames, anchor, max_age_minutes)
            if frame is None or not frame.get("path"):
                continue
            key = _source_url_key(frame)
            frame_results.append(
                ProxyLayerSelection(
                    recipe_id=recipe_id,
                    rendered_layer_id=rendered_id,
                    source_key=key,
                    source_path=str(frame["path"]),
                    source_valid_time=_frame_source_time(frame),
                    stage_aligned="-region-" in rendered_id,
                )
            )
        results.append(frame_results)
    return results


def _frame_durations(
    selected: Sequence[SelectedFrame], cadence_minutes: int
) -> list[float]:
    if not selected:
        return []
    nominal = cadence_minutes * METEOROLOGICAL_MINUTE_SECONDS
    durations: list[float] = []
    for current, following in zip(selected, selected[1:]):
        minutes = (following.valid_time - current.valid_time).total_seconds() / 60
        durations.append(max(1 / FFCONCAT_TIMEBASE_FPS, minutes * METEOROLOGICAL_MINUTE_SECONDS))
    durations.append(nominal)
    return durations


def _prepare_satellite_images(
    source_root: Path,
    spec: ProfileSpec,
    selected: Sequence[SelectedFrame],
    temporary_root: Path,
) -> list[Path]:
    base_path = _safe_path(
        source_root,
        f"static/{spec.domain_id}/base-dark.png",
    )
    with Image.open(base_path) as base_image:
        base = _crop_resize(
            base_image,
            spec.resolved_media_viewport,
            spec.resolved_media_width,
            spec.resolved_media_height,
        ).convert("RGB")
    rendered: dict[tuple[str, str], Path] = {}
    paths: list[Path] = []
    for frame in selected:
        key = (frame.source_path, frame.source_fetched_at)
        destination = rendered.get(key)
        if destination is None:
            destination = temporary_root / f"sat-{len(rendered):04d}.png"
            with Image.open(_safe_path(source_root, frame.source_path)) as source_image:
                satellite = _crop_resize(
                    source_image,
                    spec.resolved_media_viewport,
                    spec.resolved_media_width,
                    spec.resolved_media_height,
                )
                composed = base.copy()
                composed.paste(satellite.convert("RGB"), (0, 0), satellite.getchannel("A"))
                composed.save(destination, "PNG", optimize=False, compress_level=1)
            rendered[key] = destination
        paths.append(destination)
    return paths


def _ffconcat_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if "'" in value or "\n" in value or "\r" in value:
        raise ValueError(f"Unsupported ffconcat path: {value!r}")
    return value


def _encode_mp4(
    ffmpeg: str,
    images: Sequence[Path],
    durations: Sequence[float],
    destination: Path,
    spec: ProfileSpec,
) -> None:
    if len(images) != len(durations) or not images:
        raise ValueError("Video encoding requires matching non-empty images and durations")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp.mp4")
    with tempfile.TemporaryDirectory(prefix="radarsat-video-concat-") as temporary_directory:
        concat = Path(temporary_directory) / "frames.ffconcat"
        lines = ["ffconcat version 1.0"]
        for image, duration in zip(images, durations, strict=True):
            lines.extend(
                (
                    f"file '{_ffconcat_path(image)}'",
                    f"option framerate {FFCONCAT_TIMEBASE_FPS}",
                    f"duration {duration:.6f}",
                )
            )
        # ffconcat needs the next packet to materialize the preceding still's
        # declared duration.  This duplicate end sentinel begins exactly at
        # the loop end, so the final meteorological frame remains visible for
        # its full duration without adding a frame to the public timeline.
        lines.extend(
            (
                f"file '{_ffconcat_path(images[-1])}'",
                f"option framerate {FFCONCAT_TIMEBASE_FPS}",
            )
        )
        concat.write_text("\n".join(lines) + "\n")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            spec.preset,
            "-crf",
            str(spec.crf),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "vfr",
            "-enc_time_base",
            "-1",
            "-x264-params",
            (
                f"keyint={spec.gop_frames}:min-keyint={spec.gop_frames}:"
                "scenecut=0:force-cfr=0:colorprim=bt709:transfer=bt709:"
                "colormatrix=bt709"
            ),
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-movflags",
            "+faststart",
            "-video_track_timescale",
            "1000",
            "-y",
            str(temporary),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RuntimeError("ffmpeg produced no MP4 output")
            temporary.replace(destination)
        except subprocess.CalledProcessError as error:
            details = (error.stderr or error.stdout or "").strip()
            raise RuntimeError(f"ffmpeg failed: {details[-2000:]}") from error
        finally:
            temporary.unlink(missing_ok=True)


def _encode_ts(
    ffmpeg: str,
    images: Sequence[Path],
    durations: Sequence[float],
    destination: Path,
    spec: ProfileSpec,
) -> None:
    if len(images) != len(durations) or not images:
        raise ValueError("Segment encoding requires matching non-empty images and durations")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp.ts")
    with tempfile.TemporaryDirectory(prefix="radarsat-video-segment-") as temporary_directory:
        concat = Path(temporary_directory) / "frames.ffconcat"
        lines = ["ffconcat version 1.0"]
        for image, duration in zip(images, durations, strict=True):
            lines.extend(
                (
                    f"file '{_ffconcat_path(image)}'",
                    f"option framerate {FFCONCAT_TIMEBASE_FPS}",
                    f"duration {duration:.6f}",
                )
            )
        lines.extend(
            (
                f"file '{_ffconcat_path(images[-1])}'",
                f"option framerate {FFCONCAT_TIMEBASE_FPS}",
            )
        )
        concat.write_text("\n".join(lines) + "\n")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            spec.preset,
            "-crf",
            str(spec.crf),
            "-profile:v",
            "high",
            # Each immutable HLS segment is intentionally short and uses a
            # sparse variable-frame-rate weather clock. B-frame reordering can
            # push presentation timestamps beyond a segment's declared HLS
            # duration (especially for one- to three-frame hourly groups),
            # leaving MediaSource buffered but unable to present a frame.
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "vfr",
            "-enc_time_base",
            "-1",
            "-x264-params",
            (
                f"keyint={len(images)}:min-keyint={len(images)}:"
                "scenecut=0:bframes=0:force-cfr=0:colorprim=bt709:transfer=bt709:colormatrix=bt709"
            ),
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-mpegts_flags",
            "+initial_discontinuity",
            "-f",
            "mpegts",
            "-y",
            str(temporary),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RuntimeError("ffmpeg produced no MPEG-TS output")
            temporary.replace(destination)
        except subprocess.CalledProcessError as error:
            details = (error.stderr or error.stdout or "").strip()
            raise RuntimeError(f"ffmpeg failed: {details[-2000:]}") from error
        finally:
            temporary.unlink(missing_ok=True)


def _segment_groups(
    selected: Sequence[SelectedFrame],
    track: str,
) -> list[list[int]]:
    grouped: dict[dt.datetime, list[int]] = {}
    for index, frame in enumerate(selected):
        valid = frame.valid_time.astimezone(UTC)
        group_hours = 1 if track == "live" else 6
        key = valid.replace(
            hour=valid.hour - valid.hour % group_hours,
            minute=0,
            second=0,
            microsecond=0,
        )
        grouped.setdefault(key, []).append(index)
    return [grouped[key] for key in sorted(grouped)]


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _build_hls_media(
    source_root: Path,
    output_root: Path,
    spec: ProfileSpec,
    selected: Sequence[SelectedFrame],
    media_inputs: Sequence[Mapping[str, Any]],
    durations: Sequence[float],
    *,
    ffmpeg: str,
) -> tuple[Path, str, list[Mapping[str, Any]], list[float]]:
    owner = f"shared-{spec.domain_id}-{spec.media_group}"
    groups = _segment_groups(selected, spec.track)
    adjusted_durations = list(durations)
    segment_entries: list[Mapping[str, Any]] = []
    for indexes in groups:
        # The concat demuxer emits a final 1/50-second sentinel to materialize
        # the preceding still's declared duration. Account for it explicitly
        # so the HLS and meteorological clocks stay aligned across segments.
        adjusted_durations[indexes[-1]] += 1 / FFCONCAT_TIMEBASE_FPS
        segment_fingerprint = _hash_payload(
            {
                "videoRenderVersion": VIDEO_RENDER_VERSION,
                "domainId": spec.domain_id,
                "layerId": spec.layer_id,
                "track": spec.track,
                "mediaGroup": spec.media_group,
                "mediaViewport": dict(spec.resolved_media_viewport),
                "mediaWidth": spec.resolved_media_width,
                "mediaHeight": spec.resolved_media_height,
                "crf": spec.crf,
                "preset": spec.preset,
                "frames": [media_inputs[index] for index in indexes],
                "durations": [durations[index] for index in indexes],
            },
            16,
        )
        start_stamp = selected[indexes[0]].valid_time.strftime("%Y%m%dT%H%MZ")
        segment_path = (
            output_root
            / "video-segments"
            / owner
            / spec.layer_id
            / spec.track
            / f"{start_stamp}-{segment_fingerprint}.ts"
        )
        if not segment_path.is_file():
            with tempfile.TemporaryDirectory(prefix=f"radarsat-{owner}-segment-") as temporary:
                group_frames = [selected[index] for index in indexes]
                prepared = _prepare_satellite_images(
                    source_root,
                    spec,
                    group_frames,
                    Path(temporary),
                )
                _encode_ts(
                    ffmpeg,
                    prepared,
                    [durations[index] for index in indexes],
                    segment_path,
                    spec,
                )
        segment_entries.append(
            {
                "path": segment_path.relative_to(output_root).as_posix(),
                "byteLength": segment_path.stat().st_size,
                "sha256": _sha256_file(segment_path),
                "durationSeconds": round(
                    sum(adjusted_durations[index] for index in indexes), 6
                ),
                "firstFrame": indexes[0],
                "lastFrame": indexes[-1],
            }
        )
    playlist_fingerprint = _hash_payload(
        {
            "videoRenderVersion": VIDEO_RENDER_VERSION,
            "segments": segment_entries,
        }
    )
    end_stamp = selected[-1].valid_time.strftime("%Y%m%dT%H%MZ")
    playlist_path = (
        output_root
        / "videos"
        / owner
        / spec.layer_id
        / spec.track
        / f"{end_stamp}-{playlist_fingerprint}.m3u8"
    )
    if not playlist_path.is_file():
        target_duration = max(
            1,
            math.ceil(max(float(entry["durationSeconds"]) for entry in segment_entries)),
        )
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for index, entry in enumerate(segment_entries):
            if index:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{float(entry['durationSeconds']):.6f},")
            segment_absolute = output_root / str(entry["path"])
            lines.append(os.path.relpath(segment_absolute, playlist_path.parent))
        lines.append("#EXT-X-ENDLIST")
        _atomic_text(playlist_path, "\n".join(lines) + "\n")
    return playlist_path, playlist_fingerprint, segment_entries, adjusted_durations


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _manifest_dependencies(output_root: Path, manifests: Iterable[Path]) -> set[Path]:
    dependencies: set[Path] = set()
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        media = manifest.get("media")
        if isinstance(media, Mapping) and media.get("path"):
            try:
                dependencies.add(_safe_path(output_root, str(media["path"])))
            except (ValueError, FileNotFoundError):
                pass
            segments = media.get("segments")
            if isinstance(segments, list):
                for segment in segments:
                    if not isinstance(segment, Mapping) or not segment.get("path"):
                        continue
                    try:
                        dependencies.add(_safe_path(output_root, str(segment["path"])))
                    except (ValueError, FileNotFoundError):
                        pass
        proxies = manifest.get("proxies")
        values = proxies.values() if isinstance(proxies, Mapping) else []
        for proxy in values:
            if not isinstance(proxy, Mapping) or not proxy.get("path"):
                continue
            try:
                dependencies.add(_safe_path(output_root, str(proxy["path"])))
            except (ValueError, FileNotFoundError):
                pass
    return dependencies


def prune_local_video_orphans(
    output_root: Path,
    product_id: str,
    *,
    now: dt.datetime | None = None,
) -> Mapping[str, int]:
    current = (now or dt.datetime.now(UTC)).timestamp()
    grace_seconds = LOCAL_ORPHAN_GRACE_HOURS * 3600
    manifest_root = output_root / "video-manifests" / product_id
    kept_manifests: list[Path] = []
    removed_manifests = 0
    if manifest_root.exists():
        for track_directory in sorted(path for path in manifest_root.rglob("*") if path.is_dir()):
            manifests = sorted(
                track_directory.glob("*.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            if not manifests:
                continue
            for index, manifest in enumerate(manifests):
                age = current - manifest.stat().st_mtime
                if index < LOCAL_GENERATIONS_TO_KEEP or age <= grace_seconds:
                    kept_manifests.append(manifest)
                else:
                    manifest.unlink(missing_ok=True)
                    removed_manifests += 1
    dependencies = _manifest_dependencies(output_root, kept_manifests)
    all_manifest_dependencies = _manifest_dependencies(
        output_root,
        (output_root / "video-manifests").rglob("*.json"),
    )
    removed_dependencies = 0
    for prefix in ("videos", "video-proxies"):
        root = output_root / prefix / product_id
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path in dependencies:
                continue
            if current - path.stat().st_mtime <= grace_seconds:
                continue
            path.unlink(missing_ok=True)
            removed_dependencies += 1
    videos_root = output_root / "videos"
    if videos_root.exists():
        for shared_root in videos_root.glob("shared-*"):
            if not shared_root.is_dir():
                continue
            for path in shared_root.rglob("*"):
                if not path.is_file():
                    continue
                if path in all_manifest_dependencies:
                    continue
                if current - path.stat().st_mtime <= grace_seconds:
                    continue
                path.unlink(missing_ok=True)
                removed_dependencies += 1
    segment_root = output_root / "video-segments"
    if segment_root.exists():
        for path in segment_root.rglob("*.ts"):
            if path in all_manifest_dependencies:
                continue
            if current - path.stat().st_mtime <= grace_seconds:
                continue
            path.unlink(missing_ok=True)
            removed_dependencies += 1
    return {
        "removedManifests": removed_manifests,
        "removedDependencies": removed_dependencies,
    }


def build_profile(
    source_root: Path,
    output_root: Path,
    catalog: Mapping[str, Any],
    spec: ProfileSpec,
    *,
    ffmpeg: str,
    hours: float = 24.0,
    now: dt.datetime | None = None,
) -> Mapping[str, Any]:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    selected = _selected_satellite_frames(catalog, spec, hours)
    # Catalog construction and retention are independent workers. A frame can
    # legitimately age out between the catalog snapshot and this low-priority
    # video build, so omit vanished inputs instead of losing the whole profile.
    selected = [
        frame
        for frame in selected
        if (source_root / frame.source_path).is_file()
    ]
    if len(selected) < 2:
        raise RuntimeError(f"{spec.product_id} has fewer than two usable satellite frames")
    media_inputs = [_source_fingerprint(source_root, frame) for frame in selected]
    media_path, media_fingerprint, segment_entries, durations = _build_hls_media(
        source_root,
        output_root,
        spec,
        selected,
        media_inputs,
        _frame_durations(selected, spec.cadence_minutes),
        ffmpeg=ffmpeg,
    )
    end_stamp = selected[-1].valid_time.strftime("%Y%m%dT%H%MZ")

    proxy_selections = _proxy_selections(catalog, spec, selected)
    unique_proxy_sources: dict[str, ProxyLayerSelection] = {}
    for frame_selections in proxy_selections:
        for selection in frame_selections:
            unique_proxy_sources.setdefault(selection.source_key, selection)

    proxy_entries: dict[str, Mapping[str, Any]] = {}
    proxy_warnings: list[Mapping[str, str]] = []
    for source_key, selection in unique_proxy_sources.items():
        try:
            proxy_entries[source_key] = _render_proxy(
                source_root,
                output_root,
                spec,
                selection.rendered_layer_id,
                source_key,
                selection.source_path,
                stage_aligned=selection.stage_aligned,
            )
        except FileNotFoundError:
            # The ingest process can prune or replace an overlay after the catalog
            # snapshot is taken.  A missing optional overlay must not prevent a
            # fresh satellite video (or replace the last-good pointer).  The
            # frontend can still use the original asset URL if it remains in R2.
            proxy_warnings.append(
                {
                    "sourceKey": source_key,
                    "sourcePath": selection.source_path,
                    "renderedLayerId": selection.rendered_layer_id,
                    "reason": "source-disappeared-during-build",
                }
            )

    proxy_layer_entries = [
        [
            selection.manifest_entry()
            for selection in frame_selections
            if selection.source_key in proxy_entries
        ]
        for frame_selections in proxy_selections
    ]

    generation_fingerprint = _hash_payload(
        {
            "mediaFingerprint": media_fingerprint,
            "proxies": proxy_entries,
            "proxyLayers": proxy_layer_entries,
            "proxyRenderVersion": PROXY_RENDER_VERSION,
        }
    )
    generation = f"{end_stamp}-{generation_fingerprint}"
    manifest_path = (
        output_root
        / "video-manifests"
        / spec.product_id
        / spec.layer_id
        / spec.track
        / f"{generation}.json"
    )
    index_path = output_root / "video-index" / spec.product_id / f"{spec.layer_id}.json"
    current_index = _load_index(index_path)
    current_profile = current_index.get("profiles", {}).get(spec.track, {})
    if (
        current_profile.get("generation") == generation
        and current_profile.get("manifestPath") == manifest_path.relative_to(output_root).as_posix()
        and manifest_path.is_file()
        and media_path.is_file()
    ):
        return {
            "status": "unchanged",
            "productId": spec.product_id,
            "layerId": spec.layer_id,
            "track": spec.track,
            "generation": generation,
            "manifestPath": manifest_path.relative_to(output_root).as_posix(),
            "mediaPath": media_path.relative_to(output_root).as_posix(),
            "frames": len(selected),
            "proxies": len(proxy_entries),
            "proxyWarnings": len(proxy_warnings),
        }

    pts = 0.0
    frame_manifest: list[dict[str, Any]] = []
    for index, (frame, duration, frame_proxy_layers) in enumerate(
        zip(selected, durations, proxy_layer_entries, strict=True)
    ):
        frame_manifest.append(
            {
                "index": index,
                "validTime": _format_time(frame.valid_time),
                "sourceValidTime": _format_time(frame.source_valid_time),
                "sourceTimes": dict(frame.source_times),
                "encodedSourceLayer": frame.encoded_source_layer,
                "sourcePath": frame.source_path,
                "sourceFetchedAt": frame.source_fetched_at,
                "ptsSeconds": round(pts, 6),
                "durationSeconds": round(duration, 6),
                "proxyLayers": frame_proxy_layers,
            }
        )
        pts += duration
    generated_at = _format_time(now or dt.datetime.now(UTC))
    manifest: dict[str, Any] = {
        "schemaVersion": VIDEO_SCHEMA_VERSION,
        "generation": generation,
        "generatedAt": generated_at,
        "productId": spec.product_id,
        "domainId": spec.domain_id,
        "layerId": spec.layer_id,
        "track": spec.track,
        "transport": "hls-ts",
        "cadenceMinutes": spec.cadence_minutes,
        "width": spec.width,
        "height": spec.height,
        "viewport": dict(spec.viewport),
        "mediaViewport": dict(spec.resolved_media_viewport),
        "media": {
            "path": media_path.relative_to(output_root).as_posix(),
            "mimeType": "application/vnd.apple.mpegurl",
            "codec": "avc1",
            "width": spec.resolved_media_width,
            "height": spec.resolved_media_height,
            "byteLength": media_path.stat().st_size,
            "sha256": _sha256_file(media_path),
            "fingerprint": media_fingerprint,
            "segments": segment_entries,
        },
        "frames": frame_manifest,
        "proxies": proxy_entries,
    }
    if proxy_warnings:
        manifest["proxyWarnings"] = proxy_warnings
    if not manifest_path.is_file():
        _atomic_json(manifest_path, manifest)

    profiles = dict(current_index.get("profiles", {}))
    profiles[spec.track] = {
        "generation": generation,
        "manifestPath": manifest_path.relative_to(output_root).as_posix(),
    }
    pointer = {
        "schemaVersion": VIDEO_SCHEMA_VERSION,
        "productId": spec.product_id,
        "layerId": spec.layer_id,
        "updatedAt": generated_at,
        "profiles": profiles,
    }
    _atomic_json(index_path, pointer)
    prune_result = prune_local_video_orphans(output_root, spec.product_id, now=now)
    return {
        "status": "built",
        "productId": spec.product_id,
        "layerId": spec.layer_id,
        "track": spec.track,
        "generation": generation,
        "manifestPath": manifest_path.relative_to(output_root).as_posix(),
        "mediaPath": media_path.relative_to(output_root).as_posix(),
        "mediaBytes": media_path.stat().st_size
        + sum(int(entry["byteLength"]) for entry in segment_entries),
        "segments": len(segment_entries),
        "frames": len(selected),
        "proxies": len(proxy_entries),
        "proxyWarnings": len(proxy_warnings),
        **prune_result,
    }


def build_satellite_videos(
    source_root: Path,
    output_root: Path | None = None,
    *,
    product_ids: Iterable[str] | None = None,
    track_names: Iterable[str] | None = None,
    ffmpeg: str | None = None,
    hours: float = 24.0,
    archive_hours: float = 168.0,
    now: dt.datetime | None = None,
) -> Mapping[str, Any]:
    if hours <= 0 or archive_hours <= 0:
        raise ValueError("hours and archive_hours must be positive")
    source_root = source_root.resolve()
    output_root = (output_root or source_root).resolve()
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg with libx264 is required to build satellite video")
    requested = set(product_ids or (spec.product_id for spec in VIDEO_PROFILES))
    unknown = requested.difference(spec.product_id for spec in VIDEO_PROFILES)
    if unknown:
        raise ValueError(f"Unsupported video products: {sorted(unknown)}")
    requested_tracks = set(track_names or ("live", "archive"))
    unknown_tracks = requested_tracks.difference({"live", "archive"})
    if unknown_tracks:
        raise ValueError(f"Unsupported video tracks: {sorted(unknown_tracks)}")
    catalog = build_catalog(source_root)
    results: list[Mapping[str, Any]] = []
    failures: list[Mapping[str, str]] = []
    for spec in VIDEO_PROFILES:
        if spec.product_id not in requested:
            continue
        if spec.track not in requested_tracks:
            continue
        try:
            results.append(
                build_profile(
                    source_root,
                    output_root,
                    catalog,
                    spec,
                    ffmpeg=executable,
                    hours=min(hours, 24.0) if spec.track == "live" else min(archive_hours, 168.0),
                    now=now,
                )
            )
        except Exception as error:
            failures.append(
                {
                    "productId": spec.product_id,
                    "layerId": spec.layer_id,
                    "track": spec.track,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return {
        "status": "warning" if failures else "ok",
        "sourceRoot": str(source_root),
        "outputRoot": str(output_root),
        "profiles": results,
        "failures": failures,
    }
