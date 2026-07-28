from __future__ import annotations

import unittest

from radarsat.westwx_catalog import build_westwx_catalog


class WestwxCatalogTests(unittest.TestCase):
    def test_keeps_only_client_layers_and_fields(self) -> None:
        catalog = {
            "schemaVersion": 1,
            "generatedAt": "2026-07-28T18:00:00Z",
            "products": {"unused": True},
            "domains": {
                "north-america": {
                    "layers": {
                        "westwx-visir": {
                            "title": "Satellite",
                            "frames": [{
                                "validTime": "2026-07-28T18:00:00Z",
                                "path": "frames/north-america/westwx-visir/frame.webp",
                                "source": "NOAA",
                                "tiles": {
                                    "template": "tiles/example/{z}/{x}/{y}.webp",
                                    "bounds": [-180, 5, -50, 75],
                                    "minZoom": 2,
                                    "maxZoom": 4,
                                    "tileSize": 512,
                                    "format": "webp",
                                    "encoding": "lossy-webp",
                                    "manifest": "tile-manifests/private.json",
                                    "bytes": 123,
                                },
                            }],
                        },
                        "convective": {"frames": [{"validTime": "unused", "path": "unused"}]},
                    },
                },
                "bc": {"layers": {}},
                "north-pacific": {"layers": {"radar-rain": {"frames": []}}},
            },
        }

        compact = build_westwx_catalog(catalog)

        self.assertEqual(set(compact["domains"]), {"north-america", "bc"})
        layer = compact["domains"]["north-america"]["layers"]["westwx-visir"]
        self.assertEqual(len(layer["frames"]), 1)
        frame = layer["frames"][0]
        self.assertNotIn("source", frame)
        self.assertNotIn("manifest", frame["tiles"])
        self.assertNotIn("bytes", frame["tiles"])
        self.assertNotIn("convective", compact["domains"]["north-america"]["layers"])
        self.assertNotIn("products", compact)


if __name__ == "__main__":
    unittest.main()
