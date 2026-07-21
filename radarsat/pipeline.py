from __future__ import annotations

import argparse
import datetime as dt
import io
import json
from pathlib import Path
from typing import Iterable

from PIL import Image

from .catalog import write_catalog
from .config import DOMAINS, LAYERS, Domain, Layer
from .geomet import GeoMetClient, at_or_before, format_utc, frame_stamp
from .images import lightning_trail, render_static_maps, save_coverage, save_overlay, save_satellite
from .retention import keep_frame


UTC = dt.timezone.utc
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
    return [value for value in values if keep_frame(value, now, tier)]


def ensure_static_assets(client: GeoMetClient, root: Path, domain: Domain) -> None:
    base = root / "static" / domain.id / "base-dark.png"
    boundaries = root / "static" / domain.id / "boundaries.png"
    if not base.exists() or not boundaries.exists():
        render_static_maps(domain, base, boundaries)

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
) -> dict[str, list[dt.datetime]]:
    timelines: dict[str, list[dt.datetime]] = {}
    for layer_id in DEFAULT_SOURCE_LAYERS:
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
                continue
            try:
                content = client.get_map(layer, domain, valid_time)
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
            write_metadata(root, domain, layer, valid_time, destination)
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
        if meta.exists():
            try:
                current_sources = json.loads(meta.read_text()).get("sourceTimes", {})
            except (OSError, json.JSONDecodeError):
                current_sources = {}
        if not destination.exists() or current_sources != expected_sources:
            lightning_trail(existing, destination)
            write_metadata(
                root,
                domain,
                output_layer,
                anchor,
                destination,
                {f"age{index * 10}": value for index, value in enumerate(source_times) if value},
            )


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
            )
            trail_hours = spool_hours if domain.id == "bc" and spool_mode != "off" else hours
            derive_lightning_trails(output_root, domain, timelines, max(hours, trail_hours))
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
            "status": "warning" if has_native_rejections else "ok",
            "updatedAt": format_utc(dt.datetime.now(UTC)),
            "catalog": catalog.relative_to(output_root).as_posix(),
            "domains": domain_ids,
            "spool": {
                "mode": spool_mode,
                "root": str(spool_root),
                "ingestHours": spool_hours,
                "domains": native_status,
            },
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
