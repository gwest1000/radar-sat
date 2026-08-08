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
    def write_video_pointer(
        self,
        root: Path,
        *,
        manifest_path: str = (
            "video-manifests/bc-northeast-overlay/raw-visir/live/"
            "20260722T1200Z-abcdef012345.json"
        ),
        manifest_updates: dict[str, object] | None = None,
    ) -> None:
        generation = "20260722T1200Z-abcdef012345"
        manifest = root / manifest_path
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "schemaVersion": 1,
            "generation": generation,
            "productId": "bc-northeast-overlay",
            "layerId": "raw-visir",
            "track": "live",
        }
        payload.update(manifest_updates or {})
        manifest.write_text(json.dumps(payload))
        index = root / "video-index/bc-northeast-overlay/raw-visir.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({
            "schemaVersion": 1,
            "productId": "bc-northeast-overlay",
            "layerId": "raw-visir",
            "profiles": {
                "live": {
                    "generation": generation,
                    "manifestPath": manifest_path,
                }
            },
        }))

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

    def test_catalog_omits_metadata_for_a_missing_frame_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["radar-rain"]
            valid_time = dt.datetime(2026, 7, 22, 12, tzinfo=UTC)
            image = frame_path(root, domain, layer, valid_time)
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"frame")
            write_metadata(root, domain, layer, valid_time, image)
            write_catalog(root)

            image.unlink()
            rebuilt = build_catalog(root)

            self.assertNotIn("radar-rain", rebuilt["domains"]["bc"]["layers"])

    def test_catalog_falls_back_to_whole_frame_when_tile_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["ptype"]
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
                extra={
                    "tiles": {
                        "manifest": "tile-manifests/bc/ptype/missing.json",
                        "template": "tiles/bc/ptype/{z}/{x}/{y}.webp",
                    }
                },
            )

            rebuilt = build_catalog(root)
            frame = rebuilt["domains"]["bc"]["layers"]["ptype"]["frames"][0]

            self.assertNotIn("tiles", frame)
            self.assertEqual(frame["path"], image.relative_to(root).as_posix())

    def test_catalog_falls_back_when_tile_manifest_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["ptype"]
            valid_time = dt.datetime(2026, 7, 22, 12, tzinfo=UTC)
            image = frame_path(root, domain, layer, valid_time)
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"frame")
            manifest = root / "tile-manifests/bc/ptype/empty.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"files": []}))
            write_metadata(
                root,
                domain,
                layer,
                valid_time,
                image,
                extra={"tiles": {"manifest": manifest.relative_to(root).as_posix()}},
            )

            rebuilt = build_catalog(root)
            frame = rebuilt["domains"]["bc"]["layers"]["ptype"]["frames"][0]

            self.assertNotIn("tiles", frame)

    def test_static_layers_include_a_cache_busting_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boundary = root / "static" / "bc" / "boundaries.png"
            boundary.parent.mkdir(parents=True, exist_ok=True)
            boundary.write_bytes(b"boundary")

            rebuilt = build_catalog(root)
            entry = rebuilt["domains"]["bc"]["staticLayers"]["boundaries"]

            self.assertEqual(entry["path"], "static/bc/boundaries.png")
            self.assertTrue(entry["revision"].isdigit())

    def test_five_minute_catalog_uses_one_source_and_monotonic_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["raw-visir-5min"]
            frames = (
                (0, "NOAA/NESDIS/STAR", 0, 3840, 2944),
                (5, "NOAA GOES-18", 0, 1920, 1472),
                (10, "NOAA/NESDIS/STAR", -10, 3840, 2944),
                (15, "NOAA/NESDIS/STAR", 10, 3840, 2944),
            )
            base = dt.datetime(2026, 7, 22, 18, tzinfo=UTC)
            for minute, source, fallback_offset, width, height in frames:
                valid = base + dt.timedelta(minutes=minute)
                fallback = base + dt.timedelta(minutes=fallback_offset)
                image = frame_path(root, domain, layer, valid)
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b"frame")
                write_metadata(
                    root,
                    domain,
                    layer,
                    valid,
                    image,
                    source=source,
                    extra={
                        "fallbackSourceTime": fallback.isoformat(),
                        "starRenderVersion": 4,
                        "renderWidth": width,
                        "renderHeight": height,
                    },
                )

            rebuilt = build_catalog(root)
            published = rebuilt["domains"]["bc"]["layers"]["raw-visir-5min"][
                "frames"
            ]

            self.assertEqual(
                [frame["validTime"] for frame in published],
                [
                    "2026-07-22T18:00:00Z",
                    "2026-07-22T18:15:00Z",
                ],
            )
            self.assertTrue(
                all(frame["source"] == "NOAA/NESDIS/STAR" for frame in published)
            )

    def test_catalog_exposes_only_tiny_validated_video_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_video_pointer(root)

            catalog = build_catalog(root)

            self.assertEqual(
                catalog["videoProfiles"]["bc-northeast-overlay"]["raw-visir"]["live"],
                {
                    "generation": "20260722T1200Z-abcdef012345",
                    "manifestPath": (
                        "video-manifests/bc-northeast-overlay/raw-visir/live/"
                        "20260722T1200Z-abcdef012345.json"
                    ),
                },
            )
            self.assertNotIn("frames", json.dumps(catalog["videoProfiles"]))

    def test_catalog_omits_mismatched_or_unsafe_video_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_video_pointer(
                root,
                manifest_updates={"productId": "north-america-overlay"},
            )
            self.assertNotIn("videoProfiles", build_catalog(root))

            index = root / "video-index/bc-northeast-overlay/raw-visir.json"
            payload = json.loads(index.read_text())
            payload["profiles"]["live"]["manifestPath"] = "../outside.json"
            index.write_text(json.dumps(payload))
            self.assertNotIn("videoProfiles", build_catalog(root))


if __name__ == "__main__":
    unittest.main()
