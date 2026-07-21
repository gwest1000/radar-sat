from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
from typing import Iterable

from PIL import Image

from .catalog import write_catalog
from .config import DOMAINS, LAYERS, Domain, Layer
from .geomet import GeoMetClient, at_or_before, format_utc, frame_stamp
from .hotspots import CWFIS_HOTSPOT_LAYER, fetch_hotspots, render_hotspots
from .images import (
    lightning_trail,
    reproject_overlay,
    render_static_maps,
    render_watershed_overlay,
    save_coverage,
    save_overlay,
    save_satellite,
)
from .retention import keep_frame


UTC = dt.timezone.utc
LIGHTNING_TRAIL_RENDER_VERSION = 2
HOTSPOT_RENDER_VERSION = 3
RAW_SATELLITE_RENDER_VERSION = 1
COVERAGE_RENDER_VERSION = 2
DEFAULT_SOURCE_LAYERS = (
    "daynight",
    "ir",
    "natural",
    "convective",
    "snowfog",
    "radar-rain",
    "radar-snow",
    "radar-coverage",
    "ptype",
    "ptype-coverage",
    "lightning",
)

def safe_archive_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"Archive metadata path escapes output root: {relative!r}")
    return candidate


def frame_path(root: Path, domain: Domain, layer: Layer, valid_time: dt.datetime) -> Path:
    date = valid_time.astimezone(UTC)
    return (
        root
        / "frames"
        / domain.id
        / layer.id
        / date.strftime("%Y")
        / date.strftime("%m")
        / date.strftime("%d")
        / f"{frame_stamp(date)}.{layer.extension}"
    )


def metadata_path(root: Path, domain: Domain, layer: Layer, valid_time: dt.datetime) -> Path:
    date = valid_time.astimezone(UTC)
    return (
        root
        / "metadata"
        / domain.id
        / layer.id
        / date.strftime("%Y")
        / date.strftime("%m")
        / date.strftime("%d")
        / f"{frame_stamp(date)}.json"
    )


def write_metadata(
    root: Path,
    domain: Domain,
    layer: Layer,
    valid_time: dt.datetime,
    image_path: Path,
    source_times: dict[str, dt.datetime] | None = None,
    *,
    source: str | None = None,
    source_layer: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    destination = metadata_path(root, domain, layer, valid_time)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "validTime": format_utc(valid_time),
        "path": image_path.relative_to(root).as_posix(),
        "source": source or layer.source,
        "sourceLayer": source_layer or layer.source_layer or "derived",
        "fetchedAt": format_utc(dt.datetime.now(UTC)),
    }
    if source_times:
        payload["sourceTimes"] = {key: format_utc(value) for key, value in source_times.items()}
    if extra:
        protected = {
            "validTime",
            "path",
            "source",
            "sourceLayer",
            "fetchedAt",
            "sourceTimes",
        }.intersection(extra)
        if protected:
            raise ValueError(f"Extra metadata cannot replace standard fields: {sorted(protected)}")
        payload.update(extra)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(destination)


def selected_times(times: Iterable[dt.datetime], hours: float, latest_only: bool) -> list[dt.datetime]:
    values = sorted(set(times))
    if not values:
        return []
    if latest_only:
        return [values[-1]]
    cutoff = values[-1] - dt.timedelta(hours=hours)
    return [value for value in values if value >= cutoff]


def retained_times(
    times: Iterable[dt.datetime],
    hours: float,
    latest_only: bool,
    now: dt.datetime,
    tier: str,
) -> list[dt.datetime]:
    """Select only source times that can survive the archive policy.

    A long bootstrap should not download every high-frequency source image and
    then immediately remove most older frames during ``prune``. Apply the same
    retention rule before each WMS request while preserving the latest-only
    probe used by diagnostics.
    """
    values = selected_times(times, hours, latest_only)
    if latest_only:
        return values
    if tier == "broad":
        # Broad displays are deliberately half-hourly for the first day and
        # hourly thereafter. Avoid downloading six-minute radar frames that
        # the requested product cadence will never use.
        values = [value for value in values if value.minute in {0, 30}]
    return [value for value in values if keep_frame(value, now, tier)]


def ingest_hotspot_snapshot(
    root: Path,
    domain: Domain,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Archive a ten-minute snapshot of CWFIS's rolling 24-hour BC hotspots."""
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    valid_time = current.replace(
        minute=(current.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    layer = LAYERS["hotspots"]
    destination = frame_path(root, domain, layer, valid_time)
    metadata = metadata_path(root, domain, layer, valid_time)
    if destination.exists() and metadata.exists():
        try:
            existing = json.loads(metadata.read_text())
            if existing.get("renderVersion") == HOTSPOT_RENDER_VERSION:
                return {
                    "status": "unchanged",
                    "validTime": format_utc(valid_time),
                    "detectionCount": existing.get("detectionCount", 0),
                }
        except (OSError, json.JSONDecodeError):
            pass

    features = fetch_hotspots(domain)
    summary = render_hotspots(features, domain, destination, current)
    write_metadata(
        root,
        domain,
        layer,
        valid_time,
        destination,
        {"hotspots": valid_time},
        source="NRCan CWFIS",
        source_layer=CWFIS_HOTSPOT_LAYER,
        extra={**summary, "renderVersion": HOTSPOT_RENDER_VERSION},
    )
    return {"status": "rendered", "validTime": format_utc(valid_time), **summary}


def ensure_static_assets(client: GeoMetClient, root: Path, domain: Domain) -> None:
    base = root / "static" / domain.id / "base-dark.png"
    boundaries = root / "static" / domain.id / "boundaries.png"
    if not base.exists() or not boundaries.exists():
        render_static_maps(domain, base, boundaries)
    watersheds = root / "static" / domain.id / "bch-watersheds.png"
    if domain.id == "bc" and not watersheds.exists():
        render_watershed_overlay(domain, watersheds)

    legend_specs = {
        "legend-radar-rain.png": LAYERS["radar-rain"],
        "legend-radar-snow.png": LAYERS["radar-snow"],
        "legend-ptype.png": LAYERS["ptype"],
        "legend-lightning-density.png": LAYERS["lightning"],
    }
    for filename, layer in legend_specs.items():
        destination = root / "static" / filename
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = Image.open(io.BytesIO(client.get_legend(layer))).convert("RGBA")
        image.save(destination, "PNG", optimize=True)


def ingest_geomet(
    client: GeoMetClient,
    root: Path,
    domain: Domain,
    hours: float,
    latest_only: bool,
    exclude_layers: set[str] | frozenset[str] | None = None,
    include_layers: Iterable[str] | None = None,
) -> dict[str, list[dt.datetime]]:
    timelines: dict[str, list[dt.datetime]] = {}
    for layer_id in include_layers or DEFAULT_SOURCE_LAYERS:
        if exclude_layers and layer_id in exclude_layers:
            continue
        layer = LAYERS[layer_id]
        if layer.source_layer is None:
            continue
        timeline = client.timeline(layer.source_layer)
        # Radar, ptype, and lightning are usually newer than the slower
        # satellite anchor.  Fetch an extra hour of those layers so the oldest
        # satellite frame in a requested loop still has an honest at-or-before
        # match instead of starting with avoidable partial frames.
        matching_margin = 1.0 if layer_id in {
            "radar-rain",
            "radar-snow",
            "radar-coverage",
            "ptype",
            "ptype-coverage",
            "lightning",
        } else 0.0
        times = retained_times(
            timeline.times,
            hours + matching_margin,
            latest_only,
            dt.datetime.now(UTC),
            domain.tier,
        )
        timelines[layer_id] = list(timeline.times)
        for valid_time in times:
            destination = frame_path(root, domain, layer, valid_time)
            meta = metadata_path(root, domain, layer, valid_time)
            if destination.exists() and meta.exists():
                if not layer_id.endswith("coverage"):
                    continue
                try:
                    if json.loads(meta.read_text()).get("renderVersion") == COVERAGE_RENDER_VERSION:
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            try:
                request_domain = domain
                if domain.id == "north-pacific":
                    request_domain = Domain(
                        id="north-pacific-radar-source",
                        title="North Pacific radar source",
                        west=-165.0,
                        south=5.0,
                        east=-50.0,
                        north=75.0,
                        crs="EPSG:3857",
                        width=1000,
                        height=900,
                        tier="broad",
                    )
                content = client.get_map(layer, request_domain, valid_time)
                if request_domain is not domain:
                    content = reproject_overlay(
                        content,
                        request_domain,
                        domain,
                        outside_no_coverage=layer_id.endswith("coverage"),
                    )
            except Exception:
                if layer.daylight_only:
                    continue
                raise
            if layer.role == "background":
                save_satellite(content, destination)
            elif layer_id.endswith("coverage"):
                save_coverage(content, destination)
            else:
                save_overlay(content, destination)
            write_metadata(
                root,
                domain,
                layer,
                valid_time,
                destination,
                extra={"renderVersion": COVERAGE_RENDER_VERSION} if layer_id.endswith("coverage") else None,
            )
    return timelines


def derive_lightning_trails(root: Path, domain: Domain, timelines: dict[str, list[dt.datetime]], hours: float) -> None:
    def local_times(layer_id: str) -> list[dt.datetime]:
        directory = root / "metadata" / domain.id / layer_id
        values: list[dt.datetime] = []
        if not directory.exists():
            return values
        for path in directory.rglob("*.json"):
            try:
                payload = json.loads(path.read_text())
                values.append(dt.datetime.fromisoformat(payload["validTime"].replace("Z", "+00:00")))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return sorted(set(values))

    lightning_times = local_times("lightning")
    radar_times = local_times("radar-rain")
    if not lightning_times:
        return
    cutoff = max(lightning_times) - dt.timedelta(hours=hours)
    all_anchors = set(radar_times or lightning_times)
    if radar_times:
        # Normal six-minute composite scans remain the display clock. During a
        # workstation outage, however, GeoMet cannot backfill the full native
        # queue window. Add a ten-minute lightning anchor only where there is a
        # real gap in the local radar archive, so recovered lightning is not
        # silently stranded as an unused source frame.
        for lightning_time in lightning_times:
            has_nearby_radar = any(
                abs((radar_time - lightning_time).total_seconds()) <= 6 * 60
                for radar_time in radar_times
            )
            if not has_nearby_radar:
                all_anchors.add(lightning_time)
    anchors = sorted(value for value in all_anchors if value >= cutoff)
    output_layer = LAYERS["lightning-trail"]
    source_layer = LAYERS["lightning"]

    # A previous capability-timeline implementation could manufacture derived
    # frames for anchors whose source radar frame was never downloaded.  Keep
    # only anchors represented in the local archive.  Older, legitimately
    # retained anchors remain valid because ``all_anchors`` is not hour-limited.
    valid_anchor_stamps = {frame_stamp(value) for value in all_anchors}
    output_metadata_root = root / "metadata" / domain.id / output_layer.id
    if output_metadata_root.exists():
        for path in output_metadata_root.rglob("*.json"):
            if path.stem in valid_anchor_stamps:
                continue
            try:
                payload = json.loads(path.read_text())
                image_path = safe_archive_path(root, str(payload.get("path", "")))
                if image_path.is_file():
                    image_path.unlink()
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            path.unlink(missing_ok=True)
    output_frame_root = root / "frames" / domain.id / output_layer.id
    if output_frame_root.exists():
        for path in output_frame_root.rglob(f"*.{output_layer.extension}"):
            if path.stem not in valid_anchor_stamps:
                path.unlink(missing_ok=True)

    for anchor in anchors:
        source_times: list[dt.datetime | None] = []
        used: set[dt.datetime] = set()
        for offset in (0, 10, 20):
            target = anchor - dt.timedelta(minutes=offset)
            selected = at_or_before(lightning_times, target)
            if selected is not None and target - selected > dt.timedelta(minutes=10):
                selected = None
            if selected in used:
                selected = None
            if selected is not None:
                used.add(selected)
            source_times.append(selected)
        paths = [frame_path(root, domain, source_layer, value) if value else None for value in source_times]
        existing = [path if path and path.exists() else None for path in paths]
        destination = frame_path(root, domain, output_layer, anchor)
        meta = metadata_path(root, domain, output_layer, anchor)
        if not any(existing):
            destination.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            continue
        expected_sources = {
            f"age{index * 10}": format_utc(value)
            for index, value in enumerate(source_times)
            if value
        }
        current_sources: dict[str, str] = {}
        current_render_version: int | None = None
        if meta.exists():
            try:
                current_metadata = json.loads(meta.read_text())
                current_sources = current_metadata.get("sourceTimes", {})
                current_render_version = current_metadata.get("renderVersion")
            except (OSError, json.JSONDecodeError):
                current_sources = {}
        if (
            not destination.exists()
            or current_sources != expected_sources
            or current_render_version != LIGHTNING_TRAIL_RENDER_VERSION
        ):
            lightning_trail(existing, destination)
            write_metadata(
                root,
                domain,
                output_layer,
                anchor,
                destination,
                {f"age{index * 10}": value for index, value in enumerate(source_times) if value},
                extra={"renderVersion": LIGHTNING_TRAIL_RENDER_VERSION},
            )


def _raw_pair_ready(root: Path, domain: Domain, valid_time: dt.datetime) -> bool:
    for layer_id in ("raw-visible", "raw-ir"):
        layer = LAYERS[layer_id]
        image = frame_path(root, domain, layer, valid_time)
        metadata = metadata_path(root, domain, layer, valid_time)
        if not image.exists() or not metadata.exists():
            return False
        try:
            payload = json.loads(metadata.read_text())
            if payload.get("renderVersion") != RAW_SATELLITE_RENDER_VERSION:
                return False
            if domain.id == "north-pacific" and "Himawari-9" not in str(payload.get("source", "")):
                return False
        except (OSError, json.JSONDecodeError):
            return False
    return True


def _write_raw_pair_metadata(
    root: Path,
    domain: Domain,
    valid_time: dt.datetime,
    source: str,
    source_layer: str,
    source_times: dict[str, dt.datetime],
) -> None:
    for layer_id in ("raw-visible", "raw-ir"):
        layer = LAYERS[layer_id]
        write_metadata(
            root,
            domain,
            layer,
            valid_time,
            frame_path(root, domain, layer, valid_time),
            source_times,
            source=source,
            source_layer=source_layer,
            extra={"renderVersion": RAW_SATELLITE_RENDER_VERSION},
        )


def ingest_raw_satellite(
    root: Path,
    domain_ids: Iterable[str],
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Ingest one bounded half-hourly calibrated satellite frame.

    Large source files are downloaded one satellite at a time and removed in a
    ``finally`` block. Only compact WebP display rasters and Satpy's small
    auxiliary tables persist locally.
    """
    from .raw_satellite import (
        PublicSatelliteClient,
        blend_satellites,
        clear_downloads,
        install_render,
        normalized_frame_time,
        render_satpy_domain_isolated,
    )

    selected = [DOMAINS[domain_id] for domain_id in domain_ids if domain_id in DOMAINS]
    selected = [domain for domain in selected if domain.id in {"bc", "north-america", "north-pacific"}]
    if not selected:
        return {"status": "disabled", "domains": []}
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    project_root = Path(__file__).resolve().parents[1]
    configured_cache = Path(os.environ.get("RADARSAT_RAW_SAT_CACHE_ROOT", "var/cache/raw-satellite")).expanduser()
    cache_root = configured_cache if configured_cache.is_absolute() else project_root / configured_cache
    max_bytes = int(os.environ.get("RADARSAT_RAW_SAT_MAX_BYTES", "900000000"))
    warnings: list[str] = []
    downloaded_bytes = 0
    rendered_domains: list[str] = []
    render_root = cache_root / "renders"
    clear_downloads(cache_root)
    shutil.rmtree(render_root, ignore_errors=True)

    with PublicSatelliteClient() as client:
        goes18 = client.latest_goes("G18", current)
        valid_time = normalized_frame_time(goes18.valid_time)
        needed = [domain for domain in selected if not _raw_pair_ready(root, domain, valid_time)]
        if not needed:
            return {
                "status": "unchanged",
                "validTime": format_utc(valid_time),
                "sourceTimes": {"GOES-18": format_utc(goes18.valid_time)},
                "downloadBytes": 0,
                "cacheCapBytes": max_bytes,
                "domains": [domain.id for domain in selected],
                "warnings": [],
            }
        try:
            goes18_path = client.download(goes18, cache_root, max_bytes)
            downloaded_bytes += goes18.size
            rendered18 = {
                domain.id: render_satpy_domain_isolated(
                    [goes18_path], "abi_l2_nc", "C13", domain, cache_root, f"g18-{frame_stamp(valid_time)}"
                )
                for domain in needed
            }
            clear_downloads(cache_root)

            bc = next((domain for domain in needed if domain.id == "bc"), None)
            if bc is not None:
                install_render(
                    rendered18[bc.id],
                    frame_path(root, bc, LAYERS["raw-visible"], valid_time),
                    frame_path(root, bc, LAYERS["raw-ir"], valid_time),
                )
                _write_raw_pair_metadata(
                    root, bc, valid_time, "NOAA GOES-18", "ABI-L2-MCMIPF", {"GOES-18": goes18.valid_time}
                )
                rendered_domains.append(bc.id)

            north_america = next((domain for domain in needed if domain.id == "north-america"), None)
            if north_america is not None:
                source = "NOAA GOES-18"
                source_times = {"GOES-18": goes18.valid_time}
                try:
                    goes19 = client.latest_goes("G19", current)
                    goes19_path = client.download(goes19, cache_root, max_bytes)
                    downloaded_bytes += goes19.size
                    rendered19 = render_satpy_domain_isolated(
                        [goes19_path], "abi_l2_nc", "C13", north_america, cache_root, f"g19-{frame_stamp(valid_time)}"
                    )
                    blend_satellites(
                        rendered18[north_america.id],
                        rendered19,
                        north_america,
                        (-112.0, -96.0),
                        frame_path(root, north_america, LAYERS["raw-visible"], valid_time),
                        frame_path(root, north_america, LAYERS["raw-ir"], valid_time),
                    )
                    source = "NOAA GOES-18 + GOES-19"
                    source_times["GOES-19"] = goes19.valid_time
                except Exception as error:
                    warnings.append(f"GOES-19 blend unavailable; using GOES-18: {type(error).__name__}: {error}")
                    install_render(
                        rendered18[north_america.id],
                        frame_path(root, north_america, LAYERS["raw-visible"], valid_time),
                        frame_path(root, north_america, LAYERS["raw-ir"], valid_time),
                    )
                finally:
                    clear_downloads(cache_root)
                _write_raw_pair_metadata(
                    root, north_america, valid_time, source, "ABI-L2-MCMIPF", source_times
                )
                rendered_domains.append(north_america.id)

            north_pacific = next((domain for domain in needed if domain.id == "north-pacific"), None)
            if north_pacific is not None:
                source = "NOAA GOES-18"
                source_layer = "ABI-L2-MCMIPF"
                source_times = {"GOES-18": goes18.valid_time}
                try:
                    himawari = client.latest_himawari(current)
                    himawari_paths = [client.download(item, cache_root, max_bytes) for item in himawari]
                    downloaded_bytes += sum(item.size for item in himawari)
                    rendered_himawari = render_satpy_domain_isolated(
                        himawari_paths,
                        "ahi_hsd",
                        "B13",
                        north_pacific,
                        cache_root,
                        f"h09-{frame_stamp(valid_time)}",
                    )
                    blend_satellites(
                        rendered_himawari,
                        rendered18[north_pacific.id],
                        north_pacific,
                        (185.0, 205.0),
                        frame_path(root, north_pacific, LAYERS["raw-visible"], valid_time),
                        frame_path(root, north_pacific, LAYERS["raw-ir"], valid_time),
                        unwrap_longitudes=True,
                    )
                    source = "NOAA Himawari-9 + GOES-18"
                    source_layer = "AHI-L1b-FLDK + ABI-L2-MCMIPF"
                    source_times["Himawari-9"] = himawari[0].valid_time
                except Exception as error:
                    warnings.append(f"Himawari-9 blend unavailable; using GOES-18: {type(error).__name__}: {error}")
                    install_render(
                        rendered18[north_pacific.id],
                        frame_path(root, north_pacific, LAYERS["raw-visible"], valid_time),
                        frame_path(root, north_pacific, LAYERS["raw-ir"], valid_time),
                    )
                finally:
                    clear_downloads(cache_root)
                _write_raw_pair_metadata(
                    root, north_pacific, valid_time, source, source_layer, source_times
                )
                rendered_domains.append(north_pacific.id)
        finally:
            clear_downloads(cache_root)
            shutil.rmtree(render_root, ignore_errors=True)

    return {
        "status": "warning" if warnings else "rendered",
        "validTime": format_utc(valid_time),
        "sourceTimes": {"GOES-18": format_utc(goes18.valid_time)},
        "downloadBytes": downloaded_bytes,
        "cacheCapBytes": max_bytes,
        "domains": rendered_domains,
        "warnings": warnings,
    }


def prune(root: Path, now: dt.datetime) -> int:
    removed = 0
    for domain in DOMAINS.values():
        metadata_root = root / "metadata" / domain.id
        if not metadata_root.exists():
            continue
        for meta_path in metadata_root.rglob("*.json"):
            try:
                payload = json.loads(meta_path.read_text())
                valid_time = dt.datetime.fromisoformat(payload["validTime"].replace("Z", "+00:00"))
                image_path = safe_archive_path(root, str(payload["path"]))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
            layer_id = meta_path.relative_to(metadata_root).parts[0]
            if layer_id.endswith("coverage") and payload.get("renderVersion") != COVERAGE_RENDER_VERSION:
                image_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                removed += 1
                continue
            if keep_frame(valid_time, now, domain.tier):
                continue
            image_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            removed += 1
    return removed


def run(
    output_root: Path,
    domain_ids: list[str],
    hours: float,
    latest_only: bool,
    spool_root: Path | None = None,
    spool_mode: str = "auto",
    spool_hours: float = 12.0,
) -> Path:
    if spool_mode not in {"auto", "off", "only"}:
        raise ValueError(f"Unsupported spool mode: {spool_mode!r}")
    if spool_hours <= 0:
        raise ValueError("spool_hours must be positive")
    from .spool import NATIVE_LAYER_IDS, SpoolIngestResult, ingest_spool

    spool_root = (spool_root or Path.home() / ".local/share/radar-sat/spool/eccc").expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    native_status: dict[str, object] = {}
    auxiliary_warnings: list[str] = []
    hotspot_status: dict[str, object] = {}
    raw_satellite_status: dict[str, object] = {}
    with GeoMetClient() as client:
        for domain_id in domain_ids:
            domain = DOMAINS[domain_id]
            ensure_static_assets(client, output_root, domain)
            native_result = SpoolIngestResult()
            # Native products are currently consumed only on the operational BC
            # grid. Broad domains retain the lower-rate GeoMet bootstrap path.
            if domain.id == "bc" and spool_mode != "off":
                native_result = ingest_spool(
                    spool_root,
                    output_root,
                    domain,
                    spool_hours,
                    latest_only,
                )
                native_status[domain.id] = native_result.status()
            excluded = (
                set(NATIVE_LAYER_IDS)
                if domain.id == "bc" and spool_mode == "only"
                else set()
            )
            timelines = ingest_geomet(
                client,
                output_root,
                domain,
                hours,
                latest_only,
                exclude_layers=excluded,
                include_layers=None if domain.id == "bc" else ("radar-rain", "radar-coverage"),
            )
            if domain.id == "bc":
                trail_hours = spool_hours if spool_mode != "off" else hours
                derive_lightning_trails(output_root, domain, timelines, max(hours, trail_hours))
                try:
                    hotspot_status[domain.id] = ingest_hotspot_snapshot(output_root, domain)
                except Exception as error:
                    auxiliary_warnings.append(
                        f"CWFIS wildfire hotspots unavailable: {type(error).__name__}: {error}"
                    )
        if os.environ.get("RADARSAT_RAW_SAT_ENABLED", "1").lower() not in {"0", "false", "no"}:
            try:
                raw_satellite_status = ingest_raw_satellite(output_root, domain_ids)
                auxiliary_warnings.extend(str(value) for value in raw_satellite_status.get("warnings", []))
            except Exception as error:
                auxiliary_warnings.append(
                    f"Raw NOAA satellite ingest unavailable: {type(error).__name__}: {error}"
                )
                raw_satellite_status = {
                    "status": "warning",
                    "error": f"{type(error).__name__}: {error}",
                }
    prune(output_root, dt.datetime.now(UTC))
    catalog = write_catalog(output_root)
    has_native_rejections = any(
        bool(value.get("rejected"))
        for value in native_status.values()
        if isinstance(value, dict)
    )
    write_status(
        output_root / "status" / "ingest.json",
        {
            "status": "warning" if has_native_rejections or auxiliary_warnings else "ok",
            "updatedAt": format_utc(dt.datetime.now(UTC)),
            "catalog": catalog.relative_to(output_root).as_posix(),
            "domains": domain_ids,
            "spool": {
                "mode": spool_mode,
                "root": str(spool_root),
                "ingestHours": spool_hours,
                "domains": native_status,
            },
            "hotspots": hotspot_status,
            "rawSatellite": raw_satellite_status,
            "warnings": auxiliary_warnings,
        },
    )
    return catalog


def write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest and render Radar-Sat observational layers.")
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument("--domain", action="append", choices=sorted(DOMAINS), default=[])
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument(
        "--spool-root",
        type=Path,
        default=Path.home() / ".local/share/radar-sat/spool/eccc",
        help="root containing completed satellite/, lightning/, and radar/ feed files",
    )
    parser.add_argument(
        "--spool-mode",
        choices=("auto", "off", "only"),
        default="auto",
        help=(
            "auto prefers native files and fills gaps from GeoMet; off ignores the spool; "
            "only disables GeoMet fallback for native-capable satellite/lightning layers"
        ),
    )
    parser.add_argument(
        "--spool-hours",
        type=float,
        default=12.0,
        help="native backlog window to render (independent of the shorter GeoMet window)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    domain_ids = args.domain or ["bc"]
    try:
        catalog = run(
            args.output_root,
            domain_ids,
            args.hours,
            args.latest_only,
            args.spool_root,
            args.spool_mode,
            args.spool_hours,
        )
    except Exception as error:
        write_status(
            args.output_root / "status" / "ingest.json",
            {
                "status": "error",
                "updatedAt": format_utc(dt.datetime.now(UTC)),
                "error": f"{type(error).__name__}: {error}",
                "domains": domain_ids,
                "spool": {
                    "mode": args.spool_mode,
                    "root": str(args.spool_root),
                    "ingestHours": args.spool_hours,
                },
            },
        )
        raise
    else:
        print(f"Radar-Sat catalog written to {catalog}", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
