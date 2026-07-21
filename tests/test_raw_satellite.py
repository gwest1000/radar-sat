from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from radarsat.config import Domain
from radarsat.raw_satellite import (
    PublicSatelliteClient,
    RenderedSatellite,
    blend_satellites,
    normalized_frame_time,
)


UTC = dt.timezone.utc


class DiscoveryClient(PublicSatelliteClient):
    def __init__(self, objects: dict[str, list[tuple[str, int]]]) -> None:
        self.objects = objects

    def list_prefix(self, _bucket: str, prefix: str) -> list[tuple[str, int]]:
        return self.objects.get(prefix, [])

    def close(self) -> None:
        return None


class RawSatelliteTests(unittest.TestCase):
    def test_goes_scans_are_normalized_to_archive_clock(self) -> None:
        source = dt.datetime(2026, 7, 21, 6, 40, 20, tzinfo=UTC)
        self.assertEqual(normalized_frame_time(source), dt.datetime(2026, 7, 21, 6, 30, tzinfo=UTC))

    def test_latest_goes_selects_only_half_hour_cadence(self) -> None:
        prefix = "ABI-L2-MCMIPF/2026/202/06/"
        client = DiscoveryClient(
            {
                prefix: [
                    (prefix + "OR_ABI-L2-MCMIPF-M6_G18_s20262020630203_e.nc", 10),
                    (prefix + "OR_ABI-L2-MCMIPF-M6_G18_s20262020640203_e.nc", 20),
                    (prefix + "OR_ABI-L2-MCMIPF-M6_G18_s20262020650203_e.nc", 30),
                ]
            }
        )
        selected = client.latest_goes("G18", dt.datetime(2026, 7, 21, 6, 59, tzinfo=UTC))
        self.assertEqual(selected.valid_time.minute, 40)
        self.assertEqual(selected.size, 20)

    def test_himawari_requires_all_selected_bands_and_northern_segments(self) -> None:
        prefix = "AHI-L1b-FLDK/2026/07/21/0630/"
        values = []
        for band, resolution in (("01", "10"), ("02", "10"), ("03", "05"), ("13", "20")):
            for segment in range(1, 6):
                values.append(
                    (
                        prefix
                        + f"HS_H09_20260721_0630_B{band}_FLDK_R{resolution}_S{segment:02d}10.DAT.bz2",
                        100,
                    )
                )
        client = DiscoveryClient({prefix: values})
        selected = client.latest_himawari(dt.datetime(2026, 7, 21, 6, 55, tzinfo=UTC))
        self.assertEqual(len(selected), 20)
        self.assertEqual(sum(item.size for item in selected), 2000)

    def test_blend_prefers_the_only_valid_satellite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="test",
                title="test",
                west=-120,
                south=40,
                east=-80,
                north=60,
                crs="EPSG:3857",
                width=4,
                height=2,
                tier="broad",
            )

            def rendered(stem: str, colour: tuple[int, int, int], mask: np.ndarray) -> RenderedSatellite:
                visible = root / f"{stem}-visible.webp"
                infrared = root / f"{stem}-ir.webp"
                valid = root / f"{stem}-mask.png"
                Image.new("RGB", (4, 2), colour).save(visible, "WEBP", lossless=True)
                Image.new("RGB", (4, 2), colour).save(infrared, "WEBP", lossless=True)
                Image.fromarray(mask.astype(np.uint8) * 255).save(valid)
                return RenderedSatellite(visible, infrared, valid)

            left_mask = np.array([[1, 1, 0, 0], [1, 1, 0, 0]], dtype=bool)
            right_mask = ~left_mask
            left = rendered("left", (255, 0, 0), left_mask)
            right = rendered("right", (0, 0, 255), right_mask)
            visible = root / "visible.webp"
            infrared = root / "ir.webp"
            blend_satellites(left, right, domain, (-110, -90), visible, infrared)
            pixels = np.asarray(Image.open(visible).convert("RGB"))
            self.assertGreater(int(pixels[0, 0, 0]), int(pixels[0, 0, 2]))
            self.assertGreater(int(pixels[0, 3, 2]), int(pixels[0, 3, 0]))


if __name__ == "__main__":
    unittest.main()
