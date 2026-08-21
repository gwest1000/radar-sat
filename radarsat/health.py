from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .catalog import PUBLIC_VIDEO_LAYERS
from .config import VIDEO_ARCHIVE_PRODUCTS, VIDEO_COMPOSITE_PRESETS, VIDEO_EXACT_RANGES
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


def inspect_health(
    output_root: Path,
    publish_status_path: Path,
    *,
    now: dt.datetime | None = None,
    require_publish: bool = True,
    service_max_age_minutes: int = 15,
    local_warn_bytes: int | None = None,
    local_max_bytes: int | None = None,
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
        for product_id, layer_id in PUBLIC_VIDEO_LAYERS.items():
            expected_tracks = {"live"}
            if 24 in VIDEO_EXACT_RANGES.get(product_id, ()):
                expected_tracks.add("day")
            if product_id in VIDEO_ARCHIVE_PRODUCTS:
                expected_tracks.add("archive")
            profile = (
                video_profiles.get(product_id, {})
                .get(layer_id, {})
                if isinstance(video_profiles, dict)
                else {}
            )
            product_status: dict[str, object] = {}
            for track in sorted(expected_tracks):
                pointer = profile.get(track) if isinstance(profile, dict) else None
                if not isinstance(pointer, dict) or not isinstance(pointer.get("manifestPath"), str):
                    warnings.append(f"{product_id}/{track} has no operational composite video")
                    product_status[track] = "missing"
                    continue
                manifest_path = output_root / str(pointer["manifestPath"])
                try:
                    manifest = read_json(manifest_path)
                    frames = manifest.get("frames", [])
                    composites = manifest.get("composites", [])
                    expected_presets = {
                        str(value.get("id"))
                        for value in VIDEO_COMPOSITE_PRESETS.get(product_id, ())
                        if isinstance(value, dict)
                    }
                    available_presets = {
                        str(value.get("id"))
                        for value in composites
                        if isinstance(value, dict)
                    } if isinstance(composites, list) else set()
                    if not isinstance(frames, list) or len(frames) < 2:
                        raise RuntimeError(f"{manifest_path} contains fewer than two frames")
                    if not expected_presets.issubset(available_presets):
                        warnings.append(f"{product_id}/{track} is missing a common composite preset")
                    latest = parse_utc(str(frames[-1]["sourceValidTime"]))
                    age_minutes = max(0.0, (now - latest).total_seconds() / 60)
                    age_limit = 45 if track == "live" else 90 if track == "day" else 150
                    if age_minutes > age_limit:
                        warnings.append(
                            f"{product_id}/{track} video source is {age_minutes:.0f} minutes old "
                            f"(target {age_limit})"
                        )
                    product_status[track] = {
                        "generation": pointer.get("generation"),
                        "frames": len(frames),
                        "sourceAgeMinutes": round(age_minutes, 1),
                        "presets": sorted(available_presets),
                    }
                except (RuntimeError, KeyError, TypeError, ValueError) as error:
                    warnings.append(f"{product_id}/{track} video is invalid: {error}")
                    product_status[track] = "invalid"
            video_coverage[product_id] = product_status
    except (RuntimeError, KeyError, ValueError) as error:
        errors.append(str(error))

    local_bytes = directory_size(output_root)
    warning_threshold = local_warn_bytes or int(
        os.environ.get("RADARSAT_LOCAL_WARN_BYTES", 25_000_000_000)
    )
    maximum_threshold = local_max_bytes or int(
        os.environ.get("RADARSAT_LOCAL_MAX_BYTES", 40_000_000_000)
    )
    if local_bytes >= maximum_threshold:
        errors.append(
            f"local archive is {local_bytes / 1_000_000_000:.2f} GB "
            f"(limit {maximum_threshold / 1_000_000_000:.2f} GB)"
        )
    elif local_bytes >= warning_threshold:
        warnings.append(
            f"local archive is {local_bytes / 1_000_000_000:.2f} GB "
            f"(warning {warning_threshold / 1_000_000_000:.2f} GB)"
        )

    return {
        "status": "ok" if not errors else "error",
        "checkedAt": format_utc(now),
        "errors": errors,
        "warnings": warnings,
        "localBytes": local_bytes,
        "frameCounts": frame_counts,
        "videoCoverage": video_coverage,
    }
