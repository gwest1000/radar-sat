from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from radarsat.config import LAYERS, Domain
from radarsat.noaa_star_geocolor import (
    FULL_DISK,
    PACUS,
    DiscoveryResult,
    StarScan,
    _fallback_source_time,
    discover_scans,
    plan_backfill,
    render_scan,
    select_fallback,
)
from radarsat.pipeline import frame_path, write_metadata


UTC = dt.timezone.utc


def tiny_domain() -> Domain:
    return Domain(
        id="bc",
        title="Tiny BC",
        west=-145,
        south=45,
        east=-108,
        north=63,
        crs="EPSG:4326",
        width=12,
        height=8,
        tier="bc",
    )


class ListingClient:
    def __init__(self, listing: str) -> None:
        self.listing = listing

    def directory(self, _sector: object) -> str:
        return self.listing


class StarGeoColorTests(unittest.TestCase):
    def test_concurrent_renders_use_isolated_transient_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            cache = Path(temporary) / "cache"
            domain = tiny_domain()
            seen: list[Path] = []

            class DownloadClient:
                def download(
                    self,
                    scan: StarScan,
                    scan_cache: Path,
                    _maximum: int,
                ) -> Path:
                    seen.append(scan_cache)
                    destination = scan_cache / "downloads" / scan.filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"jpeg")
                    return destination

            def projector(
                _source: Path,
                _sector: object,
                selected: Domain,
                _cache: Path,
            ) -> tuple[np.ndarray, np.ndarray]:
                return (
                    np.zeros((selected.height, selected.width, 3), dtype=np.uint8),
                    np.full((selected.height, selected.width), 255, dtype=np.uint8),
                )

            scans = tuple(
                StarScan(
                    FULL_DISK,
                    dt.datetime(2026, 7, 24, 17, minute, tzinfo=UTC),
                    dt.datetime(2026, 7, 24, 17, minute, tzinfo=UTC),
                    f"{minute}.jpg",
                    4,
                )
                for minute in (0, 10)
            )
            with mock.patch.dict(
                "radarsat.noaa_star_geocolor.DOMAINS",
                {"bc": domain},
                clear=True,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(
                        lambda scan: render_scan(
                            root,
                            scan,
                            DownloadClient(),  # type: ignore[arg-type]
                            cache,
                            projector=projector,
                        ),
                        scans,
                    ))

            self.assertEqual([item.status for item in results], ["rendered", "rendered"])
            self.assertEqual(len(set(seen)), 2)
            self.assertFalse(any(path.exists() for path in seen))

    def test_discovery_reads_sizes_and_normalizes_pacus_clock(self) -> None:
        listing = "\n".join(
            (
                '<a href="20262051701_GOES18-ABI-CONUS-GEOCOLOR-10000x6000.jpg">'
                "20262051701_GOES18-ABI-CONUS-GEOCOLOR-10000x6000.jpg</a> "
                "24-Jul-2026 17:04 29000000",
                '<a href="20262051706_GOES18-ABI-CONUS-GEOCOLOR-10000x6000.jpg">'
                "20262051706_GOES18-ABI-CONUS-GEOCOLOR-10000x6000.jpg</a> "
                "24-Jul-2026 17:09 29100000",
            )
        )
        result = discover_scans(
            ListingClient(listing),  # type: ignore[arg-type]
            PACUS,
            dt.datetime(2026, 7, 24, 17, 0, tzinfo=UTC),
            dt.datetime(2026, 7, 24, 17, 10, tzinfo=UTC),
        )

        self.assertEqual([scan.valid_time.minute for scan in result.scans], [5, 0])
        self.assertEqual([scan.size for scan in result.scans], [29_100_000, 29_000_000])
        self.assertEqual(result.scans[-1].source_time.minute, 1)

    def test_plan_limits_newest_scans_but_repairs_them_oldest_first(self) -> None:
        scans = tuple(
            StarScan(
                FULL_DISK,
                dt.datetime(2026, 7, 24, 17, minute, tzinfo=UTC),
                dt.datetime(2026, 7, 24, 17, minute, 21, tzinfo=UTC),
                f"{minute}.jpg",
                55,
            )
            for minute in (30, 20, 10)
        )
        plan = plan_backfill(
            Path("unused"),
            DiscoveryResult(scans),
            max_frames=2,
            max_download_bytes=110,
        )

        self.assertEqual([scan.valid_time.minute for scan in plan.scans], [20, 30])
        self.assertEqual(plan.excluded_by_frame_limit, 1)

    def test_explicit_fallback_time_wins_over_pacus_source_time(self) -> None:
        payload = {
            "fallbackSourceTime": "2026-07-24T17:00:21Z",
            "sourceTimes": {
                "NOAA STAR GOES-18 PACUS GeoColor": "2026-07-24T17:16:17Z",
                "GOES-18 full-disk fallback": "2026-07-24T17:00:21Z",
            },
        }
        self.assertEqual(
            _fallback_source_time(payload),
            dt.datetime(2026, 7, 24, 17, 0, 21, tzinfo=UTC),
        )

    def test_fallback_selection_cannot_step_behind_adjacent_rapid_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = tiny_domain()
            full_times = (
                dt.datetime(2026, 7, 24, 17, 0, 21, tzinfo=UTC),
                dt.datetime(2026, 7, 24, 17, 10, 21, tzinfo=UTC),
            )
            for value in full_times:
                destination = frame_path(root, domain, LAYERS["raw-visir"], value)
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (12, 8), "black").save(destination, "WEBP")
                write_metadata(
                    root,
                    domain,
                    LAYERS["raw-visir"],
                    value,
                    destination,
                    {"GOES-18 full-disk GeoColor": value},
                )

            for valid, fallback in (
                (
                    dt.datetime(2026, 7, 24, 17, 10, tzinfo=UTC),
                    full_times[1],
                ),
                (
                    dt.datetime(2026, 7, 24, 17, 20, tzinfo=UTC),
                    full_times[1],
                ),
            ):
                destination = frame_path(root, domain, LAYERS["raw-visir-5min"], valid)
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (12, 8), "black").save(destination, "WEBP")
                write_metadata(
                    root,
                    domain,
                    LAYERS["raw-visir-5min"],
                    valid,
                    destination,
                    {
                        "NOAA STAR GOES-18 PACUS GeoColor": valid,
                        "GOES-18 full-disk fallback": fallback,
                    },
                    extra={"fallbackSourceTime": fallback.isoformat()},
                )

            with mock.patch.dict(
                "radarsat.noaa_star_geocolor.DOMAINS",
                {"bc": domain},
                clear=True,
            ):
                _, _, selected = select_fallback(
                    root,
                    dt.datetime(2026, 7, 24, 17, 15, tzinfo=UTC),
                    dt.datetime(2026, 7, 24, 17, 16, 17, tzinfo=UTC),
                )

            self.assertEqual(selected, full_times[1])


if __name__ == "__main__":
    unittest.main()
