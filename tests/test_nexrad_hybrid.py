from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from radarsat.config import LAYERS, VIEWPORTS
from radarsat.nexrad_hybrid import (
    DPR_GATE_COUNT,
    DPR_RADIAL_COUNT,
    NexradObject,
    SOUTH_COAST_LAYER_ID,
    _anchor_times,
    derive_south_coast_hybrid_radar,
    parse_nexrad_object,
)


UTC = dt.timezone.utc


class FakeSource:
    def __init__(self, items: list[NexradObject]) -> None:
        self.items = items

    def objects_between(
        self,
        sites: object,
        start: dt.datetime,
        end: dt.datetime,
    ) -> list[NexradObject]:
        return [item for item in self.items if start <= item.valid_time <= end]

    def fetch(self, item: NexradObject, cache_root: Path) -> Path:
        destination = cache_root / item.site / item.key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic-dpr")
        return destination


class NexradHybridTests(unittest.TestCase):
    def test_object_names_are_strictly_parsed(self) -> None:
        item = parse_nexrad_object("ATX_DPR_2026_08_15_22_56_51", size=3012)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.site, "ATX")
        self.assertEqual(item.size, 3012)
        self.assertEqual(item.valid_time, dt.datetime(2026, 8, 15, 22, 56, 51, tzinfo=UTC))
        self.assertIsNone(parse_nexrad_object("ATX_N0B_2026_08_15_22_56_51"))
        self.assertIsNone(parse_nexrad_object("../ATX_DPR_2026_08_15_22_56_51"))

    def test_layer_is_limited_to_south_coast(self) -> None:
        self.assertIn(SOUTH_COAST_LAYER_ID, LAYERS)
        self.assertNotIn("radar-rain-region-small", LAYERS)
        self.assertEqual(
            LAYERS[SOUTH_COAST_LAYER_ID].source,
            "ECCC GeoMet + NOAA NEXRAD Level III",
        )

    def test_backfill_clock_is_ten_minutes_then_hourly(self) -> None:
        from radarsat.nexrad_hybrid import LocalRadarFrame

        now = dt.datetime(2026, 8, 15, 20, 24, tzinfo=UTC)
        frames = [
            LocalRadarFrame(
                now - dt.timedelta(hours=30) + dt.timedelta(minutes=6 * index),
                Path("frame.png"),
                Path("frame.json"),
            )
            for index in range(301)
        ]
        anchors = _anchor_times(frames, 30, False, now)
        older = [value for value in anchors if now - value > dt.timedelta(hours=24)]
        recent = [value for value in anchors if now - value <= dt.timedelta(hours=24)]
        self.assertTrue(older)
        self.assertTrue(all(value.minute == 0 for value in older))
        self.assertTrue(any(value.minute not in {0} for value in recent))

    def test_render_keeps_eccc_base_and_inserts_dpr_on_stage_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            valid = dt.datetime(2026, 8, 15, 22, 56, tzinfo=UTC)
            base = root / "frames/bc/radar-rain/2026/08/15/20260815T2256Z.png"
            base.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (300, 230), (8, 9, 10, 255)).save(base)
            metadata = root / "metadata/bc/radar-rain/2026/08/15/20260815T2256Z.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-08-15T22:56:00Z",
                "path": base.relative_to(root).as_posix(),
                "fetchedAt": "2026-08-15T22:57:00Z",
            }))
            item = NexradObject("ATX", "ATX_DPR_2026_08_15_22_55_00", valid - dt.timedelta(minutes=1))
            rate = np.zeros((DPR_RADIAL_COUNT, DPR_GATE_COUNT), dtype=np.float32)
            rate[0, 0] = 10.0
            height = 1346
            radial = np.zeros((height, 1920), dtype=np.uint16)
            gate = np.zeros((height, 1920), dtype=np.uint16)
            coverage = np.zeros((height, 1920), dtype=bool)
            coverage[:, :960] = True

            with (
                mock.patch(
                    "radarsat.nexrad_hybrid._decode_dpr",
                    return_value=(rate, item.valid_time, "KATX"),
                ),
                mock.patch(
                    "radarsat.nexrad_hybrid._sampling_map",
                    return_value=(radial, gate, coverage),
                ),
            ):
                result = derive_south_coast_hybrid_radar(
                    root,
                    hours=1,
                    latest_only=True,
                    source=FakeSource([item]),
                    now=valid + dt.timedelta(minutes=1),
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["rendered"], 1)
            destination = root / "frames/bc" / SOUTH_COAST_LAYER_ID / "2026/08/15/20260815T2256Z.png"
            with Image.open(destination) as rendered:
                self.assertEqual(rendered.size, (1920, height))
                self.assertNotEqual(rendered.getpixel((100, 100)), (8, 9, 10, 255))
                self.assertEqual(rendered.getpixel((1800, 100)), (8, 9, 10, 255))
            output_metadata = json.loads(
                (root / "metadata/bc" / SOUTH_COAST_LAYER_ID / "2026/08/15/20260815T2256Z.json").read_text()
            )
            self.assertEqual(output_metadata["regionalViewport"], VIEWPORTS["south-coast"])
            self.assertEqual(output_metadata["nexradObjects"], [item.key])
            self.assertIn("NOAA NEXRAD KATX DPR", output_metadata["sourceTimes"])

    def test_latest_edge_can_advance_to_a_newer_us_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            base_valid = dt.datetime(2026, 8, 15, 22, 54, tzinfo=UTC)
            base = root / "frames/bc/radar-rain/2026/08/15/20260815T2254Z.png"
            base.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (30, 23), (0, 0, 0, 0)).save(base)
            metadata = root / "metadata/bc/radar-rain/2026/08/15/20260815T2254Z.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-08-15T22:54:00Z",
                "path": base.relative_to(root).as_posix(),
                "fetchedAt": "2026-08-15T22:55:00Z",
            }))
            us_valid = base_valid + dt.timedelta(minutes=3)
            item = NexradObject("ATX", "ATX_DPR_2026_08_15_22_57_00", us_valid)
            rate = np.zeros((DPR_RADIAL_COUNT, DPR_GATE_COUNT), dtype=np.float32)
            sampling = (
                np.zeros((1346, 1920), dtype=np.uint16),
                np.zeros((1346, 1920), dtype=np.uint16),
                np.zeros((1346, 1920), dtype=bool),
            )
            with (
                mock.patch(
                    "radarsat.nexrad_hybrid._decode_dpr",
                    return_value=(rate, us_valid, "KATX"),
                ),
                mock.patch("radarsat.nexrad_hybrid._sampling_map", return_value=sampling),
            ):
                result = derive_south_coast_hybrid_radar(
                    root,
                    latest_only=True,
                    source=FakeSource([item]),
                    now=us_valid + dt.timedelta(minutes=1),
                )

            self.assertEqual(result["latest"], "2026-08-15T22:57:00Z")
            self.assertTrue(
                (root / "metadata/bc" / SOUTH_COAST_LAYER_ID / "2026/08/15/20260815T2257Z.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
