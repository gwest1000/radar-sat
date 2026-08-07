from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from radarsat.config import Domain
from radarsat.r2 import remote_valid_time
from radarsat.raster_tiles import (
    TileProfile,
    generate_tiles,
    prune_orphan_tiles,
    strip_stale_tile_references,
)


UTC = dt.timezone.utc


class RasterTileTests(unittest.TestCase):
    def test_generates_webp_pyramid_and_catalog_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="test",
                title="Test",
                west=-130,
                south=40,
                east=-120,
                north=50,
                crs="EPSG:3857",
                width=512,
                height=512,
                tier="broad",
                projected_bounds=(-14471533.8, 4865942.3, -13358338.9, 6446275.8),
            )
            frame = root / "frames/test/satellite/2026/07/28/20260728T1800Z.webp"
            frame.parent.mkdir(parents=True)
            Image.new("RGB", (512, 512), (32, 96, 144)).save(frame, "WEBP", quality=95)
            metadata = root / "metadata/test/satellite/2026/07/28/20260728T1800Z.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-07-28T18:00:00Z",
                "path": frame.relative_to(root).as_posix(),
            }))
            profile = TileProfile("test", "satellite", 3, 4)

            with mock.patch.dict("radarsat.raster_tiles.DOMAINS", {"test": domain}, clear=True):
                result = generate_tiles(root, metadata, profile)

            self.assertEqual(result["status"], "rendered")
            updated = json.loads(metadata.read_text())
            self.assertEqual(updated["tiles"]["format"], "webp")
            self.assertEqual(updated["tiles"]["encoding"], "lossy-webp")
            self.assertIn("{z}/{x}/{y}.webp", updated["tiles"]["template"])
            manifest = json.loads((root / updated["tiles"]["manifest"]).read_text())
            self.assertGreater(len(manifest["files"]), 0)
            self.assertTrue(all((root / path).is_file() for path in manifest["files"]))

            metadata.unlink()
            cleanup = prune_orphan_tiles(root)
            self.assertEqual(cleanup["manifests"], 1)
            self.assertEqual(cleanup["files"], len(manifest["files"]))
            self.assertFalse(any((root / path).exists() for path in manifest["files"]))

    def test_cleanup_removes_unreferenced_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="test",
                title="Test",
                west=-130,
                south=40,
                east=-120,
                north=50,
                crs="EPSG:3857",
                width=512,
                height=512,
                tier="broad",
                projected_bounds=(-14471533.8, 4865942.3, -13358338.9, 6446275.8),
            )
            frame = root / "frames/test/satellite/2026/07/28/20260728T1800Z.webp"
            frame.parent.mkdir(parents=True)
            Image.new("RGB", (512, 512), (32, 96, 144)).save(frame, "WEBP")
            metadata = root / "metadata/test/satellite/2026/07/28/20260728T1800Z.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-07-28T18:00:00Z",
                "path": frame.relative_to(root).as_posix(),
            }))
            profile = TileProfile("test", "satellite", 3, 3)
            with mock.patch.dict("radarsat.raster_tiles.DOMAINS", {"test": domain}, clear=True):
                generate_tiles(root, metadata, profile)
            payload = json.loads(metadata.read_text())
            manifest = root / payload["tiles"]["manifest"]
            payload.pop("tiles")
            metadata.write_text(json.dumps(payload))

            cleanup = prune_orphan_tiles(root)

            self.assertGreater(cleanup["files"], 0)
            self.assertEqual(cleanup["manifests"], 1)
            self.assertFalse(manifest.is_file())

    def test_stale_tile_reference_is_stripped_for_whole_frame_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="test",
                title="Test",
                west=-130,
                south=40,
                east=-120,
                north=50,
                crs="EPSG:3857",
                width=512,
                height=512,
                tier="broad",
                projected_bounds=(-14471533.8, 4865942.3, -13358338.9, 6446275.8),
            )
            frame = root / "frames/test/satellite/2026/07/20/20260720T1800Z.webp"
            frame.parent.mkdir(parents=True)
            Image.new("RGB", (512, 512), (32, 96, 144)).save(frame, "WEBP")
            metadata = root / "metadata/test/satellite/2026/07/20/20260720T1800Z.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-07-20T18:00:00Z",
                "path": frame.relative_to(root).as_posix(),
            }))
            profile = TileProfile("test", "satellite", 3, 3)
            with mock.patch.dict("radarsat.raster_tiles.DOMAINS", {"test": domain}, clear=True):
                generate_tiles(root, metadata, profile)
                stripped = strip_stale_tile_references(
                    root,
                    (profile,),
                    hours=1,
                    now=dt.datetime(2026, 7, 28, 18, tzinfo=UTC),
                )

            self.assertEqual(stripped, 1)
            self.assertNotIn("tiles", json.loads(metadata.read_text()))

    def test_transparent_frame_uses_whole_frame_instead_of_empty_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="test",
                title="Test",
                west=-130,
                south=40,
                east=-120,
                north=50,
                crs="EPSG:3857",
                width=512,
                height=512,
                tier="broad",
                projected_bounds=(-14471533.8, 4865942.3, -13358338.9, 6446275.8),
            )
            frame = root / "frames/test/smoke/2026/07/28/20260728T1800Z.webp"
            frame.parent.mkdir(parents=True)
            Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(frame, "WEBP", lossless=True)
            metadata = root / "metadata/test/smoke/2026/07/28/20260728T1800Z.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-07-28T18:00:00Z",
                "path": frame.relative_to(root).as_posix(),
            }))
            profile = TileProfile("test", "smoke", 3, 3, "lossless-webp")

            with mock.patch.dict("radarsat.raster_tiles.DOMAINS", {"test": domain}, clear=True):
                result = generate_tiles(root, metadata, profile)

            self.assertEqual(result["status"], "empty")
            self.assertNotIn("tiles", json.loads(metadata.read_text()))
            self.assertFalse((root / "tile-manifests/test/smoke").exists())

    def test_tile_objects_participate_in_archive_retention(self) -> None:
        parsed = remote_valid_time(
            "tiles/bc/raw-visir-5min/2026/07/28/20260728T1800Z/7/19/42.webp"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], dt.datetime(2026, 7, 28, 18, 0, tzinfo=UTC))
        self.assertEqual(parsed[1], "bc")


if __name__ == "__main__":
    unittest.main()
