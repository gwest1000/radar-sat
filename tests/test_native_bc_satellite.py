from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from radarsat.config import DOMAINS, LAYERS, Domain
from radarsat.native_bc_satellite import (
    NATIVE_BC_CHANNELS,
    NATIVE_BC_HEIGHT,
    NATIVE_BC_RENDER_VERSION,
    NATIVE_BC_WIDTH,
    NativeBcDiscovery,
    NativeBcScan,
    discover_native_bc_scans,
    plan_native_bc_backfill,
    render_native_bc_scan,
)
from radarsat.pipeline import frame_path, metadata_path
from radarsat.raw_satellite import PublicObject, RenderedSatellite


UTC = dt.timezone.utc


def key(value: dt.datetime, channel: str) -> str:
    stamp = value.strftime("%Y%j%H%M%S")
    return (
        f"ABI-L2-CMIPF/{value:%Y}/{value:%j}/{value:%H}/"
        f"OR_ABI-L2-CMIPF-M6C{channel}_G18_s{stamp}0_e{stamp}0_c{stamp}0.nc"
    )


def scan(value: dt.datetime, size: int = 10) -> NativeBcScan:
    return NativeBcScan(
        value,
        tuple(PublicObject("noaa-goes18", key(value, channel), size, value) for channel in NATIVE_BC_CHANNELS),
    )


class ListingClient:
    def __init__(self, objects: list[tuple[str, int]]) -> None:
        self.objects = objects

    def list_prefix(self, _bucket: str, _prefix: str) -> list[tuple[str, int]]:
        return self.objects


class DownloadClient:
    def download(self, item: PublicObject, cache_root: Path, _maximum: int) -> Path:
        destination = cache_root / "downloads" / Path(item.key).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * item.size)
        return destination


class NativeBcTests(unittest.TestCase):
    def test_discovery_requires_all_four_native_channels(self) -> None:
        valid = dt.datetime(2026, 7, 22, 20, 0, 20, tzinfo=UTC)
        incomplete = valid + dt.timedelta(minutes=10)
        objects = [(key(valid, channel), 10) for channel in NATIVE_BC_CHANNELS]
        objects.extend((key(incomplete, channel), 10) for channel in NATIVE_BC_CHANNELS[:-1])

        result = discover_native_bc_scans(
            ListingClient(objects),  # type: ignore[arg-type]
            valid - dt.timedelta(minutes=1),
            incomplete + dt.timedelta(minutes=1),
        )

        self.assertEqual([item.valid_time for item in result.scans], [valid])
        self.assertEqual(result.scans[0].size, 40)

    def test_plan_is_daylight_only_and_byte_bounded(self) -> None:
        daylight = scan(dt.datetime(2026, 7, 22, 20, 0, tzinfo=UTC), 30)
        night = scan(dt.datetime(2026, 1, 22, 8, 0, tzinfo=UTC), 30)
        with tempfile.TemporaryDirectory() as temporary:
            plan = plan_native_bc_backfill(
                Path(temporary),
                NativeBcDiscovery((daylight, night)),
                max_frames=2,
                max_download_bytes=121,
            )
        self.assertEqual(plan.scans, (daylight,))
        self.assertEqual(plan.skipped_night, 1)
        self.assertEqual(plan.estimated_bytes, 120)

    def test_render_installs_only_the_native_visir_product_and_cleans_sources(self) -> None:
        valid = dt.datetime(2026, 7, 22, 20, 0, 20, tzinfo=UTC)
        source = scan(valid, 4)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            cache = base / "cache"

            def fake_render(
                source_paths: object,
                _reader: str,
                _infrared_dataset: str,
                domain: Domain,
                work_root: Path,
                stem: str,
            ) -> RenderedSatellite:
                self.assertEqual((domain.width, domain.height), (NATIVE_BC_WIDTH, NATIVE_BC_HEIGHT))
                self.assertEqual(len(list(source_paths)), 4)  # type: ignore[arg-type]
                render_root = work_root / "renders"
                render_root.mkdir(parents=True, exist_ok=True)
                paths = {
                    "visible": render_root / f"{stem}-visible.webp",
                    "infrared": render_root / f"{stem}-ir.webp",
                    "infrared_gray": render_root / f"{stem}-gray.webp",
                    "valid_mask": render_root / f"{stem}-mask.png",
                }
                Image.new("RGB", (30, 23), (30, 150, 80)).save(paths["visible"], "WEBP")
                Image.new("RGB", (30, 23), (220, 30, 120)).save(paths["infrared"], "WEBP")
                Image.new("RGBA", (30, 23), (150, 150, 150, 255)).save(paths["infrared_gray"], "WEBP")
                Image.new("L", (30, 23), 255).save(paths["valid_mask"], "PNG")
                return RenderedSatellite(**paths)

            def fake_compose(
                _visible: Path,
                _infrared: Path,
                _domain: Domain,
                _valid: dt.datetime,
                destination: Path,
            ) -> dict[str, object]:
                Image.new("RGB", (30, 23), (80, 120, 100)).save(destination, "WEBP")
                return {"solarBlend": True}

            with mock.patch(
                "radarsat.native_bc_satellite.compose_visible_infrared",
                side_effect=fake_compose,
            ):
                result = render_native_bc_scan(
                    root,
                    source,
                    DownloadClient(),  # type: ignore[arg-type]
                    cache,
                    max_source_bytes=100,
                    render_source=fake_render,
                )

            layer = LAYERS["raw-visir-native"]
            image = frame_path(root, DOMAINS["bc"], layer, valid)
            payload = json.loads(metadata_path(root, DOMAINS["bc"], layer, valid).read_text())
            self.assertEqual(result.status, "rendered")
            self.assertTrue(image.is_file())
            self.assertEqual(payload["renderVersion"], NATIVE_BC_RENDER_VERSION)
            self.assertEqual(payload["renderWidth"], NATIVE_BC_WIDTH)
            self.assertEqual(payload["retentionHours"], 24)
            self.assertFalse((root / "metadata" / "bc" / "raw-visible").exists())
            self.assertFalse((cache / "downloads").exists() and any((cache / "downloads").iterdir()))


if __name__ == "__main__":
    unittest.main()
