from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import requests
from PIL import Image

from radarsat.config import DOMAINS, LAYERS, Domain
from radarsat.pipeline import derive_raw_visir_archive, frame_path, metadata_path, write_metadata
from radarsat.raw_satellite import (
    PublicObject,
    PublicSatelliteClient,
    RenderedSatellite,
    _harmonize_visible_overlap,
    _infrared_image,
    blend_satellites,
    compose_visible_infrared,
    neutralize_archived_infrared,
    normalized_frame_time,
    solar_daylight_weight,
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
    def test_goes18_download_uses_google_mirror_then_aws_fallback(self) -> None:
        item = PublicObject(
            "noaa-goes18",
            "ABI-L2-MCMIPF/2026/203/21/example.nc",
            3,
            dt.datetime(2026, 7, 22, 21, 10, tzinfo=UTC),
        )
        self.assertEqual(
            item.urls,
            (
                "https://storage.googleapis.com/gcp-public-data-goes-18/"
                "ABI-L2-MCMIPF/2026/203/21/example.nc",
                "https://noaa-goes18.s3.amazonaws.com/"
                "ABI-L2-MCMIPF/2026/203/21/example.nc",
            ),
        )
        response = mock.Mock()
        response.iter_content.return_value = [b"abc"]
        session = mock.Mock()
        session.get.side_effect = [requests.ConnectionError("mirror unavailable"), response]
        client = PublicSatelliteClient()
        client.session = session
        with tempfile.TemporaryDirectory() as temporary:
            downloaded = client.download(item, Path(temporary), 10)
            self.assertEqual(downloaded.read_bytes(), b"abc")
        self.assertEqual(
            [call.args[0] for call in session.get.call_args_list], list(item.urls)
        )
        response.close.assert_called_once()

        goes19 = PublicObject(
            "noaa-goes19",
            "ABI-L2-MCMIPF/2026/203/21/example.nc",
            3,
            item.valid_time,
        )
        self.assertTrue(
            goes19.url.startswith(
                "https://storage.googleapis.com/gcp-public-data-goes-19/"
            )
        )

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
                infrared_gray = root / f"{stem}-ir-gray.webp"
                valid = root / f"{stem}-mask.png"
                Image.new("RGB", (4, 2), colour).save(visible, "WEBP", lossless=True)
                Image.new("RGB", (4, 2), colour).save(infrared, "WEBP", lossless=True)
                Image.new("RGB", (4, 2), colour).save(infrared_gray, "WEBP", lossless=True)
                Image.fromarray(mask.astype(np.uint8) * 255).save(valid)
                return RenderedSatellite(visible, infrared, infrared_gray, valid)

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

    def test_solar_blend_is_day_at_noon_and_night_at_midnight(self) -> None:
        latitude = np.array([[0.0]], dtype=np.float32)
        longitude = np.array([[0.0]], dtype=np.float32)
        noon = solar_daylight_weight(
            latitude,
            longitude,
            dt.datetime(2026, 3, 20, 12, tzinfo=UTC),
        )
        midnight = solar_daylight_weight(
            latitude,
            longitude,
            dt.datetime(2026, 3, 20, 0, tzinfo=UTC),
        )
        self.assertGreater(float(noon[0, 0]), 0.99)
        self.assertLess(float(midnight[0, 0]), 0.01)

    def test_overlap_harmonization_reduces_colour_bias_only_in_seam(self) -> None:
        width, height = 100, 20
        texture = np.linspace(60, 180, width, dtype=np.float32)
        left = np.broadcast_to(texture[None, :, None], (height, width, 3)).copy()
        left *= np.array([1.0, 0.92, 0.84], dtype=np.float32)
        right = np.clip(
            left * np.array([1.08, 0.93, 1.06], dtype=np.float32) + np.array([8, -4, 5]),
            0,
            255,
        )
        weights = np.broadcast_to(np.linspace(0, 1, width)[None, :], (height, width)).copy()
        mask = np.ones((height, width), dtype=bool)
        adjusted = _harmonize_visible_overlap(left, right, mask, mask, weights)
        seam = slice(35, 65)
        self.assertLess(
            float(np.mean(np.abs(adjusted[:, seam] - left[:, seam]))),
            float(np.mean(np.abs(right[:, seam] - left[:, seam]))),
        )
        self.assertTrue(np.allclose(adjusted[:, -1], right[:, -1]))

    def test_visir_night_side_is_neutral_not_false_colour(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="test",
                title="test",
                west=-0.1,
                south=-0.05,
                east=0.1,
                north=0.05,
                crs="EPSG:4326",
                width=4,
                height=2,
                tier="broad",
            )
            visible = root / "visible.png"
            infrared = root / "gray.png"
            destination = root / "visir.webp"
            Image.new("RGB", (4, 2), (20, 170, 90)).save(visible)
            Image.new("RGB", (4, 2), (180, 180, 180)).save(infrared)
            compose_visible_infrared(
                visible,
                infrared,
                domain,
                dt.datetime(2026, 3, 20, 0, tzinfo=UTC),
                destination,
            )
            pixels = np.asarray(Image.open(destination).convert("RGB"), dtype=np.int16)
            self.assertLess(int(np.max(np.ptp(pixels, axis=-1))), 4)

            compose_visible_infrared(
                visible,
                infrared,
                domain,
                dt.datetime(2026, 3, 20, 6, 10, tzinfo=UTC),
                destination,
            )
            twilight = np.asarray(Image.open(destination).convert("RGB"), dtype=np.int16)
            self.assertLess(int(np.max(np.ptp(twilight, axis=-1))), 6)

            Image.new("RGB", (4, 2), (0, 0, 0)).save(visible)
            Image.new("RGB", (4, 2), (0, 0, 0)).save(infrared)
            compose_visible_infrared(
                visible,
                infrared,
                domain,
                dt.datetime(2026, 3, 20, 12, tzinfo=UTC),
                destination,
            )
            self.assertEqual(Image.open(destination).convert("RGBA").getchannel("A").getextrema(), (0, 0))

    def test_archived_false_colour_ir_can_be_neutralized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enhanced = root / "enhanced.webp"
            neutral = root / "neutral.png"
            values = np.array([[193.15, 223.15, 273.15, 303.15]], dtype=np.float32)
            _infrared_image(values).save(enhanced, "WEBP", lossless=True)
            neutralize_archived_infrared(enhanced, neutral)
            pixels = np.asarray(Image.open(neutral).convert("RGB"))
            self.assertTrue(np.array_equal(pixels[..., 0], pixels[..., 1]))
            self.assertTrue(np.array_equal(pixels[..., 1], pixels[..., 2]))
            self.assertGreater(int(pixels[0, 0, 0]), int(pixels[0, -1, 0]))

    def test_archive_backfill_preserves_pair_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="tiny",
                title="tiny",
                west=-10,
                south=-5,
                east=10,
                north=5,
                crs="EPSG:4326",
                width=4,
                height=2,
                tier="broad",
            )
            valid = dt.datetime(2026, 3, 20, 0, tzinfo=UTC)
            visible = frame_path(root, domain, LAYERS["raw-visible"], valid)
            infrared = frame_path(root, domain, LAYERS["raw-ir"], valid)
            visible.parent.mkdir(parents=True, exist_ok=True)
            infrared.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 2), (20, 170, 90)).save(visible, "WEBP", lossless=True)
            _infrared_image(np.full((2, 4), 233.15, dtype=np.float32)).save(
                infrared, "WEBP", lossless=True
            )
            source_times = {"GOES-18": valid}
            write_metadata(
                root,
                domain,
                LAYERS["raw-visible"],
                valid,
                visible,
                source_times,
                extra={"renderVersion": 1},
            )
            write_metadata(
                root,
                domain,
                LAYERS["raw-ir"],
                valid,
                infrared,
                source_times,
                extra={"renderVersion": 1},
            )
            visible_before = visible.read_bytes()
            infrared_before = infrared.read_bytes()
            with mock.patch.dict(DOMAINS, {"tiny": domain}, clear=False):
                result = derive_raw_visir_archive(root, ["tiny"])
            destination = frame_path(root, domain, LAYERS["raw-visir"], valid)
            payload = json.loads(
                metadata_path(root, domain, LAYERS["raw-visir"], valid).read_text()
            )
            self.assertEqual(result["rendered"], {"tiny": 1})
            self.assertTrue(destination.is_file())
            self.assertEqual(payload["validTime"], "2026-03-20T00:00:00Z")
            self.assertTrue(payload["derivedFromArchivedFrames"])
            self.assertEqual(visible.read_bytes(), visible_before)
            self.assertEqual(infrared.read_bytes(), infrared_before)


if __name__ == "__main__":
    unittest.main()
