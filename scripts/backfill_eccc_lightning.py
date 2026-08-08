#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urljoin

import requests

from radarsat.catalog import write_catalog
from radarsat.config import DOMAINS
from radarsat.pipeline import LIGHTNING_ARCHIVE_HOURS, derive_lightning_trails
from radarsat.spool import ingest_spool


UTC = dt.timezone.utc
DEFAULT_DOMAINS = ("bc", "north-america", "north-pacific")
DATAMART_ROOT = "https://dd.weather.gc.ca"
LIGHTNING_NAME = re.compile(r"^(\d{8}T\d{4}Z)_MSC_Lightning_2\.5km\.tif$")
MAX_SOURCE_BYTES = 5_000_000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill dated ECCC/CLDN GeoTIFFs and rebuild broad-domain lightning."
    )
    parser.add_argument("--output-root", type=Path, default=Path("data/output"))
    parser.add_argument(
        "--spool-root",
        type=Path,
        default=Path.home() / ".local/share/radar-sat/spool/eccc",
    )
    parser.add_argument(
        "--domain",
        action="append",
        choices=DEFAULT_DOMAINS,
        help="Domain to backfill; repeat as needed (default: all displayed domains).",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=LIGHTNING_ARCHIVE_HOURS,
        help="Requested source history, capped at seven days.",
    )
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    if args.hours <= 0 or args.workers <= 0:
        parser.error("--hours and --workers must be positive")
    return args


def _listed_files(hours: float, now: dt.datetime) -> list[tuple[str, str]]:
    cutoff = now - dt.timedelta(hours=hours)
    current_date = cutoff.date()
    values: list[tuple[str, str]] = []
    while current_date <= now.date():
        directory = f"{DATAMART_ROOT}/{current_date:%Y%m%d}/WXO-DD/lightning/"
        response = requests.get(directory, timeout=45)
        response.raise_for_status()
        for href in re.findall(r'href=["\']([^"\']+)', response.text, flags=re.IGNORECASE):
            name = Path(unquote(href)).name
            match = LIGHTNING_NAME.fullmatch(name)
            if match is None:
                continue
            valid = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%MZ").replace(tzinfo=UTC)
            if cutoff <= valid <= now + dt.timedelta(minutes=20):
                values.append((urljoin(directory, href), name))
        current_date += dt.timedelta(days=1)
    return sorted(set(values), key=lambda item: item[1])


def _download(url: str, destination: Path) -> str:
    if destination.is_file() and 0 < destination.stat().st_size <= MAX_SOURCE_BYTES:
        return "existing"
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    content = response.content
    if not 0 < len(content) <= MAX_SOURCE_BYTES or content[:4] not in {
        b"II*\x00",
        b"MM\x00*",
        b"II+\x00",
        b"MM\x00+",
    }:
        raise RuntimeError(f"Invalid lightning GeoTIFF from {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return "downloaded"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output_root.resolve()
    spool_root = args.spool_root.expanduser().resolve()
    hours = min(float(args.hours), LIGHTNING_ARCHIVE_HOURS)
    domain_ids = args.domain or list(DEFAULT_DOMAINS)
    now = dt.datetime.now(UTC)
    listed = _listed_files(hours, now)
    downloads = {"downloaded": 0, "existing": 0, "failed": 0}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_download, url, spool_root / "lightning" / name): name
            for url, name in listed
        }
        for future in as_completed(futures):
            try:
                downloads[future.result()] += 1
            except Exception as error:
                downloads["failed"] += 1
                failures.append(f"{futures[future]}: {type(error).__name__}: {error}")

    results: dict[str, object] = {}
    for domain_id in domain_ids:
        domain = DOMAINS[domain_id]
        result = ingest_spool(
            spool_root,
            output_root,
            domain,
            hours,
            latest_only=False,
            now=now,
            include_layers=("lightning",),
        )
        derive_lightning_trails(output_root, domain, result.timelines, hours)
        results[domain_id] = result.status()
    catalog = write_catalog(output_root)
    print(json.dumps({
        "listed": len(listed),
        "downloads": downloads,
        "failures": failures[:20],
        "domains": results,
        "catalog": str(catalog),
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
