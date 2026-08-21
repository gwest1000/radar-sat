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
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from PIL import Image

from .catalog import build_catalog
from .config import (
    BROAD_VIEWPORTS,
    PRODUCTS,
    VIDEO_ARCHIVE_PRODUCTS,
    VIDEO_COMPOSITE_PRESETS,
    VIDEO_EXACT_RANGES,
    VIDEO_TRACKS_BY_PRODUCT,
    VIEWPORTS,
)


UTC = dt.timezone.utc
VIDEO_SCHEMA_VERSION = 2
VIDEO_RENDER_VERSION = 17
PROXY_RENDER_VERSION = 1
COMPOSITE_RENDER_VERSION = 2
METEOROLOGICAL_MINUTE_SECONDS = 0.02
FFCONCAT_TIMEBASE_FPS = 50
# A 10-minute weather step lasts 0.2 media seconds, so 5 fps preserves every
# source timestep exactly. Higher CFR values only repeat identical pictures
# and multiply client decode work, especially at 4x playback.
VIDEO_FRAME_RATE = 5
VIDEO_CLOCK_STRIP_HEIGHT = 16
MPEGTS_TIMESTAMP_WRAP_SECONDS = (1 << 33) / 90_000
LOCAL_GENERATIONS_TO_KEEP = 2
LOCAL_ORPHAN_GRACE_HOURS = 0.25


class ProxySourceUnreadableError(OSError):
    """An optional source image could not be decoded for a video proxy."""

SATELLITE_LAYER_IDS = frozenset(
    {
        "raw-visir",
        "raw-visir-5min",
        "raw-visir-native",
        "raw-ir",
        "westwx-visir",
        "westwx-ir",
        "eccc-geocolor",
        "convective",
        "snowfog",
    }
)
REGIONAL_LAYER_BASES = frozenset(
    {
        "radar-rain",
        "lightning-trail",
        "lightning-hour",
        "hotspots",
        "hrdps-hgt500",
        "hrdps-mslp",
    }
)
REGIONAL_STATIC_BASES = frozenset({"watersheds"})
REGIONAL_PRODUCT_KEYS = {
    "bc-small-overlay": "small",
    "bc-southwest-overlay": "southwest",
    "bc-southeast-overlay": "southeast",
    "bc-northeast-overlay": "northeast",
    "bc-south-coast-overlay": "south-coast",
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
    elif track in {"live", "day"} and layer_id == "eccc-geocolor" and source_width > 2400:
        source_height = _even(source_height * 2400 / source_width)
        source_width = 2400
    if (
        domain_id == "bc"
        and layer_id in {"raw-visir", "eccc-geocolor"}
        and track in {"live", "day"}
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
        and track in {"live", "day"}
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
        and layer_id in {"raw-visir", "eccc-geocolor"}
        and track in {"live", "day"}
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
                ("day", int(product.get("dayFrameIntervalMinutes", 30))),
                ("archive", int(product.get("archiveFrameIntervalMinutes", 60))),
            ):
                if track not in VIDEO_TRACKS_BY_PRODUCT.get(product_id, ("live",)):
                    continue
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
    *,
    now: dt.datetime | None = None,
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
    msc_first = spec.domain_id == "bc" and spec.layer_id == "eccc-geocolor"
    standard_frames = list(layers.get("raw-visir", {}).get("frames", []))
    native_frames = list(layers.get("raw-visir-native", {}).get("frames", []))
    timeline_frames = (
        [*anchor_frames, *standard_frames, *native_frames]
        if msc_first
        else anchor_frames
    )
    if not timeline_frames:
        return []
    broad_max_age_value = anchor_layer.get("maxAgeMinutes")
    broad_max_age = (
        int(broad_max_age_value)
        if isinstance(broad_max_age_value, (int, float))
        else None
    )
    selected: list[SelectedFrame] = []
    reference_now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    for valid_time in _timeline(timeline_frames, spec.cadence_minutes, hours):
        if msc_first:
            msc = _nearest(anchor_frames, valid_time, 2)
            if msc is not None:
                candidates = ((msc, "eccc-geocolor", 35),)
            elif reference_now - valid_time >= dt.timedelta(minutes=35):
                native = _nearest(native_frames, valid_time, 2)
                standard = _nearest(standard_frames, valid_time, 2)
                # NOAA is an exceptional, same-slot fill.  Prefer its native
                # rendition when available, then the standard full-domain
                # frame.  We never substitute an older NOAA scan merely to
                # make the timeline appear complete.
                candidates = (
                    (native, "raw-visir-native", 2),
                    (standard, "raw-visir", 2),
                )
            else:
                # MSC is still inside its accepted real-time latency window.
                # Keep the previous real observation as the live endpoint
                # rather than inventing a held duplicate presentation frame.
                continue
        elif (
            spec.domain_id == "bc"
            and spec.layer_id == "raw-visir"
            and spec.track in {"live", "day"}
        ):
            native = _nearest(native_frames, valid_time, 2) or _at_or_before(
                native_frames, valid_time, 25
            )
            standard = _nearest(standard_frames, valid_time, 2) or _at_or_before(
                standard_frames, valid_time, 90
            )
            # At the live edge, recency wins.  Native STAR/CIRA imagery is
            # preferred only when it represents the same scan as the standard
            # NOAA frame; later rebuilds naturally replace that historical
            # slot with native resolution once it arrives.
            candidates = tuple(sorted(
                (
                    (native, "raw-visir-native", 25),
                    (standard, "raw-visir", 90),
                ),
                key=lambda candidate: (
                    _frame_source_time(candidate[0]) or dt.datetime.min.replace(tzinfo=UTC)
                    if candidate[0] is not None
                    else dt.datetime.min.replace(tzinfo=UTC),
                    candidate[1] == "raw-visir-native",
                ),
                reverse=True,
            ))
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
            # MSC playback is intentionally exact-slot only. If neither MSC
            # nor the deadline-qualified NOAA fill exists for this slot, omit
            # it instead of disguising an older satellite observation as a
            # new frame. This matches the browser's live-edge selector.
            if msc_first:
                continue
            if not selected:
                continue
            previous = selected[-1]
            previous_max_age = (
                None
                if spec.track == "archive"
                else 25
                if previous.encoded_source_layer == "raw-visir-native"
                else 90
                if spec.domain_id == "bc"
                and spec.layer_id == "raw-visir"
                and spec.track in {"live", "day"}
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

    try:
        with Image.open(source) as image:
            rendered = _crop_resize(
                image,
                spec.viewport,
                spec.width,
                spec.height,
                stage_aligned=stage_aligned,
            )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ProxySourceUnreadableError(str(source)) from error
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


def _composite_layer_ids(
    spec: ProfileSpec,
    optional_layer_ids: Iterable[str],
) -> tuple[str, ...]:
    """Resolve an exact operational recipe stack from product configuration."""
    product = _product(spec.product_id)
    recipes = list(product.get("layers", []))
    requested = set(optional_layer_ids)
    selected: list[str] = []
    for recipe in recipes:
        recipe_id = str(recipe.get("id", ""))
        if not recipe_id:
            continue
        enabled_with = str(recipe.get("enabledWith", ""))
        if enabled_with:
            if enabled_with in requested:
                selected.append(recipe_id)
            continue
        if recipe_id in SATELLITE_LAYER_IDS:
            if recipe_id == spec.layer_id:
                selected.append(recipe_id)
            continue
        if recipe.get("optional") and recipe_id not in requested:
            continue
        selected.append(recipe_id)
    return tuple(selected)


def _composite_presets(spec: ProfileSpec) -> tuple[tuple[str, tuple[str, ...]], ...]:
    product = _product(spec.product_id)
    if not any(
        str(recipe.get("id", "")) == spec.layer_id
        and bool(recipe.get("defaultEnabled", False))
        for recipe in product.get("layers", [])
    ):
        return ()
    values = VIDEO_COMPOSITE_PRESETS.get(spec.product_id, ())
    return tuple(
        (
            str(value["id"]),
            _composite_layer_ids(
                spec,
                tuple(str(item) for item in value.get("optionalLayers", ())),
            ),
        )
        for value in values
    )


def _recipe_opacities(product_id: str) -> Mapping[str, float]:
    return {
        str(recipe.get("id", "")): float(recipe.get("opacity", 1.0))
        for recipe in _product(product_id).get("layers", [])
    }


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
    cache: dict[
        tuple[str, str, dt.datetime], tuple[ProxyLayerSelection, ...]
    ] | None = None,
) -> list[list[ProxyLayerSelection]]:
    """Select each frame's overlays using the viewer's catalog semantics."""
    domain = catalog["domains"][spec.domain_id]
    product = _product(spec.product_id)
    prepared: list[
        tuple[
            ProxyLayerSelection | None,
            str | None,
            str | None,
            list[Mapping[str, Any]],
            int | None,
        ]
    ] = []
    for recipe in product.get("layers", []):
        recipe_id = str(recipe.get("id", ""))
        if (
            not recipe_id
            or recipe_id == "base-dark"
            or recipe_id in SATELLITE_LAYER_IDS
        ):
            continue
        region = REGIONAL_PRODUCT_KEYS.get(spec.product_id)
        static_id = (
            f"{recipe_id}-region-{region}"
            if region and recipe_id in REGIONAL_STATIC_BASES
            else recipe_id
        )
        static_layers = domain.get("staticLayers", {})
        static = static_layers.get(static_id)
        if not isinstance(static, Mapping):
            static_id = recipe_id
            static = static_layers.get(static_id)
        if isinstance(static, Mapping) and static.get("path"):
            prepared.append(
                (
                    ProxyLayerSelection(
                        recipe_id=recipe_id,
                        rendered_layer_id=static_id,
                        source_key=_static_url_key(static),
                        source_path=str(static["path"]),
                        source_valid_time=None,
                        stage_aligned=static_id != recipe_id,
                    ),
                    None,
                    None,
                    [],
                    None,
                )
            )
            continue
        rendered_id = _rendered_layer_id(recipe_id, spec, domain)
        layer = domain.get("layers", {}).get(rendered_id)
        if not isinstance(layer, Mapping):
            continue
        max_age = layer.get("maxAgeMinutes")
        frames = list(layer.get("frames", []))
        if "-region-" in rendered_id:
            # Region-aligned rasters cannot be reused after a viewport change:
            # their pixels are already cropped to the old stage geometry.
            # Legacy frames without this metadata remain eligible, but an
            # explicit mismatched viewport is never baked into a new video.
            frames = [
                frame
                for frame in frames
                if not isinstance(frame, Mapping)
                or "regionalViewport" not in frame
                or frame.get("regionalViewport") == spec.viewport
            ]
        prepared.append(
            (
                None,
                recipe_id,
                rendered_id,
                frames,
                int(max_age) if isinstance(max_age, (int, float)) else None,
            )
        )

    results: list[list[ProxyLayerSelection]] = []
    for selected_frame in selected_frames:
        anchor = selected_frame.valid_time
        cache_key = (spec.product_id, spec.track, anchor)
        if cache is not None and cache_key in cache:
            results.append(list(cache[cache_key]))
            continue
        frame_results: list[ProxyLayerSelection] = []
        for (
            static_selection,
            prepared_recipe_id,
            rendered_id,
            frames,
            max_age_minutes,
        ) in prepared:
            if static_selection is not None:
                frame_results.append(static_selection)
                continue
            assert rendered_id is not None
            assert prepared_recipe_id is not None
            if "lightning" in rendered_id:
                frame = _at_or_before_source_time(frames, anchor, max_age_minutes)
            else:
                frame = _at_or_before(frames, anchor, max_age_minutes)
            if frame is None or not frame.get("path"):
                continue
            key = _source_url_key(frame)
            frame_results.append(
                ProxyLayerSelection(
                    recipe_id=prepared_recipe_id,
                    rendered_layer_id=rendered_id,
                    source_key=key,
                    source_path=str(frame["path"]),
                    source_valid_time=_frame_source_time(frame),
                    stage_aligned="-region-" in rendered_id,
                )
            )
        if cache is not None:
            cache[cache_key] = tuple(frame_results)
        results.append(frame_results)
    return results


def _frame_durations(
    selected: Sequence[SelectedFrame], cadence_minutes: int
) -> list[float]:
    if not selected:
        return []
    nominal = cadence_minutes * METEOROLOGICAL_MINUTE_SECONDS
    # Playback is a sequence of available observations, not a wall-clock
    # reconstruction of missing data. Encoding a four-hour source gap as a
    # four-times-long frame makes an otherwise healthy loop appear frozen.
    # Keep every displayed frame on the same cadence; its VALID timestamp can
    # still jump across a genuine historical gap.
    return [nominal] * len(selected)


def _segment_pts_offset(valid_time: dt.datetime) -> float:
    """Return a stable compressed-time PTS for an independent TS segment.

    The absolute compressed-weather clock is stable as a rolling playlist
    gains or loses older segments; constraining it to the MPEG-TS timestamp
    range preserves normal rollover handling.
    """
    utc = valid_time.astimezone(UTC)
    weather_clock = utc.timestamp() / 60 * METEOROLOGICAL_MINUTE_SECONDS
    return weather_clock % MPEGTS_TIMESTAMP_WRAP_SECONDS


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
    for index, frame in enumerate(selected):
        key = (frame.source_path, frame.source_fetched_at)
        cached = rendered.get(key)
        destination = temporary_root / f"sat-{index:04d}.png"
        if cached is None:
            with Image.open(_safe_path(source_root, frame.source_path)) as source_image:
                satellite = _crop_resize(
                    source_image,
                    spec.resolved_media_viewport,
                    spec.resolved_media_width,
                    spec.resolved_media_height,
                )
                composed = base.copy()
                composed.paste(satellite.convert("RGB"), (0, 0), satellite.getchannel("A"))
        else:
            with Image.open(cached) as cached_image:
                composed = cached_image.crop(
                    (0, 0, spec.resolved_media_width, spec.resolved_media_height)
                ).convert("RGB")
        clock_phase = int(
            frame.valid_time.timestamp() // (spec.cadence_minutes * 60)
        ) % 2
        encoded = Image.new(
            "RGB",
            (
                spec.resolved_media_width,
                spec.resolved_media_height + VIDEO_CLOCK_STRIP_HEIGHT,
            ),
            (255, 255, 255) if clock_phase else (0, 0, 0),
        )
        encoded.paste(composed, (0, 0))
        encoded.save(destination, "PNG", optimize=False, compress_level=1)
        if cached is None:
            rendered[key] = destination
        paths.append(destination)
    return paths


def _operational_satellite_filter(image: Image.Image) -> Image.Image:
    """Bake the viewer's radar/ptype satellite filter in sRGB space."""
    saturation = 0.52
    saturated = image.convert(
        "RGB",
        (
            0.213 + 0.787 * saturation,
            0.715 - 0.715 * saturation,
            0.072 - 0.072 * saturation,
            0,
            0.213 - 0.213 * saturation,
            0.715 + 0.285 * saturation,
            0.072 - 0.072 * saturation,
            0,
            0.213 - 0.213 * saturation,
            0.715 - 0.715 * saturation,
            0.072 + 0.928 * saturation,
            0,
        ),
    )
    brightness = 0.78
    brightened = saturated.convert(
        "RGB",
        (
            brightness, 0, 0, 0,
            0, brightness, 0, 0,
            0, 0, brightness, 0,
        ),
    )
    contrast = 1.06
    offset = 255 * 0.5 * (1 - contrast)
    return brightened.convert(
        "RGB",
        (
            contrast, 0, 0, offset,
            0, contrast, 0, offset,
            0, 0, contrast, offset,
        ),
    )


def _prepare_composite_images(
    source_root: Path,
    output_root: Path,
    spec: ProfileSpec,
    selected: Sequence[SelectedFrame],
    temporary_root: Path,
    layer_ids: Sequence[str],
    frame_proxy_layers: Mapping[dt.datetime, Sequence[Mapping[str, Any]]],
    proxy_entries: Mapping[str, Mapping[str, Any]],
) -> list[Path]:
    """Render a stage-aligned default stack for single-video playback."""
    base_path = _safe_path(source_root, f"static/{spec.domain_id}/base-dark.png")
    with Image.open(base_path) as base_image:
        base = _crop_resize(
            base_image,
            spec.viewport,
            spec.width,
            spec.height,
        ).convert("RGB")
    opacities = _recipe_opacities(spec.product_id)
    stack_order = {
        layer_id: index for index, layer_id in enumerate(layer_ids)
    }
    last_satellite_key: tuple[str, str] | None = None
    last_satellite: Image.Image | None = None
    paths: list[Path] = []
    for index, frame in enumerate(selected):
        key = (frame.source_path, frame.source_fetched_at)
        satellite_base = last_satellite if key == last_satellite_key else None
        if satellite_base is None:
            with Image.open(_safe_path(source_root, frame.source_path)) as source_image:
                satellite = _crop_resize(
                    source_image,
                    spec.viewport,
                    spec.width,
                    spec.height,
                )
                satellite_base = base.copy()
                satellite_base.paste(
                    satellite.convert("RGB"),
                    (0, 0),
                    satellite.getchannel("A"),
                )
            satellite_base = _operational_satellite_filter(satellite_base)
            last_satellite_key = key
            last_satellite = satellite_base
        composed = satellite_base.convert("RGBA")
        layers = sorted(
            frame_proxy_layers.get(frame.valid_time, ()),
            key=lambda item: stack_order.get(str(item.get("id", "")), len(stack_order)),
        )
        for layer in layers:
            recipe_id = str(layer.get("id", ""))
            if recipe_id not in stack_order:
                continue
            proxy = proxy_entries.get(str(layer.get("sourceKey", "")))
            if not isinstance(proxy, Mapping) or not proxy.get("path"):
                continue
            with Image.open(_safe_path(output_root, str(proxy["path"]))) as image:
                overlay = image.convert("RGBA")
            if overlay.size != (spec.width, spec.height):
                overlay = overlay.resize(
                    (spec.width, spec.height),
                    Image.Resampling.LANCZOS,
                )
            opacity = opacities.get(recipe_id, 1.0)
            if opacity < 1:
                overlay.putalpha(
                    overlay.getchannel("A").point(
                        lambda value, factor=opacity: round(value * factor)
                    )
                )
            composed.alpha_composite(overlay)
        clock_phase = int(
            frame.valid_time.timestamp() // (spec.cadence_minutes * 60)
        ) % 2
        encoded = Image.new(
            "RGB",
            (spec.width, spec.height + VIDEO_CLOCK_STRIP_HEIGHT),
            (255, 255, 255) if clock_phase else (0, 0, 0),
        )
        encoded.paste(composed.convert("RGB"), (0, 0))
        destination = temporary_root / f"composite-{index:04d}.png"
        encoded.save(destination, "PNG", optimize=False, compress_level=1)
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
        lines.extend(
            (
                f"file '{_ffconcat_path(images[-1])}'",
                f"option framerate {FFCONCAT_TIMEBASE_FPS}",
            )
        )
        # ffconcat needs the next input timestamp to materialize the preceding
        # still's declared duration. The output duration below cuts this end
        # sentinel at the exact loop boundary.
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
            "-vf",
            f"fps={VIDEO_FRAME_RATE}",
            "-fps_mode",
            "cfr",
            "-r",
            str(VIDEO_FRAME_RATE),
            "-enc_time_base",
            f"1:{VIDEO_FRAME_RATE}",
            "-x264-params",
            (
                f"keyint={max(1, round(sum(durations) * VIDEO_FRAME_RATE))}:"
                f"min-keyint={max(1, round(sum(durations) * VIDEO_FRAME_RATE))}:"
                "scenecut=0:force-cfr=1:colorprim=bt709:transfer=bt709:"
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
            "-t",
            f"{sum(durations):.6f}",
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


def _probe_vfr_packets(ffmpeg: str, path: Path) -> list[tuple[float, float]]:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    if not Path(ffprobe).is_file():
        discovered = shutil.which("ffprobe")
        if not discovered:
            raise RuntimeError("ffprobe is required to validate exact-range video")
        ffprobe = discovered
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise RuntimeError(f"ffprobe returned no video packets for {path}")
    return [
        (float(packet["pts_time"]), float(packet["duration_time"]))
        for packet in packets
    ]


def _encode_vfr_mp4(
    ffmpeg: str,
    images: Sequence[Path],
    durations: Sequence[float],
    destination: Path,
    spec: ProfileSpec,
) -> None:
    """Encode exactly one H.264 sample per meteorological frame.

    ffconcat needs a sentinel image to materialize the final sample duration.
    The first pass therefore contains one duplicate sentinel; a stream-copy
    remux retains exactly the N real samples while preserving that duration.
    """
    if len(images) != len(durations) or not images:
        raise ValueError("VFR encoding requires matching non-empty images and durations")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.encoded.mp4")
    remuxed = destination.with_name(f".{destination.name}.{os.getpid()}.tmp.mp4")
    with tempfile.TemporaryDirectory(prefix="radarsat-vfr-concat-") as directory:
        concat = Path(directory) / "frames.ffconcat"
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
        keyint = max(1, len(images))
        encode = [
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
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "vfr",
            "-enc_time_base",
            "1:1000",
            "-x264-params",
            (
                f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0:"
                "force-cfr=0:colorprim=bt709:transfer=bt709:colormatrix=bt709"
            ),
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-video_track_timescale",
            "1000",
            "-y",
            str(temporary),
        ]
        remux = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(temporary),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-frames:v",
            str(len(images)),
            "-movflags",
            "+faststart",
            "-y",
            str(remuxed),
        ]
        try:
            subprocess.run(encode, check=True, capture_output=True, text=True)
            subprocess.run(remux, check=True, capture_output=True, text=True)
            packets = _probe_vfr_packets(ffmpeg, remuxed)
            if len(packets) != len(images):
                raise RuntimeError(
                    f"Exact video has {len(packets)} packets for {len(images)} frames"
                )
            expected_pts = 0.0
            for index, ((pts, duration), expected_duration) in enumerate(
                zip(packets, durations, strict=True)
            ):
                if abs(pts - expected_pts) > 0.002 or abs(duration - expected_duration) > 0.002:
                    raise RuntimeError(
                        "Exact video timing mismatch at packet "
                        f"{index}: got ({pts:.3f}, {duration:.3f}), expected "
                        f"({expected_pts:.3f}, {expected_duration:.3f})"
                    )
                expected_pts += expected_duration
            remuxed.replace(destination)
        except subprocess.CalledProcessError as error:
            details = (error.stderr or error.stdout or "").strip()
            raise RuntimeError(f"ffmpeg VFR encode failed: {details[-2000:]}") from error
        finally:
            temporary.unlink(missing_ok=True)
            remuxed.unlink(missing_ok=True)


def _encode_ts(
    ffmpeg: str,
    images: Sequence[Path],
    durations: Sequence[float],
    destination: Path,
    spec: ProfileSpec,
    pts_offset: float,
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
            # Independent segments start with an I-frame and avoid B-frame
            # reordering so their presentation ranges match EXTINF exactly.
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"fps={VIDEO_FRAME_RATE}",
            "-fps_mode",
            "cfr",
            "-r",
            str(VIDEO_FRAME_RATE),
            "-enc_time_base",
            f"1:{VIDEO_FRAME_RATE}",
            "-x264-params",
            (
                f"keyint={max(1, round(sum(durations) * VIDEO_FRAME_RATE))}:"
                f"min-keyint={max(1, round(sum(durations) * VIDEO_FRAME_RATE))}:"
                "scenecut=0:bframes=0:force-cfr=1:colorprim=bt709:transfer=bt709:colormatrix=bt709"
            ),
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-output_ts_offset",
            f"{pts_offset:.6f}",
            "-t",
            f"{sum(durations):.6f}",
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
    cadence_minutes: int,
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
    expected = max(1, round(group_hours * 60 / cadence_minutes))
    results: list[list[int]] = []
    pending: list[int] = []
    for key in sorted(grouped):
        pending.extend(grouped[key])
        if len(pending) >= expected:
            results.append(pending)
            pending = []
    if pending:
        results.append(pending)
    return results


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
    owner: str | None = None,
    variant: Mapping[str, Any] | None = None,
    prepare_images: Callable[
        [Sequence[SelectedFrame], Path], list[Path]
    ] | None = None,
) -> tuple[Path, str, list[Mapping[str, Any]], list[float]]:
    owner = owner or f"shared-{spec.domain_id}-{spec.media_group}"
    groups = _segment_groups(selected, spec.track, spec.cadence_minutes)
    adjusted_durations = list(durations)
    segment_entries: list[Mapping[str, Any]] = []
    for indexes in groups:
        encoded_durations = [durations[index] for index in indexes]
        pts_offset = _segment_pts_offset(selected[indexes[0]].valid_time)
        segment_payload: dict[str, Any] = {
            "videoRenderVersion": VIDEO_RENDER_VERSION,
            "domainId": spec.domain_id,
            "layerId": spec.layer_id,
            "track": spec.track,
            "mediaGroup": spec.media_group,
            "mediaViewport": dict(spec.resolved_media_viewport),
            "mediaWidth": spec.resolved_media_width,
            "mediaHeight": spec.resolved_media_height,
            "clockStripHeight": VIDEO_CLOCK_STRIP_HEIGHT,
            "frameRate": VIDEO_FRAME_RATE,
            "crf": spec.crf,
            "preset": spec.preset,
            "frames": [media_inputs[index] for index in indexes],
            "durations": encoded_durations,
            "ptsOffset": round(pts_offset, 6),
        }
        if variant is not None:
            segment_payload["variant"] = dict(variant)
        segment_fingerprint = _hash_payload(segment_payload, 16)
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
                prepared = (
                    prepare_images(group_frames, Path(temporary))
                    if prepare_images is not None
                    else _prepare_satellite_images(
                        source_root,
                        spec,
                        group_frames,
                        Path(temporary),
                    )
                )
                _encode_ts(
                    ffmpeg,
                    prepared,
                    encoded_durations,
                    segment_path,
                    spec,
                    pts_offset,
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
    playlist_payload: dict[str, Any] = {
        "videoRenderVersion": VIDEO_RENDER_VERSION,
        "segments": segment_entries,
    }
    if variant is not None:
        playlist_payload["variant"] = dict(variant)
    playlist_fingerprint = _hash_payload(playlist_payload)
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
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for entry in segment_entries:
            if int(entry["firstFrame"]) > 0:
                previous = selected[int(entry["firstFrame"]) - 1]
                current = selected[int(entry["firstFrame"])]
                expected = previous.valid_time + dt.timedelta(
                    minutes=spec.cadence_minutes
                )
                # Absolute compressed PTS make ordinary rolling segments reusable.
                # If observations are missing across a segment boundary, tell HLS
                # to pack the next segment immediately after the previous one
                # instead of preserving a long, visibly frozen media-time gap.
                if current.valid_time != expected:
                    lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{float(entry['durationSeconds']):.6f},")
            segment_absolute = output_root / str(entry["path"])
            lines.append(os.path.relpath(segment_absolute, playlist_path.parent))
        lines.append("#EXT-X-ENDLIST")
        _atomic_text(playlist_path, "\n".join(lines) + "\n")
    return playlist_path, playlist_fingerprint, segment_entries, adjusted_durations


def _range_frames(
    selected: Sequence[SelectedFrame],
    hours: int,
) -> tuple[int, list[SelectedFrame]]:
    if not selected:
        return 0, []
    cutoff = selected[-1].valid_time - dt.timedelta(hours=hours)
    first = next(
        (index for index, frame in enumerate(selected) if frame.valid_time >= cutoff),
        len(selected) - 1,
    )
    return first, list(selected[first:])


def _exact_renditions(spec: ProfileSpec) -> tuple[tuple[str, int, int], ...]:
    if spec.width <= 1280:
        return (("display", spec.width, spec.height),)
    efficient_width = 1280
    efficient_height = _even(spec.height * efficient_width / spec.width)
    return (
        ("efficient", efficient_width, efficient_height),
        ("high", spec.width, spec.height),
    )


def _build_exact_composite_range(
    source_root: Path,
    output_root: Path,
    spec: ProfileSpec,
    selected: Sequence[SelectedFrame],
    media_inputs: Sequence[Mapping[str, Any]],
    hours: int,
    preset_id: str,
    layer_ids: Sequence[str],
    filtered_proxy_layers: Mapping[dt.datetime, Sequence[Mapping[str, Any]]],
    proxy_entries: Mapping[str, Mapping[str, Any]],
    composite_variant: Mapping[str, Any],
    *,
    ffmpeg: str,
    now: dt.datetime,
) -> tuple[Mapping[str, Any], int]:
    first_frame, range_frames = _range_frames(selected, hours)
    if len(range_frames) < 2:
        raise RuntimeError(
            f"{spec.product_id} {hours}h composite has fewer than two frames"
        )
    nominal_duration = spec.cadence_minutes * METEOROLOGICAL_MINUTE_SECONDS
    durations = [nominal_duration] * len(range_frames)
    durations[-1] = nominal_duration * 4
    range_inputs = list(media_inputs[first_frame:])
    range_overlay_inputs: list[list[Mapping[str, Any]]] = []
    for frame in range_frames:
        frame_inputs: list[Mapping[str, Any]] = []
        for layer in filtered_proxy_layers.get(frame.valid_time, ()):
            source_key = str(layer.get("sourceKey", ""))
            proxy = proxy_entries.get(source_key)
            if not isinstance(proxy, Mapping) or not proxy.get("path"):
                continue
            frame_inputs.append(
                {
                    "id": str(layer.get("id", "")),
                    "renderId": str(layer.get("renderId", "")),
                    "sourceKey": source_key,
                    "sourceValidTime": layer.get("sourceValidTime"),
                    # Proxy filenames are hashes of the rendered RGBA pixels.
                    # Including the immutable path binds every exact MP4 to
                    # the radar/lightning/fire/model snapshot it actually
                    # contains without re-hashing hundreds of images here.
                    "proxyPath": str(proxy["path"]),
                    "proxyByteLength": int(proxy.get("byteLength", 0)),
                }
            )
        range_overlay_inputs.append(frame_inputs)
    renditions: list[Mapping[str, Any]] = []
    total_bytes = 0
    for rendition_id, width, height in _exact_renditions(spec):
        rendition_spec = replace(
            spec,
            width=width,
            height=height,
            media_group="stage-composite-exact",
            media_viewport=dict(spec.viewport),
            media_width=width,
            media_height=height,
            crf=16 if spec.track in {"live", "day"} else 18,
        )
        payload = {
            "videoRenderVersion": VIDEO_RENDER_VERSION,
            "compositeRenderVersion": COMPOSITE_RENDER_VERSION,
            "preset": dict(composite_variant),
            "rangeHours": hours,
            "rendition": rendition_id,
            "width": width,
            "height": height,
            "frames": range_inputs,
            "overlayInputs": range_overlay_inputs,
            "durations": durations,
        }
        fingerprint = _hash_payload(payload)
        end_stamp = range_frames[-1].valid_time.strftime("%Y%m%dT%H%MZ")
        destination = (
            output_root
            / "videos"
            / f"composite-{spec.product_id}"
            / spec.layer_id
            / spec.track
            / f"exact-{hours}h"
            / rendition_id
            / f"{end_stamp}-{fingerprint}.mp4"
        )
        if not destination.is_file():
            with tempfile.TemporaryDirectory(
                prefix=f"radarsat-{spec.product_id}-{preset_id}-{hours}h-{rendition_id}-"
            ) as temporary:
                prepared = _prepare_composite_images(
                    source_root,
                    output_root,
                    rendition_spec,
                    range_frames,
                    Path(temporary),
                    layer_ids,
                    filtered_proxy_layers,
                    proxy_entries,
                )
                _encode_vfr_mp4(
                    ffmpeg,
                    prepared,
                    durations,
                    destination,
                    rendition_spec,
                )
        media = {
            "path": destination.relative_to(output_root).as_posix(),
            "mimeType": "video/mp4",
            "codec": "avc1",
            "width": width,
            "height": height + VIDEO_CLOCK_STRIP_HEIGHT,
            "contentHeight": height,
            "byteLength": destination.stat().st_size,
            "sha256": _sha256_file(destination),
        }
        total_bytes += destination.stat().st_size
        renditions.append({"id": rendition_id, "media": media})
    return (
        {
            "hours": hours,
            "builtAt": _format_time(now),
            "startValidTime": _format_time(range_frames[0].valid_time),
            "endValidTime": _format_time(range_frames[-1].valid_time),
            "firstFrame": first_frame,
            "frameCount": len(range_frames),
            "durationsSeconds": [round(value, 6) for value in durations],
            "boundaryIntervalMultiplier": 4,
            "renditions": renditions,
        },
        total_bytes,
    )


def _reusable_exact_range(
    output_root: Path,
    previous_manifest: Mapping[str, Any] | None,
    preset_id: str,
    layer_ids: Sequence[str],
    hours: int,
    selected: Sequence[SelectedFrame],
    now: dt.datetime,
) -> Mapping[str, Any] | None:
    if previous_manifest is None:
        return None
    # The source anchor itself advances on a ten-minute clock. A literal
    # fifteen-minute rebuild gate would therefore update the six-hour asset
    # only every second anchor (twenty minutes). Rebuilding it on each new
    # ten-minute anchor is the only useful schedule that meets the requested
    # <=15-minute freshness without rewriting an identical file at :15.
    minimum_minutes = {3: 0, 6: 10, 12: 30, 24: 30}.get(hours, 0)
    composites = previous_manifest.get("composites")
    if not isinstance(composites, list):
        return None
    preset = next(
        (
            value for value in composites
            if isinstance(value, Mapping)
            and value.get("id") == preset_id
            and value.get("layerIds") == list(layer_ids)
        ),
        None,
    )
    if not isinstance(preset, Mapping) or not isinstance(preset.get("ranges"), list):
        return None
    candidate = next(
        (
            value for value in preset["ranges"]
            if isinstance(value, Mapping) and value.get("hours") == hours
        ),
        None,
    )
    if not isinstance(candidate, Mapping):
        return None
    built_at = _parse_time(candidate.get("builtAt"))
    start = _parse_time(candidate.get("startValidTime"))
    end = _parse_time(candidate.get("endValidTime"))
    if (
        built_at is None
        or start is None
        or end is None
    ):
        return None
    if selected and end != selected[-1].valid_time:
        if minimum_minutes <= 0 or now - built_at >= dt.timedelta(minutes=minimum_minutes):
            return None
    valid_times = [frame.valid_time for frame in selected]
    try:
        first = valid_times.index(start)
        last = valid_times.index(end)
    except ValueError:
        return None
    if last < first:
        return None
    reused = dict(candidate)
    reused["firstFrame"] = first
    reused["frameCount"] = last - first + 1
    durations = reused.get("durationsSeconds")
    if not isinstance(durations, list) or len(durations) != reused["frameCount"]:
        return None
    renditions = reused.get("renditions")
    if not isinstance(renditions, list) or not renditions:
        return None
    for rendition in renditions:
        if not isinstance(rendition, Mapping) or not isinstance(rendition.get("media"), Mapping):
            return None
        relative = str(rendition["media"].get("path", ""))
        try:
            _safe_path(output_root, relative)
        except (ValueError, FileNotFoundError):
            return None
    return reused


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


def _update_profile_index(
    index_path: Path,
    spec: ProfileSpec,
    generated_at: str,
    profile: Mapping[str, str],
) -> None:
    """Merge one track pointer without racing the other encoder jobs."""
    lock_path = index_path.with_name(f".{index_path.name}.lock")
    deadline = time.monotonic() + 30
    while True:
        try:
            lock_path.mkdir(parents=True)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 300
                if stale:
                    lock_path.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out updating video index: {index_path}")
            time.sleep(0.05)
    try:
        current = _load_index(index_path)
        profiles = dict(current.get("profiles", {}))
        profiles[spec.track] = dict(profile)
        pointer = {
            "schemaVersion": VIDEO_SCHEMA_VERSION,
            "productId": spec.product_id,
            "layerId": spec.layer_id,
            "updatedAt": generated_at,
            "profiles": profiles,
        }
        _atomic_json(index_path, pointer)
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


def _manifest_dependencies(output_root: Path, manifests: Iterable[Path]) -> set[Path]:
    dependencies: set[Path] = set()
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        media_values: list[object] = [manifest.get("media")]
        default_composite = manifest.get("defaultComposite")
        if isinstance(default_composite, Mapping):
            media_values.append(default_composite.get("media"))
        composites = manifest.get("composites")
        if isinstance(composites, list):
            for composite in composites:
                if not isinstance(composite, Mapping):
                    continue
                ranges = composite.get("ranges")
                if not isinstance(ranges, list):
                    continue
                for range_value in ranges:
                    if not isinstance(range_value, Mapping):
                        continue
                    renditions = range_value.get("renditions")
                    if not isinstance(renditions, list):
                        continue
                    for rendition in renditions:
                        if isinstance(rendition, Mapping):
                            media_values.append(rendition.get("media"))
        for media in media_values:
            if not isinstance(media, Mapping) or not media.get("path"):
                continue
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
    _prune_shared: bool = True,
) -> Mapping[str, int]:
    output_root = output_root.resolve()
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
    if _prune_shared:
        removed_dependencies += prune_shared_video_orphans(output_root, now=now)
    return {
        "removedManifests": removed_manifests,
        "removedDependencies": removed_dependencies,
    }


def prune_shared_video_orphans(
    output_root: Path,
    *,
    now: dt.datetime | None = None,
) -> int:
    """Prune shared media once, after every parallel product worker commits."""
    output_root = output_root.resolve()
    current = (now or dt.datetime.now(UTC)).timestamp()
    grace_seconds = LOCAL_ORPHAN_GRACE_HOURS * 3600
    all_manifest_dependencies = _manifest_dependencies(
        output_root,
        (output_root / "video-manifests").rglob("*.json"),
    )
    removed = 0
    videos_root = output_root / "videos"
    if videos_root.exists():
        for media_root in videos_root.iterdir():
            if not media_root.is_dir() or not media_root.name.startswith(
                ("shared-", "composite-")
            ):
                continue
            for path in media_root.rglob("*"):
                if not path.is_file() or path in all_manifest_dependencies:
                    continue
                if current - path.stat().st_mtime <= grace_seconds:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
    segment_root = output_root / "video-segments"
    if segment_root.exists():
        for path in segment_root.rglob("*.ts"):
            if path in all_manifest_dependencies:
                continue
            if current - path.stat().st_mtime <= grace_seconds:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def build_profile(
    source_root: Path,
    output_root: Path,
    catalog: Mapping[str, Any],
    spec: ProfileSpec,
    *,
    ffmpeg: str,
    hours: float = 24.0,
    now: dt.datetime | None = None,
    proxy_selection_cache: dict[
        tuple[str, str, dt.datetime], tuple[ProxyLayerSelection, ...]
    ] | None = None,
    proxy_render_cache: dict[
        tuple[str, str, str, int, int, bool], Mapping[str, Any]
    ] | None = None,
) -> Mapping[str, Any]:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    build_now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    index_path = output_root / "video-index" / spec.product_id / f"{spec.layer_id}.json"
    current_index = _load_index(index_path)
    current_profile = current_index.get("profiles", {}).get(spec.track, {})
    previous_manifest: Mapping[str, Any] | None = None
    previous_manifest_value = current_profile.get("manifestPath")
    if isinstance(previous_manifest_value, str):
        try:
            loaded_previous = json.loads(
                _safe_path(output_root, previous_manifest_value).read_text()
            )
            if isinstance(loaded_previous, Mapping):
                previous_manifest = loaded_previous
        except (OSError, ValueError, json.JSONDecodeError):
            previous_manifest = None
    selected = _selected_satellite_frames(catalog, spec, hours, now=now)
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

    proxy_selections = _proxy_selections(
        catalog,
        spec,
        selected,
        proxy_selection_cache,
    )
    unique_proxy_sources: dict[str, ProxyLayerSelection] = {}
    for frame_selections in proxy_selections:
        for selection in frame_selections:
            unique_proxy_sources.setdefault(selection.source_key, selection)

    proxy_entries: dict[str, Mapping[str, Any]] = {}
    proxy_warnings: list[Mapping[str, str]] = []
    for source_key, selection in unique_proxy_sources.items():
        render_cache_key = (
            spec.product_id,
            selection.rendered_layer_id,
            source_key,
            spec.width,
            spec.height,
            selection.stage_aligned,
        )
        cached_proxy = (
            proxy_render_cache.get(render_cache_key)
            if proxy_render_cache is not None
            else None
        )
        if cached_proxy is not None:
            proxy_entries[source_key] = cached_proxy
            continue
        try:
            rendered_proxy = _render_proxy(
                source_root,
                output_root,
                spec,
                selection.rendered_layer_id,
                source_key,
                selection.source_path,
                stage_aligned=selection.stage_aligned,
            )
            proxy_entries[source_key] = rendered_proxy
            if proxy_render_cache is not None:
                proxy_render_cache[render_cache_key] = rendered_proxy
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
        except ProxySourceUnreadableError:
            # A producer may still be replacing an optional image when this
            # snapshot is rendered.  Pillow reports truncated/unidentified
            # images as OSError subclasses.  Treat that the same way as a
            # missing optional proxy: omit it from this immutable generation
            # and let the next video build pick up the repaired source.
            proxy_warnings.append(
                {
                    "sourceKey": source_key,
                    "sourcePath": selection.source_path,
                    "renderedLayerId": selection.rendered_layer_id,
                    "reason": "source-unreadable-during-build",
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

    composites: list[Mapping[str, Any]] = []
    composite_media_bytes = 0
    for preset_id, composite_layer_ids in _composite_presets(spec):
        selected_layer_ids = set(composite_layer_ids)
        opacities = _recipe_opacities(spec.product_id)
        proxy_hashes: dict[str, str] = {}
        composite_inputs: list[Mapping[str, Any]] = []
        filtered_proxy_layers: dict[
            dt.datetime, Sequence[Mapping[str, Any]]
        ] = {}
        for frame, satellite_input, layers in zip(
            selected, media_inputs, proxy_layer_entries, strict=True
        ):
            active_layers = [
                layer for layer in layers
                if str(layer.get("id", "")) in selected_layer_ids
            ]
            filtered_proxy_layers[frame.valid_time] = active_layers
            layer_inputs: list[Mapping[str, Any]] = []
            for layer in active_layers:
                source_key = str(layer.get("sourceKey", ""))
                proxy = proxy_entries.get(source_key)
                if not isinstance(proxy, Mapping) or not proxy.get("path"):
                    continue
                proxy_path = str(proxy["path"])
                if proxy_path not in proxy_hashes:
                    proxy_hashes[proxy_path] = _sha256_file(
                        _safe_path(output_root, proxy_path)
                    )
                recipe_id = str(layer.get("id", ""))
                layer_inputs.append(
                    {
                        "id": recipe_id,
                        "renderId": str(layer.get("renderId", "")),
                        "sourceKey": source_key,
                        "sourceValidTime": layer.get("sourceValidTime"),
                        "proxyPath": proxy_path,
                        "proxyByteLength": int(proxy.get("byteLength", 0)),
                        "proxySha256": proxy_hashes[proxy_path],
                        "opacity": opacities.get(recipe_id, 1.0),
                    }
                )
            composite_inputs.append(
                {
                    "satellite": satellite_input,
                    "layers": layer_inputs,
                }
            )
        base_path = _safe_path(
            source_root, f"static/{spec.domain_id}/base-dark.png"
        )
        base_stat = base_path.stat()
        composite_variant = {
            "id": preset_id,
            "compositeRenderVersion": COMPOSITE_RENDER_VERSION,
            "proxyRenderVersion": PROXY_RENDER_VERSION,
            "layerIds": list(composite_layer_ids),
            "opacities": {
                layer_id: opacities.get(layer_id, 1.0)
                for layer_id in composite_layer_ids
            },
            "satelliteFilter": "saturate(0.52) brightness(0.78) contrast(1.06)",
            "base": {
                "path": f"static/{spec.domain_id}/base-dark.png",
                "size": base_stat.st_size,
                "sha256": _sha256_file(base_path),
            },
        }
        ranges: list[Mapping[str, Any]] = []
        exact_ranges = VIDEO_EXACT_RANGES.get(spec.product_id, ())
        track_ranges = tuple(
            range_hours
            for range_hours in exact_ranges
            if (
                (spec.track == "live" and range_hours < 24)
                or (spec.track == "day" and range_hours == 24)
            )
        )
        for range_hours in track_ranges:
            exact_range = _reusable_exact_range(
                output_root,
                previous_manifest,
                preset_id,
                composite_layer_ids,
                range_hours,
                selected,
                build_now,
            )
            exact_bytes = 0
            if exact_range is None:
                exact_range, exact_bytes = _build_exact_composite_range(
                    source_root,
                    output_root,
                    spec,
                    selected,
                    media_inputs,
                    range_hours,
                    preset_id,
                    composite_layer_ids,
                    filtered_proxy_layers,
                    proxy_entries,
                    composite_variant,
                    ffmpeg=ffmpeg,
                    now=build_now,
                )
            ranges.append(exact_range)
            composite_media_bytes += exact_bytes

        if spec.track == "archive" and spec.product_id in VIDEO_ARCHIVE_PRODUCTS:
            archive_renditions: list[Mapping[str, Any]] = []
            archive_range_durations: list[float] | None = None
            for rendition_id, width, height in _exact_renditions(spec):
                composite_spec = replace(
                    spec,
                    width=width,
                    height=height,
                    media_group="stage-composite",
                    media_viewport=dict(spec.viewport),
                    media_width=width,
                    media_height=height,
                    crf=18,
                )

                def prepare_composite(
                    group_frames: Sequence[SelectedFrame],
                    temporary: Path,
                    rendition_spec: ProfileSpec = composite_spec,
                ) -> list[Path]:
                    return _prepare_composite_images(
                        source_root,
                        output_root,
                        rendition_spec,
                        group_frames,
                        temporary,
                        composite_layer_ids,
                        filtered_proxy_layers,
                        proxy_entries,
                    )

                (
                    composite_media_path,
                    composite_fingerprint,
                    composite_segments,
                    composite_durations,
                ) = _build_hls_media(
                    source_root,
                    output_root,
                    composite_spec,
                    selected,
                    composite_inputs,
                    durations,
                    ffmpeg=ffmpeg,
                    owner=f"composite-{spec.product_id}-{preset_id}",
                    variant={**composite_variant, "rendition": rendition_id},
                    prepare_images=prepare_composite,
                )
                composite_media_bytes += composite_media_path.stat().st_size + sum(
                    int(entry["byteLength"]) for entry in composite_segments
                )
                archive_range_durations = list(composite_durations)
                archive_renditions.append({
                    "id": rendition_id,
                    "media": {
                        "path": composite_media_path.relative_to(output_root).as_posix(),
                        "mimeType": "application/vnd.apple.mpegurl",
                        "codec": "avc1",
                        "width": width,
                        "height": height + VIDEO_CLOCK_STRIP_HEIGHT,
                        "contentHeight": height,
                        "frameRate": VIDEO_FRAME_RATE,
                        "byteLength": composite_media_path.stat().st_size,
                        "sha256": _sha256_file(composite_media_path),
                        "fingerprint": composite_fingerprint,
                        "segments": composite_segments,
                    },
                })
            assert archive_range_durations is not None
            archive_range_durations[-1] *= 4
            ranges.append(
                {
                    "hours": 168,
                    "firstFrame": 0,
                    "frameCount": len(selected),
                    "durationsSeconds": [
                        round(value, 6) for value in archive_range_durations
                    ],
                    "boundaryIntervalMultiplier": 4,
                    "renditions": archive_renditions,
                }
            )
        if not ranges:
            continue
        composites.append({
            "id": preset_id,
            "layerIds": list(composite_layer_ids),
            "mediaViewport": dict(spec.viewport),
            "ranges": ranges,
        })

    generation_fingerprint = _hash_payload(
        {
            "mediaFingerprint": media_fingerprint,
            "proxies": proxy_entries,
            "proxyLayers": proxy_layer_entries,
            "proxyRenderVersion": PROXY_RENDER_VERSION,
            "composites": composites,
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
    generated_at = _format_time(build_now)
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
            "height": spec.resolved_media_height + VIDEO_CLOCK_STRIP_HEIGHT,
            "contentHeight": spec.resolved_media_height,
            "frameRate": VIDEO_FRAME_RATE,
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
    if composites:
        manifest["composites"] = composites
    if not manifest_path.is_file():
        _atomic_json(manifest_path, manifest)

    _update_profile_index(
        index_path,
        spec,
        generated_at,
        {
            "generation": generation,
            "manifestPath": manifest_path.relative_to(output_root).as_posix(),
        },
    )
    return {
        "status": "built",
        "productId": spec.product_id,
        "layerId": spec.layer_id,
        "track": spec.track,
        "generation": generation,
        "manifestPath": manifest_path.relative_to(output_root).as_posix(),
        "mediaPath": media_path.relative_to(output_root).as_posix(),
        "mediaBytes": media_path.stat().st_size
        + sum(int(entry["byteLength"]) for entry in segment_entries)
        + composite_media_bytes,
        "compositeMediaBytes": composite_media_bytes,
        "segments": len(segment_entries),
        "frames": len(selected),
        "proxies": len(proxy_entries),
        "proxyWarnings": len(proxy_warnings),
    }


def build_satellite_videos(
    source_root: Path,
    output_root: Path | None = None,
    *,
    product_ids: Iterable[str] | None = None,
    layer_ids: Iterable[str] | None = None,
    track_names: Iterable[str] | None = None,
    ffmpeg: str | None = None,
    hours: float = 24.0,
    archive_hours: float = 168.0,
    now: dt.datetime | None = None,
    prune_shared_assets: bool = True,
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
    requested_layers = set(layer_ids or (spec.layer_id for spec in VIDEO_PROFILES))
    unknown_layers = requested_layers.difference(spec.layer_id for spec in VIDEO_PROFILES)
    if unknown_layers:
        raise ValueError(f"Unsupported video layers: {sorted(unknown_layers)}")
    requested_tracks = set(track_names or ("live", "day", "archive"))
    unknown_tracks = requested_tracks.difference({"live", "day", "archive"})
    if unknown_tracks:
        raise ValueError(f"Unsupported video tracks: {sorted(unknown_tracks)}")
    catalog = build_catalog(source_root)
    results: list[Mapping[str, Any]] = []
    failures: list[Mapping[str, str]] = []
    proxy_selection_cache: dict[
        tuple[str, str, dt.datetime], tuple[ProxyLayerSelection, ...]
    ] = {}
    proxy_render_cache: dict[
        tuple[str, str, str, int, int, bool], Mapping[str, Any]
    ] = {}
    for spec in VIDEO_PROFILES:
        if spec.product_id not in requested:
            continue
        if spec.layer_id not in requested_layers:
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
                    hours=(
                        min(hours, 24.0)
                        if spec.track in {"live", "day"}
                        else min(archive_hours, 168.0)
                    ),
                    now=now,
                    proxy_selection_cache=proxy_selection_cache,
                    proxy_render_cache=proxy_render_cache,
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
    sorted_products = sorted(requested)
    prune_results = {
        product_id: prune_local_video_orphans(
            output_root,
            product_id,
            now=now,
            # Shared media and segments are referenced across products. Scan
            # that large tree only after every product's old manifests have
            # been pruned, and only once per cycle.
            _prune_shared=(
                prune_shared_assets and index == len(sorted_products) - 1
            ),
        )
        for index, product_id in enumerate(sorted_products)
    }
    return {
        "status": "warning" if failures else "ok",
        "sourceRoot": str(source_root),
        "outputRoot": str(output_root),
        "profiles": results,
        "failures": failures,
        "pruned": prune_results,
    }
