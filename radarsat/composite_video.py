from __future__ import annotations

from collections import OrderedDict
import datetime as dt
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image

from .catalog import build_catalog
from .config import (
    VIDEO_COMPOSITE_PRESETS,
    VIDEO_EXACT_RANGES,
    VIDEO_TRACKS_BY_PRODUCT,
    video_composite_kind,
    video_composite_overlay_layer_ids,
)
from .video import (
    COMPOSITE_VIDEO_CRF,
    METEOROLOGICAL_MINUTE_SECONDS,
    PROXY_RENDER_VERSION,
    UTC,
    VIDEO_CLOCK_STRIP_HEIGHT,
    VIDEO_FRAME_RATE,
    VIDEO_PROFILES,
    ProfileSpec,
    ProxyLayerSelection,
    SelectedFrame,
    _atomic_json,
    _at_or_before,
    _build_hls_media,
    _composite_presets,
    _crop_resize,
    _exact_renditions,
    _format_time,
    _frame_source_time,
    _hash_payload,
    _operational_satellite_filter,
    _proxy_selections,
    _range_frames,
    _recipe_opacities,
    _render_proxy,
    _safe_path,
    _selected_satellite_frames,
    _sha256_file,
    _source_fingerprint,
)


COMPOSITE_SIDECAR_SCHEMA_VERSION = 1
HYBRID_COMPOSITE_SIDECAR_SCHEMA_VERSION = 2
COMPOSITE_FRAME_CACHE_VERSION = 1
COMPOSITE_FRAME_CACHE_MAX_AGE_HOURS = 36.0
COMPOSITE_FRAME_CACHE_MAX_BYTES = 6_000_000_000
COMPOSITE_LOCAL_GENERATIONS_TO_KEEP = 1
COMPOSITE_MANIFEST_GRACE_HOURS = 0.25
@dataclass(frozen=True)
class CompositeFrame:
    frame: SelectedFrame
    selections: tuple[ProxyLayerSelection, ...]
    fingerprint: str
    high_path: Path


class _RenderContext:
    """Small bounded decode cache used only while filling missing frame caches."""

    def __init__(self, source_root: Path, spec: ProfileSpec, *, limit: int = 16) -> None:
        self.source_root = source_root
        self.spec = spec
        self.limit = limit
        base_path = _safe_path(source_root, f"static/{spec.domain_id}/base-dark.png")
        with Image.open(base_path) as image:
            self.base = _crop_resize(
                image,
                spec.viewport,
                spec.width,
                spec.height,
            ).convert("RGB")
        self.satellites: OrderedDict[tuple[str, str], Image.Image] = OrderedDict()
        self.overlays: OrderedDict[tuple[str, bool], Image.Image] = OrderedDict()

    def close(self) -> None:
        self.base.close()
        for image in (*self.satellites.values(), *self.overlays.values()):
            image.close()
        self.satellites.clear()
        self.overlays.clear()

    def __enter__(self) -> _RenderContext:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _remember(
        self,
        cache: OrderedDict[Any, Image.Image],
        key: Any,
        image: Image.Image,
    ) -> Image.Image:
        cache[key] = image
        cache.move_to_end(key)
        while len(cache) > self.limit:
            _, expired = cache.popitem(last=False)
            expired.close()
        return image

    def satellite(self, frame: SelectedFrame) -> Image.Image:
        key = (frame.source_path, frame.source_fetched_at)
        cached = self.satellites.get(key)
        if cached is not None:
            self.satellites.move_to_end(key)
            return cached
        with Image.open(_safe_path(self.source_root, frame.source_path)) as source:
            satellite = _crop_resize(
                source,
                self.spec.viewport,
                self.spec.width,
                self.spec.height,
            )
            composed = self.base.copy()
            composed.paste(
                satellite.convert("RGB"),
                (0, 0),
                satellite.getchannel("A"),
            )
        filtered = _operational_satellite_filter(composed)
        composed.close()
        return self._remember(self.satellites, key, filtered)

    def overlay(self, selection: ProxyLayerSelection) -> Image.Image:
        key = (selection.source_key, selection.stage_aligned)
        cached = self.overlays.get(key)
        if cached is not None:
            self.overlays.move_to_end(key)
            return cached
        with Image.open(_safe_path(self.source_root, selection.source_path)) as source:
            rendered = _crop_resize(
                source,
                self.spec.viewport,
                self.spec.width,
                self.spec.height,
                stage_aligned=selection.stage_aligned,
            ).convert("RGBA")
        return self._remember(self.overlays, key, rendered)


def _atomic_png(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp.png")
    try:
        image.save(temporary, "PNG", optimize=False, compress_level=1)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _proxy_descriptor(
    output_root: Path,
    entry: Mapping[str, Any],
) -> Mapping[str, Any]:
    relative = str(entry["path"])
    path = _safe_path(output_root, relative)
    return {
        "path": relative,
        "width": int(entry["width"]),
        "height": int(entry["height"]),
        "byteLength": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _combined_model_proxy(
    output_root: Path,
    spec: ProfileSpec,
    members: Sequence[tuple[str, Mapping[str, Any], float]],
) -> Mapping[str, Any]:
    """Combine MSLP then H500 into one lossless display-sized RGBA proxy."""
    composed = Image.new("RGBA", (spec.width, spec.height), (0, 0, 0, 0))
    try:
        for _recipe_id, descriptor, opacity in members:
            with Image.open(
                _safe_path(output_root, str(descriptor["path"]))
            ) as source:
                overlay = source.convert("RGBA")
            try:
                if overlay.size != (spec.width, spec.height):
                    resized = overlay.resize(
                        (spec.width, spec.height),
                        Image.Resampling.LANCZOS,
                    )
                    overlay.close()
                    overlay = resized
                if opacity < 1:
                    overlay.putalpha(
                        overlay.getchannel("A").point(
                            lambda value, factor=opacity: round(value * factor)
                        )
                    )
                composed.alpha_composite(overlay)
            finally:
                overlay.close()
        content_hash = hashlib.sha256(
            b"radarsat-model-contours-v1\0"
            + spec.width.to_bytes(4, "big")
            + spec.height.to_bytes(4, "big")
            + composed.tobytes()
        ).hexdigest()[:16]
        destination = (
            output_root
            / "video-proxies"
            / spec.product_id
            / "model-contours"
            / f"{content_hash}.webp"
        )
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.tmp.webp"
            )
            try:
                composed.save(
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
    finally:
        composed.close()
    return _proxy_descriptor(
        output_root,
        {
            "path": destination.relative_to(output_root).as_posix(),
            "width": spec.width,
            "height": spec.height,
        },
    )


def _hybrid_overlay_bundle(
    source_root: Path,
    output_root: Path,
    spec: ProfileSpec,
    frame_selections: Sequence[Sequence[ProxyLayerSelection]],
    eligible_layer_ids: Sequence[str],
    opacities: Mapping[str, float],
) -> tuple[Mapping[str, Mapping[str, Any]], list[list[Mapping[str, Any]]]]:
    """Freeze one sidecar generation's upper-layer proxy dependencies.

    Proxy keys are their immutable content-addressed paths rather than mutable
    catalog URLs. This lets the sidecar remain atomic and independently
    publishable even while the ordinary dynamic video manifest is rebuilding.
    """
    eligible = set(eligible_layer_ids)
    rendered: dict[tuple[str, str, bool], Mapping[str, Any]] = {}
    combined_models: dict[tuple[tuple[str, str, float], ...], Mapping[str, Any]] = {}
    proxies: dict[str, Mapping[str, Any]] = {}
    frames: list[list[Mapping[str, Any]]] = []
    for selections in frame_selections:
        entries: list[Mapping[str, Any]] = []
        by_recipe: dict[str, tuple[Mapping[str, Any], ProxyLayerSelection]] = {}
        for selection in selections:
            if selection.recipe_id not in eligible:
                continue
            cache_key = (
                selection.source_key,
                selection.rendered_layer_id,
                selection.stage_aligned,
            )
            descriptor = rendered.get(cache_key)
            if descriptor is None:
                descriptor = _proxy_descriptor(
                    output_root,
                    _render_proxy(
                        source_root,
                        output_root,
                        spec,
                        selection.rendered_layer_id,
                        selection.source_key,
                        selection.source_path,
                        stage_aligned=selection.stage_aligned,
                    ),
                )
                rendered[cache_key] = descriptor
            source_key = str(descriptor["path"])
            proxies[source_key] = descriptor
            entry = {
                **selection.manifest_entry(),
                "sourceKey": source_key,
            }
            entries.append(entry)
            by_recipe[selection.recipe_id] = (descriptor, selection)

        model_ids = ("model-mslp", "model-hgt500")
        if all(recipe_id in by_recipe for recipe_id in model_ids):
            members = [
                (
                    recipe_id,
                    by_recipe[recipe_id][0],
                    opacities.get(recipe_id, 1.0),
                )
                for recipe_id in model_ids
            ]
            combined_cache_key = tuple(
                (
                    str(descriptor["path"]),
                    str(descriptor["sha256"]),
                    opacity,
                )
                for _recipe_id, descriptor, opacity in members
            )
            combined = combined_models.get(combined_cache_key)
            if combined is None:
                combined = _combined_model_proxy(output_root, spec, members)
                combined_models[combined_cache_key] = combined
            combined_key = str(combined["path"])
            proxies[combined_key] = combined
            source_times = {
                recipe_id: (
                    _format_time(by_recipe[recipe_id][1].source_valid_time)
                    if by_recipe[recipe_id][1].source_valid_time is not None
                    else None
                )
                for recipe_id in model_ids
            }
            parsed_times = [
                by_recipe[recipe_id][1].source_valid_time
                for recipe_id in model_ids
                if by_recipe[recipe_id][1].source_valid_time is not None
            ]
            entries.append(
                {
                    "id": "model-contours",
                    "renderId": "model-contours",
                    "sourceKey": combined_key,
                    "sourceValidTime": (
                        _format_time(max(parsed_times)) if parsed_times else None
                    ),
                    "ids": list(model_ids),
                    "sourceValidTimes": source_times,
                }
            )
        frames.append(entries)
    return proxies, frames


def _asset_fingerprint(root: Path, relative: str) -> Mapping[str, Any]:
    path = _safe_path(root, relative)
    stat = path.stat()
    return {
        "path": relative,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
    }


def _frame_fingerprint(
    source_root: Path,
    spec: ProfileSpec,
    preset_id: str,
    layer_ids: Sequence[str],
    frame: SelectedFrame,
    selections: Sequence[ProxyLayerSelection],
    opacities: Mapping[str, float],
) -> str:
    base = _asset_fingerprint(
        source_root,
        f"static/{spec.domain_id}/base-dark.png",
    )
    overlays = [
        {
            "id": selection.recipe_id,
            "renderId": selection.rendered_layer_id,
            "sourceKey": selection.source_key,
            "source": _asset_fingerprint(source_root, selection.source_path),
            "stageAligned": selection.stage_aligned,
            "opacity": opacities.get(selection.recipe_id, 1.0),
        }
        for selection in selections
    ]
    return _hash_payload(
        {
            "cacheVersion": COMPOSITE_FRAME_CACHE_VERSION,
            "productId": spec.product_id,
            "satelliteLayerId": spec.layer_id,
            "trackCadenceMinutes": spec.cadence_minutes,
            "presetId": preset_id,
            "layerIds": list(layer_ids),
            "width": spec.width,
            "height": spec.height,
            "viewport": dict(spec.viewport),
            "satelliteFilter": "saturate(0.52) brightness(0.78) contrast(1.06)",
            "base": base,
            "satellite": _source_fingerprint(source_root, frame),
            "overlays": overlays,
            "clockPhase": int(
                frame.valid_time.timestamp() // (spec.cadence_minutes * 60)
            )
            % 2,
        },
        24,
    )


def _cache_path(
    output_root: Path,
    spec: ProfileSpec,
    preset_id: str,
    rendition_id: str,
    fingerprint: str,
) -> Path:
    return (
        output_root
        / "composite-frame-cache"
        / spec.product_id
        / spec.layer_id
        / preset_id
        / rendition_id
        / f"{fingerprint}.png"
    )


def _usable_cache(path: Path) -> bool:
    try:
        usable = path.stat().st_size > 0
        if usable:
            path.touch()
        return usable
    except OSError:
        return False


def _render_high_frame(
    context: _RenderContext,
    spec: ProfileSpec,
    layer_ids: Sequence[str],
    selections: Sequence[ProxyLayerSelection],
    frame: SelectedFrame,
    opacities: Mapping[str, float],
    destination: Path,
) -> None:
    stack_order = {layer_id: index for index, layer_id in enumerate(layer_ids)}
    composed = context.satellite(frame).convert("RGBA")
    try:
        for selection in sorted(
            selections,
            key=lambda value: stack_order.get(value.recipe_id, len(stack_order)),
        ):
            if selection.recipe_id not in stack_order:
                continue
            overlay = context.overlay(selection).copy()
            try:
                opacity = opacities.get(selection.recipe_id, 1.0)
                if opacity < 1:
                    overlay.putalpha(
                        overlay.getchannel("A").point(
                            lambda value, factor=opacity: round(value * factor)
                        )
                    )
                composed.alpha_composite(overlay)
            finally:
                overlay.close()
        clock_phase = int(
            frame.valid_time.timestamp() // (spec.cadence_minutes * 60)
        ) % 2
        encoded = Image.new(
            "RGB",
            (spec.width, spec.height + VIDEO_CLOCK_STRIP_HEIGHT),
            (255, 255, 255) if clock_phase else (0, 0, 0),
        )
        try:
            encoded.paste(composed.convert("RGB"), (0, 0))
            _atomic_png(destination, encoded)
        finally:
            encoded.close()
    finally:
        composed.close()


def _derived_fingerprint(
    high_fingerprint: str,
    spec: ProfileSpec,
    rendition_id: str,
    width: int,
    height: int,
) -> str:
    return _hash_payload(
        {
            "cacheVersion": COMPOSITE_FRAME_CACHE_VERSION,
            "source": high_fingerprint,
            "rendition": rendition_id,
            "width": width,
            "height": height,
            "clockStripHeight": VIDEO_CLOCK_STRIP_HEIGHT,
            "cadenceMinutes": spec.cadence_minutes,
        },
        24,
    )


def _derive_rendition(
    high_path: Path,
    high_content_height: int,
    width: int,
    height: int,
    destination: Path,
) -> None:
    with Image.open(high_path) as source:
        content = source.crop((0, 0, source.width, high_content_height)).convert("RGB")
    try:
        resized = content.resize((width, height), Image.Resampling.LANCZOS)
    finally:
        content.close()
    try:
        # Copy the hidden phase strip without spatially softening it. The high
        # cache uses a solid black or white strip, so one sampled pixel is exact.
        with Image.open(high_path) as source:
            phase_colour = source.getpixel((0, high_content_height))
        encoded = Image.new(
            "RGB",
            (width, height + VIDEO_CLOCK_STRIP_HEIGHT),
            phase_colour,
        )
        try:
            encoded.paste(resized, (0, 0))
            _atomic_png(destination, encoded)
        finally:
            encoded.close()
    finally:
        resized.close()


def _frame_layer_times(
    spec: ProfileSpec,
    frame: SelectedFrame,
    layer_ids: Sequence[str],
    selections: Sequence[ProxyLayerSelection],
) -> Mapping[str, str | None]:
    values: dict[str, str | None] = {layer_id: None for layer_id in layer_ids}
    if spec.layer_id in values:
        values[spec.layer_id] = _format_time(frame.source_valid_time)
    for selection in selections:
        if selection.recipe_id in values:
            values[selection.recipe_id] = (
                _format_time(selection.source_valid_time)
                if selection.source_valid_time is not None
                else None
            )
    return values


def _sidecar_pointer_path(
    output_root: Path,
    spec: ProfileSpec,
    preset_id: str,
    hours: int,
) -> Path:
    return (
        output_root
        / "composite-index"
        / spec.product_id
        / spec.layer_id
        / spec.track
        / preset_id
        / f"{hours}.json"
    )


def _sidecar_manifest_path(
    output_root: Path,
    spec: ProfileSpec,
    preset_id: str,
    hours: int,
    generation: str,
) -> Path:
    return (
        output_root
        / "composite-manifests"
        / spec.product_id
        / spec.layer_id
        / spec.track
        / preset_id
        / str(hours)
        / f"{generation}.json"
    )


def _pointer_payload(
    spec: ProfileSpec,
    preset_id: str,
    layer_ids: Sequence[str],
    hours: int,
    generation: str,
    manifest_path: Path,
    output_root: Path,
    generated_at: str,
    end_valid_time: str,
    end_source_time: str,
    *,
    composite_kind: str = "exact",
    eligible_overlay_layer_ids: Sequence[str] = (),
) -> Mapping[str, Any]:
    schema_version = (
        HYBRID_COMPOSITE_SIDECAR_SCHEMA_VERSION
        if composite_kind == "hybrid-prefix"
        else COMPOSITE_SIDECAR_SCHEMA_VERSION
    )
    payload: dict[str, Any] = {
        "schemaVersion": schema_version,
        "productId": spec.product_id,
        "layerId": spec.layer_id,
        "track": spec.track,
        "presetId": preset_id,
        "layerIds": list(layer_ids),
        "renditionPolicy": "high-only",
        "rangeHours": hours,
        "generation": generation,
        "manifestPath": manifest_path.relative_to(output_root).as_posix(),
        "generatedAt": generated_at,
        "endValidTime": end_valid_time,
        "endSourceTime": end_source_time,
    }
    if composite_kind == "hybrid-prefix":
        payload.update(
            {
                "compositeKind": composite_kind,
                "bakedLayerIds": list(layer_ids),
                "eligibleOverlayLayerIds": list(eligible_overlay_layer_ids),
            }
        )
    return payload


def _requested_ranges(spec: ProfileSpec, ranges: Iterable[int] | None) -> tuple[int, ...]:
    configured = VIDEO_EXACT_RANGES.get(spec.product_id, ())
    track_ranges = tuple(
        value
        for value in configured
        if (spec.track == "live" and value < 24)
        or (spec.track == "day" and value == 24)
    )
    if ranges is None:
        return track_ranges
    requested = set(ranges)
    return tuple(value for value in track_ranges if value in requested)


def _build_range_sidecar(
    output_root: Path,
    spec: ProfileSpec,
    preset_id: str,
    layer_ids: Sequence[str],
    hours: int,
    selected: Sequence[SelectedFrame],
    frame_plans: Sequence[CompositeFrame],
    rendition_paths: Mapping[str, Sequence[Path]],
    rendition_fingerprints: Mapping[str, Sequence[str]],
    composite_kind: str,
    eligible_overlay_layer_ids: Sequence[str],
    overlay_proxies: Mapping[str, Mapping[str, Any]],
    frame_overlay_layers: Sequence[Sequence[Mapping[str, Any]]],
    *,
    ffmpeg: str,
    generated_at: str,
) -> Mapping[str, Any]:
    first, range_frames = _range_frames(selected, hours)
    range_plans = list(frame_plans[first:])
    range_overlay_layers = list(frame_overlay_layers[first:])
    if len(range_frames) < 2 or len(range_plans) != len(range_frames):
        raise RuntimeError(
            f"{spec.product_id} {preset_id} {hours}h has fewer than two frames"
        )
    nominal = max(
        1 / VIDEO_FRAME_RATE,
        spec.cadence_minutes * METEOROLOGICAL_MINUTE_SECONDS,
    )
    durations = [nominal] * len(range_frames)
    durations[-1] *= 4
    renditions: list[Mapping[str, Any]] = []
    for rendition_id, width, height in _exact_renditions(spec):
        images = list(rendition_paths[rendition_id][first:])
        cache_fingerprints = list(rendition_fingerprints[rendition_id][first:])
        rendition_spec = replace(
            spec,
            width=width,
            height=height,
            media_group="stage-composite-sidecar",
            media_viewport=dict(spec.viewport),
            media_width=width,
            media_height=height,
            crf=COMPOSITE_VIDEO_CRF,
        )
        images_by_valid_time = {
            frame.valid_time: image for frame, image in zip(range_frames, images, strict=True)
        }

        def prepared_images(
            group_frames: Sequence[SelectedFrame],
            _temporary: Path,
        ) -> list[Path]:
            return [images_by_valid_time[frame.valid_time] for frame in group_frames]

        media_variant = {
            "schemaVersion": COMPOSITE_SIDECAR_SCHEMA_VERSION,
            "frameCacheVersion": COMPOSITE_FRAME_CACHE_VERSION,
            "productId": spec.product_id,
            "layerId": spec.layer_id,
            "presetId": preset_id,
            "layerIds": list(layer_ids),
            "rendition": rendition_id,
            "width": width,
            "height": height,
            "crf": COMPOSITE_VIDEO_CRF,
            "preset": spec.preset,
        }
        destination, media_fingerprint, segments, _ = _build_hls_media(
            output_root,
            output_root,
            rendition_spec,
            range_frames,
            [{"frameCacheFingerprint": value} for value in cache_fingerprints],
            durations,
            ffmpeg=ffmpeg,
            owner=f"composite-{spec.product_id}-{preset_id}",
            variant=media_variant,
            prepare_images=prepared_images,
            # The same one-hour namespace is used by 3/6/12-hour playlists.
            # Their overlapping encoded segments therefore exist only once.
            media_track="day",
            segment_group_hours=1,
        )
        renditions.append(
            {
                "id": rendition_id,
                "media": {
                    "path": destination.relative_to(output_root).as_posix(),
                    "mimeType": "application/vnd.apple.mpegurl",
                    "codec": "avc1",
                    "width": width,
                    "height": height + VIDEO_CLOCK_STRIP_HEIGHT,
                    "contentHeight": height,
                    "byteLength": destination.stat().st_size,
                    "sha256": _sha256_file(destination),
                    "fingerprint": media_fingerprint,
                    "segments": segments,
                },
            }
        )

    frames: list[Mapping[str, Any]] = []
    referenced_proxy_keys: set[str] = set()
    for plan, duration, overlay_layers in zip(
        range_plans,
        durations,
        range_overlay_layers,
        strict=True,
    ):
        frame_value: dict[str, Any] = {
            "validTime": _format_time(plan.frame.valid_time),
            "sourceValidTime": _format_time(plan.frame.source_valid_time),
            **(
                {"sourceTimes": dict(plan.frame.source_times)}
                if plan.frame.source_times
                else {}
            ),
            "layerSourceTimes": _frame_layer_times(
                spec,
                plan.frame,
                layer_ids,
                plan.selections,
            ),
            "durationSeconds": round(duration, 6),
        }
        if composite_kind == "hybrid-prefix":
            frozen_layers = [dict(value) for value in overlay_layers]
            frame_value["proxyLayers"] = frozen_layers
            referenced_proxy_keys.update(
                str(value.get("sourceKey", "")) for value in frozen_layers
            )
        frames.append(frame_value)
    end_valid_time = _format_time(range_frames[-1].valid_time)
    end_source_time = _format_time(range_frames[-1].source_valid_time)
    schema_version = (
        HYBRID_COMPOSITE_SIDECAR_SCHEMA_VERSION
        if composite_kind == "hybrid-prefix"
        else COMPOSITE_SIDECAR_SCHEMA_VERSION
    )
    manifest_basis = {
        "schemaVersion": schema_version,
        "productId": spec.product_id,
        "domainId": spec.domain_id,
        "layerId": spec.layer_id,
        "track": spec.track,
        "presetId": preset_id,
        "layerIds": list(layer_ids),
        "rangeHours": hours,
        "cadenceMinutes": spec.cadence_minutes,
        "viewport": dict(spec.viewport),
        "mediaViewport": dict(spec.viewport),
        "endValidTime": end_valid_time,
        "endSourceTime": end_source_time,
        "boundaryIntervalMultiplier": 4,
        "renditionPolicy": "high-only",
        "frames": frames,
        "renditions": renditions,
    }
    if composite_kind == "hybrid-prefix":
        manifest_basis.update(
            {
                "compositeKind": composite_kind,
                "bakedLayerIds": list(layer_ids),
                "eligibleOverlayLayerIds": list(eligible_overlay_layer_ids),
                "proxyRenderVersion": PROXY_RENDER_VERSION,
                "proxies": {
                    key: dict(overlay_proxies[key])
                    for key in sorted(referenced_proxy_keys)
                    if key in overlay_proxies
                },
            }
        )
    generation = f"{range_frames[-1].valid_time.strftime('%Y%m%dT%H%MZ')}-{_hash_payload(manifest_basis)}"
    manifest_path = _sidecar_manifest_path(
        output_root,
        spec,
        preset_id,
        hours,
        generation,
    )
    manifest = {
        "schemaVersion": schema_version,
        "generation": generation,
        "generatedAt": generated_at,
        **{key: value for key, value in manifest_basis.items() if key != "schemaVersion"},
    }
    if not manifest_path.is_file():
        _atomic_json(manifest_path, manifest)
    pointer_path = _sidecar_pointer_path(
        output_root,
        spec,
        preset_id,
        hours,
    )
    pointer = _pointer_payload(
        spec,
        preset_id,
        layer_ids,
        hours,
        generation,
        manifest_path,
        output_root,
        generated_at,
        end_valid_time,
        end_source_time,
        composite_kind=composite_kind,
        eligible_overlay_layer_ids=eligible_overlay_layer_ids,
    )
    previous: Mapping[str, Any] = {}
    try:
        loaded = json.loads(pointer_path.read_text())
        previous = loaded if isinstance(loaded, Mapping) else {}
    except (OSError, ValueError):
        pass
    status = "unchanged" if previous.get("generation") == generation else "built"
    if status == "built":
        _atomic_json(pointer_path, pointer)
    return {
        "status": status,
        "productId": spec.product_id,
        "layerId": spec.layer_id,
        "track": spec.track,
        "presetId": preset_id,
        "rangeHours": hours,
        "generation": generation,
        "manifestPath": manifest_path.relative_to(output_root).as_posix(),
        "pointerPath": pointer_path.relative_to(output_root).as_posix(),
        "frames": len(range_frames),
        "mediaBytes": sum(
            int(value["media"]["byteLength"])
            + sum(int(segment["byteLength"]) for segment in value["media"]["segments"])
            for value in renditions
        ),
    }


def build_composite_profile(
    source_root: Path,
    output_root: Path,
    catalog: Mapping[str, Any],
    spec: ProfileSpec,
    *,
    ffmpeg: str,
    ranges: Iterable[int] | None = None,
    preset_ids: Iterable[str] | None = None,
    now: dt.datetime | None = None,
) -> Mapping[str, Any]:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    build_now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    requested_ranges = _requested_ranges(spec, ranges)
    if not requested_ranges:
        return {"status": "skipped", "profiles": [], "failures": []}
    # Preserve the existing exact-only default. Reusable cores are an explicit
    # lower-priority lane selected with ``preset_ids``/``--preset`` so their
    # proxy preparation can never delay an operational exact commit.
    presets = _composite_presets(
        spec,
        preset_ids,
        include_hybrid=preset_ids is not None,
    )
    if not presets:
        raise ValueError(
            f"{spec.product_id}/{spec.layer_id} is not an operational default composite"
        )
    selected = _selected_satellite_frames(
        catalog,
        spec,
        max(requested_ranges),
        now=build_now,
    )
    selected = [
        frame
        for frame in selected
        if (source_root / frame.source_path).is_file()
    ]
    if len(selected) < 2:
        raise RuntimeError(f"{spec.product_id} has fewer than two usable satellite frames")

    all_selections = _proxy_selections(catalog, spec, selected)
    # Optional layers can rotate after the catalog snapshot. Omit those inputs
    # for this immutable generation; a subsequent run upgrades the sidecar.
    all_selections = [
        [
            selection
            for selection in selections
            if (source_root / selection.source_path).is_file()
        ]
        for selections in all_selections
    ]
    opacities = _recipe_opacities(spec.product_id)
    rendition_specs = _exact_renditions(spec)
    high_id, high_width, high_height = max(
        rendition_specs,
        key=lambda value: value[1] * value[2],
    )
    if (high_width, high_height) != (spec.width, spec.height):
        raise RuntimeError("Composite source profile is not the highest rendition")

    plans_by_preset: dict[str, list[CompositeFrame]] = {}
    layers_by_preset: dict[str, tuple[str, ...]] = {}
    kind_by_preset: dict[str, str] = {}
    eligible_layers_by_preset: dict[str, tuple[str, ...]] = {}
    proxies_by_preset: dict[str, Mapping[str, Mapping[str, Any]]] = {}
    overlay_frames_by_preset: dict[
        str, list[list[Mapping[str, Any]]]
    ] = {}
    failures: list[Mapping[str, Any]] = []
    for preset_id, layer_ids in presets:
        composite_kind = video_composite_kind(spec.product_id, preset_id)
        eligible_overlay_layer_ids = video_composite_overlay_layer_ids(
            spec.product_id,
            spec.layer_id,
            preset_id,
        )
        if composite_kind == "hybrid-prefix" and not eligible_overlay_layer_ids:
            failures.extend(
                {
                    "productId": spec.product_id,
                    "layerId": spec.layer_id,
                    "track": spec.track,
                    "presetId": preset_id,
                    "rangeHours": hours,
                    "error": "ValueError: hybrid composite is not a strict recipe prefix",
                }
                for hours in requested_ranges
            )
            continue
        selected_ids = set(layer_ids)
        plans: list[CompositeFrame] = []
        try:
            for frame, selections in zip(selected, all_selections, strict=True):
                active = tuple(
                    selection
                    for selection in selections
                    if selection.recipe_id in selected_ids
                )
                fingerprint = _frame_fingerprint(
                    source_root,
                    spec,
                    preset_id,
                    layer_ids,
                    frame,
                    active,
                    opacities,
                )
                plans.append(
                    CompositeFrame(
                        frame=frame,
                        selections=active,
                        fingerprint=fingerprint,
                        high_path=_cache_path(
                            output_root,
                            spec,
                            preset_id,
                            high_id,
                            fingerprint,
                        ),
                    )
                )
        except Exception as error:
            failures.extend(
                {
                    "productId": spec.product_id,
                    "layerId": spec.layer_id,
                    "track": spec.track,
                    "presetId": preset_id,
                    "rangeHours": hours,
                    "error": f"{type(error).__name__}: {error}",
                }
                for hours in requested_ranges
            )
            continue
        plans_by_preset[preset_id] = plans
        layers_by_preset[preset_id] = tuple(layer_ids)
        kind_by_preset[preset_id] = composite_kind
        eligible_layers_by_preset[preset_id] = eligible_overlay_layer_ids
        if composite_kind == "hybrid-prefix":
            try:
                proxies, overlay_frames = _hybrid_overlay_bundle(
                    source_root,
                    output_root,
                    spec,
                    all_selections,
                    eligible_overlay_layer_ids,
                    opacities,
                )
            except Exception as error:
                failures.extend(
                    {
                        "productId": spec.product_id,
                        "layerId": spec.layer_id,
                        "track": spec.track,
                        "presetId": preset_id,
                        "rangeHours": hours,
                        "error": f"{type(error).__name__}: {error}",
                    }
                    for hours in requested_ranges
                )
                plans_by_preset.pop(preset_id, None)
                layers_by_preset.pop(preset_id, None)
                kind_by_preset.pop(preset_id, None)
                eligible_layers_by_preset.pop(preset_id, None)
                continue
            proxies_by_preset[preset_id] = proxies
            overlay_frames_by_preset[preset_id] = overlay_frames
        else:
            proxies_by_preset[preset_id] = {}
            overlay_frames_by_preset[preset_id] = [
                [] for _frame in selected
            ]

    # Fill missing high-resolution cache entries frame-by-frame. This ordering
    # lets the two common presets share the currently decoded satellite and
    # overlay sources during a cold build.
    preset_errors: dict[str, Exception] = {}
    with _RenderContext(source_root, spec) as context:
        for index, frame in enumerate(selected):
            for preset_id, plans in plans_by_preset.items():
                if preset_id in preset_errors:
                    continue
                plan = plans[index]
                if _usable_cache(plan.high_path):
                    continue
                try:
                    _render_high_frame(
                        context,
                        spec,
                        layers_by_preset[preset_id],
                        plan.selections,
                        frame,
                        opacities,
                        plan.high_path,
                    )
                except Exception as error:
                    preset_errors[preset_id] = error

    profiles: list[Mapping[str, Any]] = []
    generated_at = _format_time(build_now)
    for preset_id, plans in plans_by_preset.items():
        if preset_id in preset_errors:
            error = preset_errors[preset_id]
            failures.extend(
                {
                    "productId": spec.product_id,
                    "layerId": spec.layer_id,
                    "track": spec.track,
                    "presetId": preset_id,
                    "rangeHours": hours,
                    "error": f"{type(error).__name__}: {error}",
                }
                for hours in requested_ranges
            )
            continue
        rendition_paths: dict[str, list[Path]] = {high_id: []}
        rendition_fingerprints: dict[str, list[str]] = {high_id: []}
        try:
            for plan in plans:
                rendition_paths[high_id].append(plan.high_path)
                rendition_fingerprints[high_id].append(plan.fingerprint)
            for rendition_id, width, height in rendition_specs:
                if rendition_id == high_id:
                    continue
                rendition_paths[rendition_id] = []
                rendition_fingerprints[rendition_id] = []
                for plan in plans:
                    fingerprint = _derived_fingerprint(
                        plan.fingerprint,
                        spec,
                        rendition_id,
                        width,
                        height,
                    )
                    destination = _cache_path(
                        output_root,
                        spec,
                        preset_id,
                        rendition_id,
                        fingerprint,
                    )
                    if not _usable_cache(destination):
                        _derive_rendition(
                            plan.high_path,
                            spec.height,
                            width,
                            height,
                            destination,
                        )
                    rendition_paths[rendition_id].append(destination)
                    rendition_fingerprints[rendition_id].append(fingerprint)
        except Exception as error:
            failures.extend(
                {
                    "productId": spec.product_id,
                    "layerId": spec.layer_id,
                    "track": spec.track,
                    "presetId": preset_id,
                    "rangeHours": hours,
                    "error": f"{type(error).__name__}: {error}",
                }
                for hours in requested_ranges
            )
            continue

        for hours in requested_ranges:
            try:
                profiles.append(
                    _build_range_sidecar(
                        output_root,
                        spec,
                        preset_id,
                        layers_by_preset[preset_id],
                        hours,
                        selected,
                        plans,
                        rendition_paths,
                        rendition_fingerprints,
                        kind_by_preset[preset_id],
                        eligible_layers_by_preset[preset_id],
                        proxies_by_preset[preset_id],
                        overlay_frames_by_preset[preset_id],
                        ffmpeg=ffmpeg,
                        generated_at=generated_at,
                    )
                )
            except Exception as error:
                # The mutable pointer is only changed inside the successful
                # path, so this range retains its independently last-good loop.
                failures.append(
                    {
                        "productId": spec.product_id,
                        "layerId": spec.layer_id,
                        "track": spec.track,
                        "presetId": preset_id,
                        "rangeHours": hours,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
    return {
        "status": "warning" if failures else "ok",
        "profiles": profiles,
        "failures": failures,
        "framesSelected": len(selected),
    }


def prune_composite_frame_cache(
    output_root: Path,
    *,
    max_age_hours: float = COMPOSITE_FRAME_CACHE_MAX_AGE_HOURS,
    max_bytes: int = COMPOSITE_FRAME_CACHE_MAX_BYTES,
    now: dt.datetime | None = None,
) -> int:
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    cache_root = output_root.resolve() / "composite-frame-cache"
    if not cache_root.is_dir():
        return 0
    current = (now or dt.datetime.now(UTC)).timestamp()
    cutoff = current - max_age_hours * 3600
    removed = 0
    retained: list[tuple[float, int, Path]] = []
    retained_bytes = 0
    for path in cache_root.rglob("*.png"):
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                path.unlink()
                removed += 1
            else:
                retained.append((stat.st_mtime, stat.st_size, path))
                retained_bytes += stat.st_size
        except FileNotFoundError:
            continue
    # Cache hits touch mtime, so this is a durable least-recently-used pass.
    # HLS segments and manifests are authoritative; evicted PNGs are merely
    # regenerated if a future segment actually needs them.
    for _mtime, size, path in sorted(retained):
        if retained_bytes <= max_bytes:
            break
        try:
            path.unlink()
            removed += 1
            retained_bytes -= size
        except FileNotFoundError:
            continue
    for directory in sorted(
        (path for path in cache_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def prune_composite_sidecar_manifests(
    output_root: Path,
    *,
    now: dt.datetime | None = None,
) -> int:
    """Retain the current immutable sidecar generation plus a short grace.

    Media deletion remains centralized in ``prune_shared_video_orphans`` so a
    manifest is always removed before any media it references. The short grace
    protects browsers that began loading a generation just before publication.
    """
    output_root = output_root.resolve()
    manifest_root = output_root / "composite-manifests"
    if not manifest_root.is_dir():
        return 0
    current = (now or dt.datetime.now(UTC)).timestamp()
    grace_seconds = COMPOSITE_MANIFEST_GRACE_HOURS * 3600
    removed = 0
    range_directories = [
        path
        for path in manifest_root.rglob("*")
        if path.is_dir() and path.name.isdigit()
    ]
    for directory in range_directories:
        manifests = list(directory.glob("*.json"))
        if not manifests:
            continue
        parts = directory.relative_to(manifest_root).parts
        if len(parts) != 5:
            continue
        product_id, layer_id, track, preset_id, range_hours = parts
        configured_presets = {
            str(value.get("id"))
            for value in VIDEO_COMPOSITE_PRESETS.get(product_id, ())
            if isinstance(value, Mapping) and isinstance(value.get("id"), str)
        }
        try:
            numeric_range = int(range_hours)
        except ValueError:
            numeric_range = -1
        expected_track = (
            "day" if numeric_range == 24
            else "live" if numeric_range in {3, 6, 12}
            else ""
        )
        group_is_configured = (
            preset_id in configured_presets
            and track in VIDEO_TRACKS_BY_PRODUCT.get(product_id, ())
            and numeric_range in VIDEO_EXACT_RANGES.get(product_id, ())
            and track == expected_track
        )
        pointer_path = (
            output_root
            / "composite-index"
            / product_id
            / layer_id
            / track
            / preset_id
            / f"{range_hours}.json"
        )
        current_manifest = ""
        try:
            pointer = json.loads(pointer_path.read_bytes())
            if isinstance(pointer, Mapping):
                current_manifest = str(pointer.get("manifestPath", ""))
        except (OSError, json.JSONDecodeError):
            pass
        # The hash suffix is content-derived, not an ordering token.  In
        # particular, two rebuilds of the same meteorological anchor can sort
        # in the opposite order from their commits.  Rank immutable manifests
        # by their declared build/commit time and use mtime only as a
        # crash-compatible fallback for older files.
        def commit_order(path: Path) -> tuple[float, int]:
            try:
                payload = json.loads(path.read_bytes())
                generated = (
                    payload.get("generatedAt")
                    if isinstance(payload, Mapping)
                    else None
                )
                if isinstance(generated, str):
                    value = dt.datetime.fromisoformat(
                        generated.replace("Z", "+00:00")
                    ).astimezone(UTC)
                    return value.timestamp(), path.stat().st_mtime_ns
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            try:
                stat = path.stat()
                return stat.st_mtime, stat.st_mtime_ns
            except OSError:
                return float("-inf"), -1

        current_path: Path | None = None
        if group_is_configured and current_manifest:
            candidate = output_root / current_manifest
            if candidate.parent == directory:
                current_path = candidate
        retained = {current_path} if current_path in manifests else set()
        previous_candidates = [
            manifest for manifest in manifests if manifest not in retained
        ]
        previous_candidates.sort(key=commit_order, reverse=True)
        if group_is_configured:
            retained.update(
                previous_candidates[
                    : max(0, COMPOSITE_LOCAL_GENERATIONS_TO_KEEP - len(retained))
                ]
            )
        for manifest in manifests:
            if manifest in retained:
                continue
            try:
                if current - manifest.stat().st_mtime <= grace_seconds:
                    continue
                manifest.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        if not group_is_configured and not any(directory.glob("*.json")):
            pointer_path.unlink(missing_ok=True)
    return removed


def build_composite_videos(
    source_root: Path,
    output_root: Path | None = None,
    *,
    product_ids: Iterable[str] | None = None,
    layer_ids: Iterable[str] | None = None,
    track_names: Iterable[str] | None = None,
    ranges: Iterable[int] | None = None,
    preset_ids: Iterable[str] | None = None,
    ffmpeg: str | None = None,
    now: dt.datetime | None = None,
    prune_cache: bool = True,
) -> Mapping[str, Any]:
    source_root = source_root.resolve()
    output_root = (output_root or source_root).resolve()
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg with libx264 is required to build composite video")
    requested_products = set(
        product_ids or (spec.product_id for spec in VIDEO_PROFILES)
    )
    requested_layers = set(layer_ids or (spec.layer_id for spec in VIDEO_PROFILES))
    requested_tracks = set(track_names or ("live", "day"))
    configured_preset_ids = {
        str(value.get("id"))
        for values in VIDEO_COMPOSITE_PRESETS.values()
        for value in values
        if isinstance(value, Mapping) and isinstance(value.get("id"), str)
    }
    requested_presets = set(preset_ids) if preset_ids is not None else None
    if requested_presets is not None:
        unknown_presets = requested_presets.difference(configured_preset_ids)
        if unknown_presets:
            raise ValueError(f"Unsupported composite presets: {sorted(unknown_presets)}")
    if not requested_tracks.issubset({"live", "day"}):
        raise ValueError("Composite sidecars support only live and day tracks")
    if ranges is not None:
        requested_ranges = set(ranges)
        unknown_ranges = requested_ranges.difference({3, 6, 12, 24})
        if unknown_ranges:
            raise ValueError(f"Unsupported exact ranges: {sorted(unknown_ranges)}")
    else:
        requested_ranges = None
    profiles = [
        spec
        for spec in VIDEO_PROFILES
        if spec.product_id in requested_products
        and spec.layer_id in requested_layers
        and spec.track in requested_tracks
        and _composite_presets(
            spec,
            requested_presets,
            include_hybrid=requested_presets is not None,
        )
        and _requested_ranges(spec, requested_ranges)
    ]
    catalog = build_catalog(source_root)
    results: list[Mapping[str, Any]] = []
    failures: list[Mapping[str, Any]] = []
    for spec in profiles:
        try:
            result = build_composite_profile(
                source_root,
                output_root,
                catalog,
                spec,
                ffmpeg=executable,
                ranges=requested_ranges,
                preset_ids=requested_presets,
                now=now,
            )
            results.extend(result["profiles"])
            failures.extend(result["failures"])
        except Exception as error:
            failures.append(
                {
                    "productId": spec.product_id,
                    "layerId": spec.layer_id,
                    "track": spec.track,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    removed = prune_composite_frame_cache(output_root, now=now) if prune_cache else 0
    removed_manifests = (
        prune_composite_sidecar_manifests(output_root, now=now)
        if prune_cache
        else 0
    )
    return {
        "status": "warning" if failures else "ok",
        "sourceRoot": str(source_root),
        "outputRoot": str(output_root),
        "profiles": results,
        "failures": failures,
        "prunedCacheFrames": removed,
        "prunedManifests": removed_manifests,
    }
