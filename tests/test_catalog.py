from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from radarsat.catalog import build_catalog, write_catalog
from radarsat.config import (
    DOMAINS,
    LAYERS,
    VIEWPORTS,
    video_composite_kind,
    video_composite_layer_ids,
    video_composite_overlay_layer_ids,
)
from radarsat.pipeline import frame_path, metadata_path, write_metadata


UTC = dt.timezone.utc


class CatalogTests(unittest.TestCase):
    def write_video_pointer(
        self,
        root: Path,
        *,
        manifest_path: str = (
            "video-manifests/bc-northeast-overlay/eccc-geocolor/live/"
            "20260722T1200Z-abcdef012345.json"
        ),
        manifest_updates: dict[str, object] | None = None,
    ) -> None:
        generation = "20260722T1200Z-abcdef012345"
        manifest = root / manifest_path
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "schemaVersion": 2,
            "generation": generation,
            "productId": "bc-northeast-overlay",
            "layerId": "eccc-geocolor",
            "track": "live",
        }
        payload.update(manifest_updates or {})
        manifest.write_text(json.dumps(payload))
        index = root / "video-index/bc-northeast-overlay/eccc-geocolor.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({
            "schemaVersion": 2,
            "productId": "bc-northeast-overlay",
            "layerId": "eccc-geocolor",
            "profiles": {
                "live": {
                    "generation": generation,
                    "manifestPath": manifest_path,
                }
            },
        }))

    def write_composite_pointer(
        self,
        root: Path,
        *,
        preset_id: str = "operational-core-v1",
        generation: str = "20260722T1200Z-abcdef012345",
    ) -> None:
        product_id = "bc-northeast-overlay"
        layer_id = "eccc-geocolor"
        track = "live"
        range_hours = 3
        manifest_relative = (
            f"composite-manifests/{product_id}/{layer_id}/{track}/"
            f"{preset_id}/{range_hours}/{generation}.json"
        )
        layer_ids = list(
            video_composite_layer_ids(product_id, layer_id, preset_id)
        )
        composite_kind = video_composite_kind(product_id, preset_id)
        schema_version = 2 if composite_kind == "hybrid-prefix" else 1
        pointer = {
            "schemaVersion": schema_version,
            "productId": product_id,
            "layerId": layer_id,
            "track": track,
            "presetId": preset_id,
            "layerIds": layer_ids,
            "rangeHours": range_hours,
            "generation": generation,
            "manifestPath": manifest_relative,
            "generatedAt": "2026-07-22T12:02:00Z",
            "endValidTime": "2026-07-22T12:00:00Z",
            "endSourceTime": "2026-07-22T11:50:00Z",
        }
        if composite_kind == "hybrid-prefix":
            pointer.update({
                "compositeKind": "hybrid-prefix",
                "bakedLayerIds": layer_ids,
                "eligibleOverlayLayerIds": list(
                    video_composite_overlay_layer_ids(
                        product_id, layer_id, preset_id
                    )
                ),
            })
        manifest = root / manifest_relative
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(pointer))
        index = (
            root
            / "composite-index"
            / product_id
            / layer_id
            / track
            / preset_id
            / f"{range_hours}.json"
        )
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(pointer))

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

    def test_catalog_omits_explicitly_stale_regional_viewports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            layer = LAYERS["lightning-trail-region-south-coast"]
            base = dt.datetime(2026, 7, 22, 12, tzinfo=UTC)
            for index, viewport in enumerate((
                {**VIEWPORTS["south-coast"], "left": 0.51},
                VIEWPORTS["south-coast"],
            )):
                valid_time = base + dt.timedelta(minutes=index * 10)
                image = frame_path(root, domain, layer, valid_time)
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b"frame")
                write_metadata(
                    root,
                    domain,
                    layer,
                    valid_time,
                    image,
                    extra={"regionalViewport": viewport},
                )

            frames = build_catalog(root)["domains"]["bc"]["layers"][layer.id]["frames"]

            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0]["regionalViewport"], VIEWPORTS["south-coast"])

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
            regional_watershed = (
                root / "static" / "bc" / "bch-watersheds-region-south-coast.png"
            )
            regional_watershed.write_bytes(b"regional watershed")

            rebuilt = build_catalog(root)
            entry = rebuilt["domains"]["bc"]["staticLayers"]["boundaries"]
            regional_entry = rebuilt["domains"]["bc"]["staticLayers"][
                "watersheds-region-south-coast"
            ]

            self.assertEqual(entry["path"], "static/bc/boundaries.png")
            self.assertTrue(entry["revision"].isdigit())
            self.assertEqual(
                regional_entry["path"],
                "static/bc/bch-watersheds-region-south-coast.png",
            )
            self.assertTrue(regional_entry["revision"].isdigit())

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
                catalog["videoProfiles"]["bc-northeast-overlay"]["eccc-geocolor"]["live"],
                {
                    "generation": "20260722T1200Z-abcdef012345",
                    "manifestPath": (
                        "video-manifests/bc-northeast-overlay/eccc-geocolor/live/"
                        "20260722T1200Z-abcdef012345.json"
                    ),
                },
            )
            self.assertNotIn("frames", json.dumps(catalog["videoProfiles"]))

    def test_catalog_exposes_independent_composite_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_composite_pointer(root)

            catalog = build_catalog(root)
            values = catalog["compositeProfiles"]["bc-northeast-overlay"][
                "eccc-geocolor"
            ]["live"]

            self.assertEqual(len(values), 1)
            self.assertEqual(values[0]["presetId"], "operational-core-v1")
            self.assertEqual(values[0]["rangeHours"], 3)
            self.assertNotIn("frames", json.dumps(values))

    def test_complete_exact_pair_retires_bulky_live_hls_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_video_pointer(root)
            self.write_composite_pointer(root)
            self.write_composite_pointer(
                root,
                preset_id="operational-default-v1",
                generation="20260722T1200Z-fedcba543210",
            )

            catalog = build_catalog(root)

            self.assertNotIn("videoProfiles", catalog)
            pointers = catalog["compositeProfiles"]["bc-northeast-overlay"][
                "eccc-geocolor"
            ]["live"]
            self.assertEqual(
                {pointer["presetId"] for pointer in pointers},
                {"operational-default-v1", "operational-core-v1"},
            )

    def test_catalog_exposes_hybrid_contract_without_requiring_it_for_hls_retirement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_video_pointer(root)
            self.write_composite_pointer(root)
            self.write_composite_pointer(
                root,
                preset_id="operational-default-v1",
                generation="20260722T1200Z-fedcba543210",
            )
            self.write_composite_pointer(
                root,
                preset_id="weather-smoke-core-v1",
                generation="20260722T1200Z-012345abcdef",
            )

            catalog = build_catalog(root)

            self.assertNotIn("videoProfiles", catalog)
            pointers = catalog["compositeProfiles"]["bc-northeast-overlay"][
                "eccc-geocolor"
            ]["live"]
            hybrid = next(
                value
                for value in pointers
                if value["presetId"] == "weather-smoke-core-v1"
            )
            self.assertEqual(hybrid["compositeKind"], "hybrid-prefix")
            self.assertEqual(hybrid["bakedLayerIds"], hybrid["layerIds"])
            self.assertEqual(
                hybrid["eligibleOverlayLayerIds"],
                ["lightning-trail", "hotspots", "model-mslp", "model-hgt500"],
            )

    def test_catalog_rejects_noncanonical_hybrid_overlay_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_composite_pointer(root, preset_id="weather-smoke-core-v1")
            index = next((root / "composite-index").rglob("*.json"))
            pointer = json.loads(index.read_text())
            pointer["eligibleOverlayLayerIds"] = ["hotspots"]
            index.write_text(json.dumps(pointer))

            self.assertNotIn("compositeProfiles", build_catalog(root))

    def test_catalog_omits_mismatched_composite_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_composite_pointer(root)
            index = next((root / "composite-index").rglob("*.json"))
            payload = json.loads(index.read_text())
            payload["endSourceTime"] = "not-a-time"
            index.write_text(json.dumps(payload))

            self.assertNotIn("compositeProfiles", build_catalog(root))

    def test_catalog_omits_valid_preset_with_noncanonical_layer_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_composite_pointer(root)
            index = next((root / "composite-index").rglob("*.json"))
            pointer = json.loads(index.read_text())
            pointer["layerIds"].remove("model-hgt500")
            index.write_text(json.dumps(pointer))
            manifest = root / pointer["manifestPath"]
            payload = json.loads(manifest.read_text())
            payload["layerIds"] = pointer["layerIds"]
            manifest.write_text(json.dumps(payload))

            self.assertNotIn("compositeProfiles", build_catalog(root))

    def test_catalog_omits_legacy_default_video_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_video_pointer(root)
            index = root / "video-index/bc-northeast-overlay/eccc-geocolor.json"
            index_payload = json.loads(index.read_text())
            index_payload["schemaVersion"] = 1
            index.write_text(json.dumps(index_payload))
            manifest = root / index_payload["profiles"]["live"]["manifestPath"]
            manifest_payload = json.loads(manifest.read_text())
            manifest_payload["schemaVersion"] = 1
            manifest.write_text(json.dumps(manifest_payload))

            self.assertNotIn("videoProfiles", build_catalog(root))

    def test_catalog_omits_obsolete_secondary_video_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = "20260722T1200Z-abcdef012345"
            manifest_path = (
                "video-manifests/bc-northeast-overlay/raw-ir/live/"
                f"{generation}.json"
            )
            manifest = root / manifest_path
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "generation": generation,
                "productId": "bc-northeast-overlay",
                "layerId": "raw-ir",
                "track": "live",
            }))
            index = root / "video-index/bc-northeast-overlay/raw-ir.json"
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({
                "schemaVersion": 1,
                "productId": "bc-northeast-overlay",
                "layerId": "raw-ir",
                "profiles": {"live": {
                    "generation": generation,
                    "manifestPath": manifest_path,
                }},
            }))

            self.assertNotIn("videoProfiles", build_catalog(root))

    def test_catalog_omits_track_not_offered_by_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = "20260722T1200Z-abcdef012345"
            manifest_path = (
                "video-manifests/bc-northeast-overlay/eccc-geocolor/archive/"
                f"{generation}.json"
            )
            self.write_video_pointer(
                root,
                manifest_path=manifest_path,
                manifest_updates={"track": "archive"},
            )
            index = root / "video-index/bc-northeast-overlay/eccc-geocolor.json"
            payload = json.loads(index.read_text())
            payload["profiles"] = {"archive": payload["profiles"]["live"]}
            index.write_text(json.dumps(payload))

            self.assertNotIn("videoProfiles", build_catalog(root))

    def test_catalog_omits_mismatched_or_unsafe_video_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_video_pointer(
                root,
                manifest_updates={"productId": "north-america-overlay"},
            )
            self.assertNotIn("videoProfiles", build_catalog(root))

            index = root / "video-index/bc-northeast-overlay/eccc-geocolor.json"
            payload = json.loads(index.read_text())
            payload["profiles"]["live"]["manifestPath"] = "../outside.json"
            index.write_text(json.dumps(payload))
            self.assertNotIn("videoProfiles", build_catalog(root))


if __name__ == "__main__":
    unittest.main()
