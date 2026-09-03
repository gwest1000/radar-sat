from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .catalog import PUBLIC_VIDEO_LAYERS
from .config import (
    VIDEO_ARCHIVE_PRODUCTS,
    VIDEO_COMPOSITE_PRESETS,
    VIDEO_EXACT_RANGES,
    video_composite_kind,
)
from .geomet import format_utc, parse_utc


UTC = dt.timezone.utc
# Monitor the satellite product the BC viewer actually selects by default.
REQUIRED_LAYERS = ("eccc-geocolor", "radar-rain", "ptype", "lightning")


def directory_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def storage_breakdown(root: Path) -> dict[str, int]:
    categories = {
        "compositeCacheBytes": 0,
        "videoSegmentBytes": 0,
        "videoBytes": 0,
        "videoProxyBytes": 0,
        "sourceFrameBytes": 0,
        "otherBytes": 0,
    }
    roots = {
        "composite-frame-cache": "compositeCacheBytes",
        "video-segments": "videoSegmentBytes",
        "videos": "videoBytes",
        "video-proxies": "videoProxyBytes",
        "video-proxy-index": "videoProxyBytes",
        "frames": "sourceFrameBytes",
        "metadata": "sourceFrameBytes",
    }
    if not root.exists():
        return {"totalBytes": 0, **categories}
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            relative = path.relative_to(root)
        except (OSError, ValueError):
            continue
        category = roots.get(relative.parts[0], "otherBytes")
        categories[category] += size
    return {"totalBytes": sum(categories.values()), **categories}


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as error:
        raise RuntimeError(f"missing {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error


def status_age_issue(
    payload: dict[str, Any], label: str, now: dt.datetime, max_age_minutes: int
) -> str | None:
    if payload.get("status") not in {"ok", "warning"}:
        return f"{label} status is {payload.get('status', 'unknown')}"
    try:
        updated = parse_utc(str(payload["updatedAt"]))
    except (KeyError, ValueError):
        return f"{label} status has no valid updatedAt"
    age = now - updated
    if age > dt.timedelta(minutes=max_age_minutes):
        return f"{label} has not succeeded for {age.total_seconds() / 60:.0f} minutes"
    return None


def recent_layer_source_count(
    frames: list[object],
    layer_id: str,
    *,
    window_minutes: int = 60,
) -> int:
    parsed: list[tuple[dt.datetime, str]] = []
    for value in frames:
        if not isinstance(value, dict):
            continue
        source_times = value.get("layerSourceTimes")
        source_time = source_times.get(layer_id) if isinstance(source_times, dict) else None
        if not source_time:
            continue
        try:
            parsed.append((parse_utc(str(value["validTime"])), str(source_time)))
        except (KeyError, ValueError):
            continue
    if not parsed:
        return 0
    cutoff = max(valid for valid, _source in parsed) - dt.timedelta(minutes=window_minutes)
    return len({source for valid, source in parsed if valid >= cutoff})


def inspect_health(
    output_root: Path,
    publish_status_path: Path,
    *,
    now: dt.datetime | None = None,
    require_publish: bool = True,
    service_max_age_minutes: int = 15,
    local_warn_bytes: int | None = None,
    local_max_bytes: int | None = None,
    disk_warn_free_bytes: int | None = None,
    disk_min_free_bytes: int | None = None,
) -> dict[str, Any]:
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    warnings: list[str] = []
    errors: list[str] = []
    ingest_status_path = output_root / "status" / "ingest.json"

    try:
        ingest = read_json(ingest_status_path)
        if ingest.get("status") == "warning":
            details = ingest.get("warnings", [])
            suffix = f": {details[0]}" if isinstance(details, list) and details else ""
            warnings.append(f"ingest completed with source warnings{suffix}")
        issue = status_age_issue(ingest, "ingest", now, service_max_age_minutes)
        if issue:
            errors.append(issue)
    except RuntimeError as error:
        errors.append(str(error))

    if require_publish:
        try:
            publication = read_json(publish_status_path)
            issue = status_age_issue(
                publication, "publication", now, service_max_age_minutes
            )
            if issue:
                errors.append(issue)
            projected = publication.get("projectedBytes")
            if isinstance(projected, (int, float)) and not isinstance(projected, bool):
                r2_warning = int(os.environ.get("RADARSAT_R2_WARN_BYTES", 9_000_000_000))
                r2_maximum = int(os.environ.get("RADARSAT_R2_MAX_BYTES", 9_800_000_000))
                if projected >= r2_maximum:
                    errors.append(
                        f"projected R2 storage is {projected / 1_000_000_000:.2f} GB "
                        f"(guard {r2_maximum / 1_000_000_000:.2f} GB)"
                    )
                elif projected >= r2_warning:
                    warnings.append(
                        f"projected R2 storage is {projected / 1_000_000_000:.2f} GB "
                        f"(warning {r2_warning / 1_000_000_000:.2f} GB)"
                    )
        except RuntimeError as error:
            errors.append(str(error))

    frame_counts: dict[str, int] = {}
    video_coverage: dict[str, dict[str, object]] = {}
    try:
        catalog = read_json(output_root / "catalog.json")
        generated = parse_utc(str(catalog["generatedAt"]))
        catalog_age = now - generated
        if catalog_age > dt.timedelta(minutes=service_max_age_minutes):
            errors.append(
                f"catalog has not regenerated for {catalog_age.total_seconds() / 60:.0f} minutes"
            )
        layers = catalog.get("domains", {}).get("bc", {}).get("layers", {})
        for layer_id in REQUIRED_LAYERS:
            layer = layers.get(layer_id, {})
            frames = layer.get("frames", [])
            frame_counts[layer_id] = len(frames)
            if not frames:
                errors.append(f"bc/{layer_id} has no frames")
                continue
            try:
                latest = max(parse_utc(str(frame["validTime"])) for frame in frames)
                maximum = int(layer.get("maxAgeMinutes", 30))
            except (KeyError, TypeError, ValueError):
                errors.append(f"bc/{layer_id} has invalid frame metadata")
                continue
            age = now - latest
            if age > dt.timedelta(minutes=maximum):
                errors.append(
                    f"bc/{layer_id} latest source is {age.total_seconds() / 60:.0f} minutes old "
                    f"(limit {maximum})"
                )

        video_profiles = catalog.get("videoProfiles", {})
        composite_profiles = catalog.get("compositeProfiles", {})
        product_configs = {
            str(value.get("id")): value
            for value in catalog.get("products", [])
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        }
        freshness_minutes = {3: 20, 6: 25, 12: 40, 24: 40}
        for product_id, layer_id in PUBLIC_VIDEO_LAYERS.items():
            configured_presets = {
                str(value.get("id")): video_composite_kind(
                    product_id, str(value.get("id"))
                )
                for value in VIDEO_COMPOSITE_PRESETS.get(product_id, ())
                if isinstance(value, dict)
            }
            expected_presets = {
                preset_id
                for preset_id, kind in configured_presets.items()
                if kind == "exact"
            }
            expected_hybrids = {
                preset_id
                for preset_id, kind in configured_presets.items()
                if kind == "hybrid-prefix"
            }
            product_status: dict[str, object] = {"exact": {}}
            if expected_hybrids:
                product_status["hybrid"] = {}
            product_config = product_configs.get(product_id, {})
            domain_id = str(product_config.get("domain", ""))
            source_frames = (
                catalog.get("domains", {})
                .get(domain_id, {})
                .get("layers", {})
                .get(layer_id, {})
                .get("frames", [])
            )
            newest_source: dt.datetime | None = None
            if isinstance(source_frames, list) and source_frames:
                latest_frame = source_frames[-1]
                if isinstance(latest_frame, dict):
                    try:
                        newest_source = parse_utc(str(
                            latest_frame.get("sourceValidTime")
                            or latest_frame.get("validTime")
                        ))
                    except ValueError:
                        pass
            tracks = (
                composite_profiles.get(product_id, {}).get(layer_id, {})
                if isinstance(composite_profiles, dict)
                else {}
            )
            for hours in VIDEO_EXACT_RANGES.get(product_id, ()):
                track = "day" if hours == 24 else "live"
                pointers = tracks.get(track, []) if isinstance(tracks, dict) else []
                for status_key, expected, label_kind in (
                    ("exact", expected_presets, "prebuilt composite"),
                    ("hybrid", expected_hybrids, "hybrid core"),
                ):
                    if not expected:
                        continue
                    range_status: dict[str, object] = {}
                    for preset_id in sorted(expected):
                        pointer = next((
                            value for value in pointers
                            if isinstance(value, dict)
                            and value.get("presetId") == preset_id
                            and value.get("rangeHours") == hours
                        ), None) if isinstance(pointers, list) else None
                        label = f"{product_id}/{hours}h/{preset_id}"
                        if not isinstance(pointer, dict) or not isinstance(pointer.get("manifestPath"), str):
                            warnings.append(f"{label} has no {label_kind}")
                            range_status[preset_id] = "missing"
                            continue
                        try:
                            manifest_path = output_root / str(pointer["manifestPath"])
                            manifest = read_json(manifest_path)
                            frames = manifest.get("frames", [])
                            if not isinstance(frames, list) or len(frames) < 2:
                                raise RuntimeError(f"{manifest_path} contains fewer than two frames")
                            if status_key == "hybrid" and manifest.get("compositeKind") != "hybrid-prefix":
                                raise RuntimeError(f"{manifest_path} is not a hybrid-prefix manifest")
                            if manifest.get("renditionPolicy") == "high-only":
                                expected_id = "high" if domain_id == "bc" else "display"
                                rendition_ids = {
                                    value.get("id")
                                    for value in manifest.get("renditions", [])
                                    if isinstance(value, dict)
                                }
                                if rendition_ids != {expected_id}:
                                    raise RuntimeError(
                                        f"{manifest_path} does not contain exactly the {expected_id} rendition"
                                    )
                            end_source = parse_utc(str(pointer["endSourceTime"]))
                            lag_minutes = max(
                                0.0,
                                ((newest_source or end_source) - end_source).total_seconds() / 60,
                            )
                            lag_limit = freshness_minutes.get(hours, 40)
                            if lag_minutes > lag_limit:
                                warnings.append(
                                    f"{label} trails the newest satellite by {lag_minutes:.0f} minutes "
                                    f"(target {lag_limit})"
                                )
                            range_status[preset_id] = {
                                "generation": pointer.get("generation"),
                                "frames": len(frames),
                                "sourceLagMinutes": round(lag_minutes, 1),
                                **(
                                    {"proxies": len(manifest.get("proxies", {}))}
                                    if status_key == "hybrid"
                                    and isinstance(manifest.get("proxies"), dict)
                                    else {}
                                ),
                            }
                            if (
                                product_id == "bc-south-coast-overlay"
                                and hours == 3
                                and status_key == "exact"
                            ):
                                radar_updates = recent_layer_source_count(frames, "radar-rain")
                                satellite_updates = recent_layer_source_count(frames, layer_id)
                                range_status[preset_id]["recentSourceCounts"] = {
                                    "radar": radar_updates,
                                    "satellite": satellite_updates,
                                }
                                if radar_updates and radar_updates < 6:
                                    warnings.append(
                                        f"{label} contains only {radar_updates} distinct radar sources "
                                        "in its latest hour (target at least 6)"
                                    )
                                if satellite_updates and satellite_updates < 3:
                                    warnings.append(
                                        f"{label} contains only {satellite_updates} distinct satellite sources "
                                        "in its latest hour (target at least 3)"
                                    )
                        except (RuntimeError, KeyError, TypeError, ValueError) as error:
                            warnings.append(f"{label} is invalid: {error}")
                            range_status[preset_id] = "invalid"
                    status_bucket = product_status.get(status_key)
                    if isinstance(status_bucket, dict):
                        status_bucket[f"{hours}h"] = range_status

            # Seven-day playback remains on the existing reusable HLS profile
            # until its CMAF sidecar migration. Monitor it independently.
            if product_id in VIDEO_ARCHIVE_PRODUCTS:
                archive_profile = (
                    video_profiles.get(product_id, {}).get(layer_id, {})
                    if isinstance(video_profiles, dict)
                    else {}
                )
                pointer = archive_profile.get("archive") if isinstance(archive_profile, dict) else None
                if not isinstance(pointer, dict) or not isinstance(pointer.get("manifestPath"), str):
                    warnings.append(f"{product_id}/archive has no operational composite video")
                    product_status["archive"] = "missing"
                else:
                    try:
                        manifest_path = output_root / str(pointer["manifestPath"])
                        manifest = read_json(manifest_path)
                        frames = manifest.get("frames", [])
                        if not isinstance(frames, list) or len(frames) < 2:
                            raise RuntimeError(f"{manifest_path} contains fewer than two frames")
                        latest = parse_utc(str(frames[-1]["sourceValidTime"]))
                        age_minutes = max(0.0, (now - latest).total_seconds() / 60)
                        if age_minutes > 150:
                            warnings.append(
                                f"{product_id}/archive video source is {age_minutes:.0f} minutes old "
                                "(target 150)"
                            )
                        product_status["archive"] = {
                            "generation": pointer.get("generation"),
                            "frames": len(frames),
                            "sourceAgeMinutes": round(age_minutes, 1),
                        }
                    except (RuntimeError, KeyError, TypeError, ValueError) as error:
                        warnings.append(f"{product_id}/archive video is invalid: {error}")
                        product_status["archive"] = "invalid"
            video_coverage[product_id] = product_status
    except (RuntimeError, KeyError, ValueError) as error:
        errors.append(str(error))

    storage = storage_breakdown(output_root)
    local_bytes = storage["totalBytes"]
    warning_threshold = local_warn_bytes or int(
        os.environ.get("RADARSAT_LOCAL_WARN_BYTES", 20_000_000_000)
    )
    maximum_threshold = local_max_bytes or int(
        os.environ.get("RADARSAT_LOCAL_MAX_BYTES", 30_000_000_000)
    )
    if local_bytes >= maximum_threshold:
        errors.append(
            f"local working set is {local_bytes / 1_000_000_000:.2f} GB "
            f"(limit {maximum_threshold / 1_000_000_000:.2f} GB)"
        )
    elif local_bytes >= warning_threshold:
        warnings.append(
            f"local working set is {local_bytes / 1_000_000_000:.2f} GB "
            f"(warning {warning_threshold / 1_000_000_000:.2f} GB)"
        )

    try:
        disk = shutil.disk_usage(output_root)
        storage.update({
            "diskTotalBytes": disk.total,
            "diskUsedBytes": disk.used,
            "diskFreeBytes": disk.free,
        })
        free_warning = disk_warn_free_bytes or int(
            os.environ.get("RADARSAT_DISK_WARN_FREE_BYTES", 200_000_000_000)
        )
        free_minimum = disk_min_free_bytes or int(
            os.environ.get("RADARSAT_DISK_MIN_FREE_BYTES", 100_000_000_000)
        )
        if disk.free <= free_minimum:
            errors.append(
                f"forecast-data disk has {disk.free / 1_000_000_000:.1f} GB free "
                f"(minimum {free_minimum / 1_000_000_000:.0f} GB)"
            )
        elif disk.free <= free_warning:
            warnings.append(
                f"forecast-data disk has {disk.free / 1_000_000_000:.1f} GB free "
                f"(warning {free_warning / 1_000_000_000:.0f} GB)"
            )
    except OSError as error:
        errors.append(f"cannot inspect forecast-data disk usage: {error}")

    return {
        "status": "ok" if not errors else "error",
        "checkedAt": format_utc(now),
        "errors": errors,
        "warnings": warnings,
        "localBytes": local_bytes,
        "storage": storage,
        "frameCounts": frame_counts,
        "videoCoverage": video_coverage,
    }
