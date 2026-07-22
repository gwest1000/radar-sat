#!/usr/bin/env python3
"""Validate Radar-Sat's Sarracenia configs and, optionally, its live spool."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


UTC = timezone.utc
CONFIG_NAMES = (
    "radarsat_goes_west.conf",
    "radarsat_lightning.conf",
    "radarsat_bc_site_radar.conf",
)
EXPECTED_TOPICS = {
    "radarsat_goes_west.conf": {"*.WXO-DD.satellite.goes.west.#"},
    "radarsat_lightning.conf": {"*.WXO-DD.lightning.#"},
    "radarsat_bc_site_radar.conf": {
        f"*.WXO-DD.radar.{product}.GIF.{station}.#"
        for product in ("DPQPE", "CAPPI")
        for station in ("CASAG", "CASHP", "CASSS", "CASPG")
    },
}


@dataclass(frozen=True)
class FeedSpec:
    directory: str
    suffix: str
    max_age_minutes: int
    pattern: re.Pattern[str]
    required: bool = True


FEEDS: dict[str, FeedSpec] = {
    "goes-daynight": FeedSpec(
        directory="satellite",
        suffix=".tif",
        max_age_minutes=45,
        pattern=re.compile(
            r"^(?P<time>\d{8}T\d{4}Z)_MSC_GOES-West_DayVis-NightIR_1km\.tif$"
        ),
    ),
    "goes-convective": FeedSpec(
        directory="satellite",
        suffix=".tif",
        max_age_minutes=45,
        pattern=re.compile(
            r"^(?P<time>\d{8}T\d{4}Z)_MSC_GOES-West_"
            r"VisibleIRSandwich-NightMicrophysicsIR_1km\.tif$"
        ),
    ),
    "goes-ir": FeedSpec(
        directory="satellite",
        suffix=".tif",
        max_age_minutes=45,
        pattern=re.compile(
            r"^(?P<time>\d{8}T\d{4}Z)_MSC_GOES-West_NightIR_2km\.tif$"
        ),
    ),
    "goes-snowfog": FeedSpec(
        directory="satellite",
        suffix=".tif",
        max_age_minutes=45,
        pattern=re.compile(
            r"^(?P<time>\d{8}T\d{4}Z)_MSC_GOES-West_"
            r"SnowFog-NightMicrophysics_1km\.tif$"
        ),
    ),
    "lightning": FeedSpec(
        directory="lightning",
        suffix=".tif",
        max_age_minutes=35,
        pattern=re.compile(r"^(?P<time>\d{8}T\d{4}Z)_MSC_Lightning_2\.5km\.tif$"),
    ),
}
for station in ("CASAG", "CASHP", "CASSS", "CASPG"):
    FEEDS[f"{station.lower()}-dpqpe"] = FeedSpec(
        directory="radar",
        suffix=".gif",
        max_age_minutes=20,
        pattern=re.compile(
            rf"^(?P<iso>\d{{8}}T\d{{4}}Z)_MSC_Radar-DPQPE_{station}_"
            rf"(?:Rain|Snow)(?:-Contingency)?\.gif$"
        ),
    )
    FEEDS[f"{station.lower()}-cappi"] = FeedSpec(
        directory="radar",
        suffix=".gif",
        max_age_minutes=20,
        pattern=re.compile(
            rf"^(?P<compact>\d{{12}})_{station}_CAPPI_1\."
            rf"(?:5_RAIN|0_SNOW)\.gif$"
        ),
    )


def parse_config(path: Path) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"{path}:{number}: expected 'option value'")
        settings.setdefault(parts[0], []).append(parts[1])
    return settings


def check_configs(config_dir: Path) -> list[str]:
    errors: list[str] = []
    for name in CONFIG_NAMES:
        path = config_dir / name
        if not path.is_file():
            errors.append(f"missing config: {path}")
            continue
        try:
            settings = parse_config(path)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue

        def require(option: str, value: str | None = None) -> None:
            values = settings.get(option, [])
            if not values:
                errors.append(f"{name}: missing {option}")
            elif value is not None and values[-1].lower() != value.lower():
                errors.append(f"{name}: {option} must be {value!r}, found {values[-1]!r}")

        require("broker", "amqps://anonymous@dd.weather.gc.ca/")
        require("topicPrefix", "v02.post")
        require("queueName", "q_${BROKER_USER}.${PROGRAM}.${CONFIG}.${HOSTNAME}")
        require("expire", "12h")
        require("retry_ttl", "12h")
        require("inflight", ".")
        require("mirror", "False")
        require("acceptUnmatched", "False")

        topics = set(settings.get("subtopic", []))
        expected = EXPECTED_TOPICS[name]
        if topics != expected:
            errors.append(
                f"{name}: subtopics differ; missing={sorted(expected - topics)}, "
                f"unexpected={sorted(topics - expected)}"
            )
        for expression in settings.get("accept", []):
            try:
                re.compile(expression)
            except re.error as exc:
                errors.append(f"{name}: invalid accept regex {expression!r}: {exc}")

    return errors


def source_time(path: Path, spec: FeedSpec) -> datetime | None:
    match = spec.pattern.match(path.name)
    if not match:
        return None
    text = match.groupdict().get("time") or match.groupdict().get("iso")
    if text:
        return datetime.strptime(text, "%Y%m%dT%H%MZ").replace(tzinfo=UTC)
    compact = match.groupdict().get("compact")
    if compact:
        return datetime.strptime(compact, "%Y%m%d%H%M").replace(tzinfo=UTC)
    return None


def has_expected_magic(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(6)
    except OSError:
        return False
    if path.suffix.lower() == ".gif":
        return header in (b"GIF87a", b"GIF89a")
    if path.suffix.lower() in (".tif", ".tiff"):
        return header[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
    return False


def check_spool(spool: Path, require_data: bool) -> list[str]:
    errors: list[str] = []
    now = datetime.now(UTC)
    for feed, spec in FEEDS.items():
        directory = spool / spec.directory
        candidates: list[tuple[datetime, Path]] = []
        if directory.is_dir():
            for path in directory.rglob(f"*{spec.suffix}"):
                if path.is_symlink() or not path.is_file():
                    continue
                timestamp = source_time(path, spec)
                if timestamp is not None:
                    candidates.append((timestamp, path))

        if not candidates:
            message = f"{feed}: no recognized files under {directory}"
            if require_data and spec.required:
                errors.append(message)
            else:
                print(f"WARN  {message}")
            continue

        timestamp, newest = max(candidates, key=lambda item: item[0])
        age_minutes = (now - timestamp).total_seconds() / 60
        status = "OK" if age_minutes <= spec.max_age_minutes else "STALE"
        print(
            f"{status:<5} {feed:<10} newest={timestamp:%Y-%m-%dT%H:%MZ} "
            f"age={age_minutes:.1f}m files={len(candidates)} path={newest}"
        )
        if not has_expected_magic(newest):
            errors.append(f"{feed}: newest file has invalid {spec.suffix} signature: {newest}")
        if age_minutes < -5:
            errors.append(f"{feed}: source time is {-age_minutes:.1f} minutes in the future")
        if age_minutes > spec.max_age_minutes and spec.required:
            errors.append(
                f"{feed}: newest source time is {age_minutes:.1f} minutes old "
                f"(limit {spec.max_age_minutes}m)"
            )
    return errors


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=project / "config" / "sarracenia" / "subscribe",
    )
    parser.add_argument(
        "--spool",
        type=Path,
        help="also inspect a live spool (default deployment path is ~/.local/share/radar-sat/spool/eccc)",
    )
    parser.add_argument(
        "--require-data",
        action="store_true",
        help="fail when a configured feed has no recognized file; implies --spool at the default path",
    )
    args = parser.parse_args()

    errors = check_configs(args.config_dir)
    print(f"Checked {len(CONFIG_NAMES)} Sarracenia configuration files in {args.config_dir}")

    spool = args.spool
    if args.require_data and spool is None:
        spool = Path.home() / ".local" / "share" / "radar-sat" / "spool" / "eccc"
    if spool is not None:
        errors.extend(check_spool(spool.expanduser().resolve(), args.require_data))

    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    if errors:
        return 1
    print("ECCC feed validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
