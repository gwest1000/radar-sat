from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from radarsat.config import LAYERS, Domain
from radarsat.five_minute_bc_satellite import (
    RENDER_VERSION,
    DiscoveryResult,
    FiveMinuteScan,
    _fallback_frame,
    discover_scans,
    plan_backfill,
    render_scan,
)
from radarsat.pipeline import frame_path, metadata_path, write_metadata
from radarsat.raw_satellite import PublicObject, RenderedSatellite


UTC = dt.timezone.utc


def key(value: dt.datetime) -> str:
    stamp = value.strftime("%Y%j%H%M%S")
    return (
        f"ABI-L2-MCMIPC/{value:%Y}/{value:%j}/{value:%H}/"
        f"OR_ABI-L2-MCMIPC-M6_G18_s{stamp}0_e{stamp}0_c{stamp}0.nc"
    )


def tiny_domain() -> Domain:
    return Domain(
        id="bc",
        title="Tiny five-minute BC",
        west=-145,
        south=45,
        east=-108,
        north=63,
        crs="EPSG:4326",
        width=12,
        height=8,
        tier="bc",
    )


class DiscoveryClient:
    def __init__(self, objects: dict[str, list[tuple[str, int]]]) -> None:
        self.objects = objects

    def list_prefix(self, _bucket: str, prefix: str) -> list[tuple[str, int]]:
        return self.objects.get(prefix, [])


class DownloadClient:
    def download(self, item: PublicObject, cache_root: Path, _maximum: int) -> Path:
        destination = cache_root / "downloads" / Path(item.key).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * item.size)
        return destination


class FiveMinuteSatelliteTests(unittest.TestCase):
    def test_fallback_allows_one_delayed_full_disk_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = tiny_domain()
            fallback_time = dt.datetime(2026, 7, 23, 1, 20, tzinfo=UTC)
            fallback = frame_path(root, domain, LAYERS["raw-visir"], fallback_time)
            fallback.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (domain.width, domain.height), "black").save(fallback, "WEBP")
            write_metadata(
                root,
                domain,
                LAYERS["raw-visir"],
                fallback_time,
                fallback,
                {"GOES-18 ABI scan start": fallback_time},
            )

            selected, _ = _fallback_frame(
                root,
                dt.datetime(2026, 7, 23, 1, 46, 17, tzinfo=UTC),
            )

            self.assertEqual(selected.resolve(), fallback.resolve())

    def test_discovery_preserves_source_time_and_normalizes_display_clock(self) -> None:
        first = dt.datetime(2026, 7, 23, 1, 31, 17, tzinfo=UTC)
        second = dt.datetime(2026, 7, 23, 1, 36, 17, tzinfo=UTC)
        prefix = "ABI-L2-MCMIPC/2026/204/01/"
        result = discover_scans(
            DiscoveryClient({prefix: [(key(first), 53), (key(second), 54)]}),
            dt.datetime(2026, 7, 23, 1, 30, tzinfo=UTC),
            dt.datetime(2026, 7, 23, 1, 40, tzinfo=UTC),
        )
        self.assertEqual(
            [scan.valid_time.minute for scan in result.scans],
            [35, 30],
        )
        self.assertEqual(result.scans[-1].source_time, first)

    def test_plan_is_newest_first_and_bounded_by_bytes(self) -> None:
        scans = tuple(
            FiveMinuteScan(
                dt.datetime(2026, 7, 23, 1, minute, tzinfo=UTC),
                dt.datetime(2026, 7, 23, 1, minute + 1, 17, tzinfo=UTC),
                PublicObject("noaa-goes18", key(dt.datetime(2026, 7, 23, 1, minute + 1, 17, tzinfo=UTC)), 55, dt.datetime(2026, 7, 23, 1, minute + 1, 17, tzinfo=UTC)),
            )
            for minute in (35, 30, 25)
        )
        plan = plan_backfill(
            Path("unused"),
            DiscoveryResult(scans),
            max_frames=2,
            max_download_bytes=60,
        )
        self.assertEqual(len(plan.scans), 1)
        self.assertEqual(plan.scans[0], scans[0])
        self.assertEqual(plan.excluded_by_frame_limit, 1)
        self.assertEqual(plan.excluded_by_byte_limit, 1)

    def test_render_composites_pacus_over_recent_full_disk_and_writes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            cache = Path(temporary) / "cache"
            domain = tiny_domain()
            fallback_time = dt.datetime(2026, 7, 23, 1, 30, 20, tzinfo=UTC)
            fallback = frame_path(root, domain, LAYERS["raw-visir"], fallback_time)
            fallback.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (domain.width, domain.height), (180, 20, 20)).save(fallback, "WEBP", lossless=True)
            write_metadata(
                root,
                domain,
                LAYERS["raw-visir"],
                fallback_time,
                fallback,
                {"GOES-18 ABI scan start": fallback_time},
            )
            source_time = dt.datetime(2026, 7, 23, 1, 36, 17, tzinfo=UTC)
            scan = FiveMinuteScan(
                dt.datetime(2026, 7, 23, 1, 35, tzinfo=UTC),
                source_time,
                PublicObject("noaa-goes18", key(source_time), 10, source_time),
            )

            def fake_render(
                _paths: object,
                _reader: str,
                _infrared: str,
                selected: Domain,
                work: Path,
                stem: str,
            ) -> RenderedSatellite:
                render_root = work / "renders"
                render_root.mkdir(parents=True, exist_ok=True)
                visible = render_root / f"{stem}-visible.webp"
                infrared = render_root / f"{stem}-ir.webp"
                infrared_gray = render_root / f"{stem}-ir-gray.webp"
                mask = render_root / f"{stem}-mask.png"
                Image.new("RGB", (selected.width, selected.height), (20, 170, 60)).save(visible, "WEBP", lossless=True)
                Image.new("RGBA", (selected.width, selected.height), (50, 50, 50, 255)).save(infrared, "WEBP", lossless=True)
                Image.new("RGBA", (selected.width, selected.height), (70, 70, 70, 255)).save(infrared_gray, "WEBP", lossless=True)
                validity = Image.new("L", (selected.width, selected.height), 0)
                validity.paste(255, (0, selected.height // 2, selected.width, selected.height))
                validity.save(mask)
                return RenderedSatellite(visible, infrared, infrared_gray, mask)

            with mock.patch.dict("radarsat.five_minute_bc_satellite.DOMAINS", {"bc": domain}, clear=True):
                result = render_scan(
                    root,
                    scan,
                    DownloadClient(),  # type: ignore[arg-type]
                    cache,
                    render_source=fake_render,
                )

            self.assertEqual(result.status, "rendered")
            destination = frame_path(root, domain, LAYERS["raw-visir-5min"], scan.valid_time)
            payload = json.loads(metadata_path(root, domain, LAYERS["raw-visir-5min"], scan.valid_time).read_text())
            self.assertEqual(payload["renderVersion"], RENDER_VERSION)
            self.assertEqual(payload["nominalCadenceMinutes"], 5)
            self.assertEqual(payload["retentionHours"], 24)
            with Image.open(destination).convert("RGB") as output:
                top = output.getpixel((domain.width // 2, 0))
                bottom = output.getpixel((domain.width // 2, domain.height - 1))
            self.assertGreater(top[0], top[1])
            self.assertNotEqual(top, bottom)
            self.assertFalse((cache / "downloads").exists() and any((cache / "downloads").iterdir()))


if __name__ == "__main__":
    unittest.main()
