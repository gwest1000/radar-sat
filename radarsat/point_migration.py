from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable

from .config import DOMAINS, LAYERS, Domain, Layer
from .geomet import format_utc
from .pipeline import (
    GLM_LIGHTNING_POINT_RENDER_VERSION,
    HOTSPOT_POINT_RENDER_VERSION,
    LIGHTNING_POINT_RENDER_VERSION,
    frame_path,
    metadata_path,
    safe_archive_path,
    write_metadata,
)
from .point_frames import (
    point_frame_metadata,
    points_from_glm_png,
    points_from_hotspot_png,
    points_from_lightning_density_png,
    write_point_frame,
)


UTC = dt.timezone.utc


def _parse_time(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected an ISO timestamp, received {value!r}")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _source_times(payload: dict[str, object]) -> dict[str, dt.datetime]:
    raw = payload.get("sourceTimes")
    if not isinstance(raw, dict):
        return {}
    values: dict[str, dt.datetime] = {}
    for key, value in raw.items():
        try:
            values[str(key)] = _parse_time(value)
        except ValueError:
            continue
    return values


def _target_ready(path: Path, metadata: Path, render_version: int) -> bool:
    if not path.is_file() or not metadata.is_file():
        return False
    try:
        return json.loads(metadata.read_text()).get("renderVersion") == render_version
    except (OSError, json.JSONDecodeError):
        return False


def _legacy_metadata(root: Path, domain: Domain, layer: Layer) -> list[Path]:
    directory = root / "metadata" / domain.id / layer.id
    return sorted(directory.rglob("*.json")) if directory.exists() else []


def derive_hazard_point_archive(
    root: Path,
    domain_ids: Iterable[str],
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    """Derive point JSON from retained hazard PNGs without source downloads.

    Existing PNGs and their metadata are read-only inputs. A partial point
    target is never replaced unless ``overwrite`` is explicit.
    """
    root = root.resolve()
    results: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    specifications = (
        (
            LAYERS["glm-lightning"],
            LAYERS["glm-lightning-points"],
            GLM_LIGHTNING_POINT_RENDER_VERSION,
        ),
        (
            LAYERS["lightning"],
            LAYERS["lightning-points"],
            LIGHTNING_POINT_RENDER_VERSION,
        ),
        (
            LAYERS["hotspots"],
            LAYERS["hotspot-points"],
            HOTSPOT_POINT_RENDER_VERSION,
        ),
    )

    for domain_id in domain_ids:
        domain = DOMAINS.get(domain_id)
        if domain is None:
            warnings.append(f"Unknown domain skipped: {domain_id}")
            continue
        for source_layer, point_layer, render_version in specifications:
            key = f"{domain.id}/{point_layer.id}"
            counts = results.setdefault(
                key,
                {"discovered": 0, "derived": 0, "unchanged": 0, "skipped": 0},
            )
            for source_metadata_path in _legacy_metadata(root, domain, source_layer):
                counts["discovered"] += 1
                try:
                    source_metadata = json.loads(source_metadata_path.read_text())
                    source_valid_time = _parse_time(source_metadata["validTime"])
                    source_path = safe_archive_path(root, str(source_metadata["path"]))
                    if source_layer.id == "glm-lightning":
                        window_start = source_valid_time
                        window_end = _parse_time(
                            source_metadata.get(
                                "windowEnd",
                                format_utc(source_valid_time + dt.timedelta(minutes=10)),
                            )
                        )
                        # A ten-minute aggregate is not complete at its window
                        # start. Key the point frame to the end so clients never
                        # display flashes before they were observed.
                        valid_time = window_end
                    else:
                        valid_time = source_valid_time
                    destination = frame_path(root, domain, point_layer, valid_time)
                    destination_metadata = metadata_path(root, domain, point_layer, valid_time)
                    if _target_ready(destination, destination_metadata, render_version) and not overwrite:
                        counts["unchanged"] += 1
                        continue
                    if (
                        not overwrite
                        and (destination.exists() or destination_metadata.exists())
                    ):
                        counts["skipped"] += 1
                        warnings.append(
                            f"Partial point target preserved; rerun with --overwrite: {destination}"
                        )
                        continue

                    if source_layer.id == "glm-lightning":
                        age_reference = window_end
                        points = points_from_glm_png(source_path, domain)
                        age_mode = "window-midpoint-estimate"
                        age_precision_seconds = 600
                        migration_method = "nontransparent GLM marker pixels"
                        point_details: dict[str, object] = {}
                    elif source_layer.id == "lightning":
                        age_reference = source_valid_time
                        window_end = source_valid_time
                        window_start = window_end - dt.timedelta(minutes=10)
                        points = points_from_lightning_density_png(source_path, domain)
                        age_mode = "window-midpoint-estimate"
                        age_precision_seconds = 600
                        migration_method = "connected positive ECCC density cells"
                        point_details = {
                            "countMeaning": (
                                "connected positive 2.5-km density cells; not strokes"
                            ),
                            "densityWindowMinutes": 10,
                        }
                    else:
                        age_reference = _parse_time(
                            source_metadata.get("fetchedAt", source_metadata["validTime"])
                        )
                        window_end = age_reference
                        window_start = window_end - dt.timedelta(hours=24)
                        points = points_from_hotspot_png(source_path, domain)
                        age_mode = "render-colour-bucket-midpoint-estimate"
                        age_precision_seconds = 43_200
                        migration_method = "hotspot fill-colour connected components"
                        point_details = {}

                    if not dry_run:
                        write_point_frame(
                            destination,
                            layer=point_layer.id,
                            domain=domain,
                            valid_time=valid_time,
                            window_start=window_start,
                            window_end=window_end,
                            age_reference_time=age_reference,
                            point_schema=point_layer.point_schema,
                            points=points,
                            age_mode=age_mode,
                            age_precision_seconds=age_precision_seconds,
                        )
                        write_metadata(
                            root,
                            domain,
                            point_layer,
                            valid_time,
                            destination,
                            _source_times(source_metadata),
                            source=str(source_metadata.get("source") or point_layer.source),
                            source_layer=str(
                                source_metadata.get("sourceLayer")
                                or source_layer.source_layer
                                or "derived"
                            ),
                            extra={
                                **point_frame_metadata(
                                    points=points,
                                    point_schema=point_layer.point_schema,
                                    window_start=window_start,
                                    window_end=window_end,
                                    age_reference_time=age_reference,
                                    age_mode=age_mode,
                                    age_precision_seconds=age_precision_seconds,
                                    render_version=render_version,
                                    migration_source_path=source_path.relative_to(root).as_posix(),
                                ),
                                "migrationMethod": migration_method,
                                "sourceTimingRecovered": source_layer.id == "lightning",
                                "eventTimingRecovered": False,
                                **point_details,
                            },
                        )
                    counts["derived"] += 1

                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    counts["skipped"] += 1
                    warnings.append(
                        f"{source_metadata_path.relative_to(root)} skipped: "
                        f"{type(error).__name__}: {error}"
                    )

    return {
        "status": "warning" if warnings else "ok",
        "dryRun": dry_run,
        "overwrite": overwrite,
        "layers": results,
        "warnings": warnings,
    }
