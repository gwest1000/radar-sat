from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .geomet import format_utc, parse_utc


UTC = dt.timezone.utc
REQUIRED_LAYERS = ("daynight", "radar-rain", "ptype", "lightning")


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
    if payload.get("status") != "ok":
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
        except RuntimeError as error:
            errors.append(str(error))

    frame_counts: dict[str, int] = {}
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
    except (RuntimeError, KeyError, ValueError) as error:
        errors.append(str(error))

    local_bytes = directory_size(output_root)
    warning_threshold = local_warn_bytes or int(
        os.environ.get("RADARSAT_LOCAL_WARN_BYTES", 8_000_000_000)
    )
    maximum_threshold = local_max_bytes or int(
        os.environ.get("RADARSAT_LOCAL_MAX_BYTES", 10_000_000_000)
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
    }
