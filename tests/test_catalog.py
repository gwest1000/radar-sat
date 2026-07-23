from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from radarsat.catalog import build_catalog, write_catalog
from radarsat.config import DOMAINS, LAYERS
from radarsat.pipeline import frame_path, metadata_path, write_metadata


UTC = dt.timezone.utc


class CatalogTests(unittest.TestCase):
    def test_incremental_catalog_detects_replacement_and_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["radar-rain"]
            valid_time = dt.datetime(2026, 7, 22, 12, tzinfo=UTC)
            image = frame_path(root, domain, layer, valid_time)
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"frame")
            write_metadata(
                root,
                domain,
                layer,
                valid_time,
                image,
                extra={"revision": 1},
            )
            write_catalog(root)

            write_metadata(
                root,
                domain,
                layer,
                valid_time,
                image,
                extra={"revision": 2},
            )
            rebuilt = build_catalog(root)
            frames = rebuilt["domains"]["bc"]["layers"]["radar-rain"]["frames"]
            self.assertEqual(frames[0]["revision"], 2)

            metadata_path(root, domain, layer, valid_time).unlink()
            rebuilt = build_catalog(root)
            self.assertNotIn("radar-rain", rebuilt["domains"]["bc"]["layers"])

    def test_invalid_previous_catalog_falls_back_to_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["radar-rain"]
            valid_time = dt.datetime(2026, 7, 22, 12, tzinfo=UTC)
            image = frame_path(root, domain, layer, valid_time)
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"frame")
            write_metadata(root, domain, layer, valid_time, image)
            (root / "catalog.json").write_text("{not-json")

            rebuilt = build_catalog(root)
            frame = rebuilt["domains"]["bc"]["layers"]["radar-rain"]["frames"][0]
            self.assertEqual(
                json.loads(metadata_path(root, domain, layer, valid_time).read_text()),
                frame,
            )


if __name__ == "__main__":
    unittest.main()
