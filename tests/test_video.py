from __future__ import annotations

import datetime as dt
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

import radarsat.video as video_module

from radarsat.composite_video import (
    _derive_rendition,
    _render_high_frame,
    build_composite_profile,
    prune_composite_frame_cache,
    prune_composite_sidecar_manifests,
)
from radarsat.catalog import build_catalog
from radarsat.config import (
    PRODUCTS,
    VIDEO_COMPOSITE_PRESETS,
    VIDEO_TRACKS_BY_PRODUCT,
    VIEWPORTS,
    video_composite_kind,
    video_composite_layer_ids,
    video_composite_overlay_layer_ids,
)
from radarsat.video import (
    COMPOSITE_VIDEO_CRF,
    ProfileSpec,
    SelectedFrame,
    VIDEO_FRAME_RATE,
    VIDEO_PROFILES,
    _exact_renditions,
    _render_proxy,
    _proxy_selections,
    _selected_satellite_frames,
    _update_profile_index,
    build_profile,
    build_satellite_videos,
    prune_local_video_orphans,
    prune_shared_video_orphans,
)


UTC = dt.timezone.utc


def stamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def frame(relative: str, valid: dt.datetime, *, fetched: str = "2026-08-01T01:00:00Z") -> dict[str, object]:
    return {
        "validTime": stamp(valid),
        "path": relative,
        "fetchedAt": fetched,
        "sourceTimes": {"GOES-18": stamp(valid)},
    }


def write_rgba(path: Path, colour: tuple[int, int, int, int], size: tuple[int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, colour).save(path)


class VideoSelectionTests(unittest.TestCase):
    def test_bc_video_timeline_remains_on_regular_satellite_slots(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        satellite = [
            frame(f"frames/bc/eccc-geocolor/{minute}.png", base + dt.timedelta(minutes=minute))
            for minute in range(0, 61, 10)
        ]
        radar = [
            frame(f"frames/bc/radar-rain/{minute}.png", base + dt.timedelta(minutes=minute))
            for minute in range(0, 61, 6)
        ]
        catalog = {
            "domains": {
                "bc": {
                    "layers": {
                        "eccc-geocolor": {"maxAgeMinutes": 35, "frames": satellite},
                        "radar-rain": {"maxAgeMinutes": 20, "frames": radar},
                    }
                }
            }
        }
        spec = ProfileSpec(
            "bc-large-overlay",
            "bc",
            "eccc-geocolor",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            10,
        )
        selected = _selected_satellite_frames(catalog, spec, 1, now=base + dt.timedelta(hours=1))
        self.assertEqual(
            [int((item.valid_time - base).total_seconds() // 60) for item in selected],
            list(range(0, 61, 10)),
        )
        self.assertEqual(selected[-1].source_valid_time, base + dt.timedelta(hours=1))
        self.assertEqual(
            [value["id"] for value in VIDEO_COMPOSITE_PRESETS["bc-south-coast-overlay"]],
            ["operational-default-v1"],
        )
        self.assertEqual(COMPOSITE_VIDEO_CRF, 20)

    def test_regional_radar_accepts_scan_seconds_after_nominal_slot(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        viewport = VIEWPORTS["south-coast"]
        old = frame(
            "frames/bc/radar-rain-region-south-coast/0031.png",
            base + dt.timedelta(minutes=31, seconds=9),
        )
        current = frame(
            "frames/bc/radar-rain-region-south-coast/0036.png",
            base + dt.timedelta(minutes=36, seconds=54),
        )
        for value in (old, current):
            value["regionalViewport"] = viewport
        catalog = {
            "domains": {
                "bc": {
                    "staticLayers": {},
                    "layers": {
                        "radar-rain-region-south-coast": {
                            "maxAgeMinutes": 12,
                            "frames": [old, current],
                        }
                    },
                }
            }
        }
        spec = ProfileSpec(
            "bc-south-coast-overlay",
            "bc",
            "eccc-geocolor",
            viewport,
            64,
            48,
            6,
        )
        selected = SelectedFrame(
            valid_time=base + dt.timedelta(minutes=36),
            source_valid_time=base + dt.timedelta(minutes=30),
            source_times={},
            encoded_source_layer="eccc-geocolor",
            source_path="frames/bc/eccc-geocolor/0030.png",
            source_fetched_at="2026-08-01T00:31:00Z",
        )

        selections = _proxy_selections(catalog, spec, [selected])[0]
        radar = next(value for value in selections if value.recipe_id == "radar-rain")

        self.assertEqual(radar.source_path, current["path"])
        self.assertEqual(
            radar.source_valid_time,
            base + dt.timedelta(minutes=36, seconds=54),
        )

    def test_profiles_follow_the_operational_range_matrix(self) -> None:
        by_product_layer: dict[tuple[str, str], dict[str, int]] = {}
        for spec in VIDEO_PROFILES:
            by_product_layer.setdefault((spec.product_id, spec.layer_id), {})[
                spec.track
            ] = spec.cadence_minutes

        self.assertTrue(by_product_layer)
        for (product_id, _layer_id), tracks in by_product_layer.items():
            self.assertEqual(set(tracks), set(VIDEO_TRACKS_BY_PRODUCT[product_id]))
            self.assertIn("live", tracks)
            if "day" in tracks:
                self.assertEqual(tracks["day"], 30)
            if "archive" in tracks:
                self.assertEqual(tracks["archive"], 60)

        specs = {
            (spec.product_id, spec.layer_id, spec.track): spec
            for spec in VIDEO_PROFILES
        }
        for product_id, layer_id in by_product_layer:
            live = specs[(product_id, layer_id, "live")]
            if "day" in VIDEO_TRACKS_BY_PRODUCT[product_id]:
                day = specs[(product_id, layer_id, "day")]
                self.assertEqual(day.media_group, live.media_group)
                self.assertEqual(day.resolved_media_viewport, live.resolved_media_viewport)
                self.assertEqual(day.resolved_media_width, live.resolved_media_width)
                self.assertEqual(day.resolved_media_height, live.resolved_media_height)

    def test_track_index_updates_preserve_other_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "video-index/product/layer.json"
            base = next(spec for spec in VIDEO_PROFILES if spec.track == "live")
            live = replace(base, product_id="product", layer_id="layer")
            day = replace(live, cadence_minutes=30, track_name="day")
            _update_profile_index(
                path,
                live,
                "2026-08-15T18:00:00Z",
                {"generation": "live", "manifestPath": "live.json"},
            )
            _update_profile_index(
                path,
                day,
                "2026-08-15T18:30:00Z",
                {"generation": "day", "manifestPath": "day.json"},
            )

            payload = json.loads(path.read_text())
            self.assertEqual(set(payload["profiles"]), {"live", "day"})
            self.assertEqual(payload["profiles"]["live"]["generation"], "live")
            self.assertEqual(payload["profiles"]["day"]["generation"], "day")

    def test_ne_bc_prefers_recency_and_upgrades_same_slot_to_native(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        standard = [
            frame(f"frames/bc/raw-visir/{minute}.webp", base + dt.timedelta(minutes=minute))
            for minute in range(0, 51, 10)
        ]
        native = [
            frame("frames/bc/raw-visir-native/10.webp", base + dt.timedelta(minutes=10))
        ]
        catalog = {
            "domains": {
                "bc": {
                    "layers": {
                        "raw-visir": {"frames": standard},
                        "raw-visir-native": {"frames": native},
                    }
                }
            }
        }
        spec = ProfileSpec(
            "bc-northeast-overlay",
            "bc",
            "raw-visir",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            10,
        )

        selected = _selected_satellite_frames(catalog, spec, 1)

        self.assertEqual(
            [item.encoded_source_layer for item in selected],
            [
                "raw-visir",
                "raw-visir-native",
                "raw-visir",
                "raw-visir",
                "raw-visir",
                "raw-visir",
            ],
        )
        self.assertEqual([item.valid_time.minute for item in selected], [0, 10, 20, 30, 40, 50])
        self.assertEqual(selected[3].source_valid_time, base + dt.timedelta(minutes=30))

    def test_bc_msc_primary_uses_only_same_slot_noaa_after_35_minute_deadline(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        msc = [
            frame(f"frames/bc/eccc-geocolor/{minute}.webp", base + dt.timedelta(minutes=minute))
            for minute in (0, 10)
        ]
        standard = [
            frame(f"frames/bc/raw-visir/{minute}.webp", base + dt.timedelta(minutes=minute))
            for minute in (20, 30, 40)
        ]
        native = [
            frame("frames/bc/raw-visir-native/20.webp", base + dt.timedelta(minutes=20))
        ]
        catalog = {
            "domains": {
                "bc": {
                    "layers": {
                        "eccc-geocolor": {"maxAgeMinutes": 35, "frames": msc},
                        "raw-visir": {"frames": standard},
                        "raw-visir-native": {"frames": native},
                    }
                }
            }
        }
        spec = ProfileSpec(
            "bc-northeast-overlay",
            "bc",
            "eccc-geocolor",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            10,
        )

        inside_deadline = _selected_satellite_frames(
            catalog,
            spec,
            1,
            now=base + dt.timedelta(minutes=54),
        )
        self.assertEqual(
            [item.encoded_source_layer for item in inside_deadline],
            ["eccc-geocolor", "eccc-geocolor"],
        )

        expired = _selected_satellite_frames(
            catalog,
            spec,
            1,
            now=base + dt.timedelta(minutes=56),
        )
        self.assertEqual(
            [item.encoded_source_layer for item in expired],
            ["eccc-geocolor", "eccc-geocolor", "raw-visir-native"],
        )
        self.assertEqual(expired[-1].valid_time, base + dt.timedelta(minutes=20))

        # A later MSC arrival replaces the exceptional NOAA fill on rebuild.
        msc.append(
            frame("frames/bc/eccc-geocolor/20.webp", base + dt.timedelta(minutes=20))
        )
        upgraded = _selected_satellite_frames(
            catalog,
            spec,
            1,
            now=base + dt.timedelta(minutes=56),
        )
        self.assertEqual(upgraded[-1].encoded_source_layer, "eccc-geocolor")

        # A genuinely missing slot is not manufactured by holding the prior
        # MSC image under a later display timestamp.
        msc.append(
            frame("frames/bc/eccc-geocolor/40.webp", base + dt.timedelta(minutes=40))
        )
        standard.clear()
        native.clear()
        with_gap = _selected_satellite_frames(
            catalog,
            spec,
            1,
            now=base + dt.timedelta(minutes=80),
        )
        self.assertEqual(
            [item.valid_time for item in with_gap],
            [base, base + dt.timedelta(minutes=10), base + dt.timedelta(minutes=20), base + dt.timedelta(minutes=40)],
        )
        self.assertEqual(with_gap[2].encoded_source_layer, "eccc-geocolor")

    def test_bc_msc_range_ignores_noaa_slots_that_are_not_yet_eligible(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        msc = [
            frame(
                f"frames/bc/eccc-geocolor/{minute}.webp",
                base + dt.timedelta(minutes=minute, seconds=21),
            )
            for minute in range(0, 61, 10)
        ]
        standard = [
            frame(
                "frames/bc/raw-visir/70.webp",
                base + dt.timedelta(minutes=70, seconds=21),
            )
        ]
        catalog = {
            "domains": {
                "bc": {
                    "layers": {
                        "eccc-geocolor": {"maxAgeMinutes": 35, "frames": msc},
                        "raw-visir": {"frames": standard},
                        "raw-visir-native": {"frames": []},
                    }
                }
            }
        }
        spec = ProfileSpec(
            "bc-northeast-overlay",
            "bc",
            "eccc-geocolor",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            10,
        )

        selected = _selected_satellite_frames(
            catalog,
            spec,
            1,
            now=base + dt.timedelta(minutes=75),
        )

        self.assertEqual(len(selected), 7)
        self.assertEqual(selected[0].valid_time, base)
        self.assertEqual(selected[-1].valid_time, base + dt.timedelta(minutes=60))
        self.assertEqual(
            selected[-1].valid_time - selected[0].valid_time,
            dt.timedelta(hours=1),
        )
        self.assertTrue(
            all(item.encoded_source_layer == "eccc-geocolor" for item in selected)
        )

    def test_broad_selection_honours_max_age_and_never_regresses(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        broad_frames = [
            frame("frames/north-america/westwx-visir/00.webp", base),
            frame(
                "frames/north-america/westwx-visir/20.webp",
                base + dt.timedelta(minutes=20),
            ),
            frame(
                "frames/north-america/westwx-visir/40.webp",
                base + dt.timedelta(minutes=40),
            ),
            frame(
                "frames/north-america/westwx-visir/100.webp",
                base + dt.timedelta(minutes=100),
            ),
        ]
        broad_frames[1]["sourceTimes"] = {
            "GOES-18": stamp(base - dt.timedelta(minutes=10))
        }
        catalog = {
            "domains": {
                "north-america": {
                    "layers": {
                        "westwx-visir": {
                            "maxAgeMinutes": 30,
                            "frames": broad_frames,
                        }
                    }
                }
            }
        }
        spec = ProfileSpec(
            "north-america-overlay",
            "north-america",
            "westwx-visir",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            20,
        )

        selected = _selected_satellite_frames(catalog, spec, 2)

        self.assertEqual(
            [item.valid_time for item in selected],
            [
                base,
                base + dt.timedelta(minutes=20),
                base + dt.timedelta(minutes=40),
                base + dt.timedelta(minutes=60),
                base + dt.timedelta(minutes=100),
            ],
        )
        self.assertEqual(
            [item.source_valid_time for item in selected],
            [
                base,
                base,
                base + dt.timedelta(minutes=40),
                base + dt.timedelta(minutes=40),
                base + dt.timedelta(minutes=100),
            ],
        )
        self.assertEqual(selected[1].source_path, selected[0].source_path)

    def test_broad_archive_accepts_scan_seconds_after_nominal_hour(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        frames = [
            frame(
                f"frames/north-america/westwx-visir/{hour}.webp",
                base + dt.timedelta(hours=hour, seconds=22),
            )
            for hour in (0, 1, 4)
        ]
        catalog = {
            "domains": {
                "north-america": {
                    "layers": {
                        "westwx-visir": {
                            "maxAgeMinutes": 30,
                            "frames": frames,
                        }
                    }
                }
            }
        }
        spec = ProfileSpec(
            "north-america-overlay",
            "north-america",
            "westwx-visir",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            60,
            track_name="archive",
        )

        selected = _selected_satellite_frames(catalog, spec, 6)

        self.assertEqual(
            [item.valid_time for item in selected],
            [
                base,
                base + dt.timedelta(hours=1),
                base + dt.timedelta(hours=2),
                base + dt.timedelta(hours=3),
                base + dt.timedelta(hours=4),
            ],
        )
        self.assertEqual(
            [item.source_valid_time for item in selected],
            [
                base + dt.timedelta(seconds=22),
                base + dt.timedelta(hours=1, seconds=22),
                base + dt.timedelta(hours=1, seconds=22),
                base + dt.timedelta(hours=1, seconds=22),
                base + dt.timedelta(hours=4, seconds=22),
            ],
        )

    def test_ne_bc_uses_nonregressing_standard_when_native_regresses(self) -> None:
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        standard = [
            frame(
                f"frames/bc/raw-visir/{minute}.webp",
                base + dt.timedelta(minutes=minute),
            )
            for minute in (0, 10, 20)
        ]
        native = [
            frame(
                "frames/bc/raw-visir-native/10.webp",
                base + dt.timedelta(minutes=10),
            ),
            frame(
                "frames/bc/raw-visir-native/20.webp",
                base + dt.timedelta(minutes=20),
            ),
        ]
        native[1]["sourceTimes"] = {
            "GOES-18": stamp(base + dt.timedelta(minutes=5))
        }
        catalog = {
            "domains": {
                "bc": {
                    "layers": {
                        "raw-visir": {"frames": standard},
                        "raw-visir-native": {"frames": native},
                    }
                }
            }
        }
        spec = ProfileSpec(
            "bc-northeast-overlay",
            "bc",
            "raw-visir",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            10,
        )

        selected = _selected_satellite_frames(catalog, spec, 1)

        self.assertEqual(
            [item.encoded_source_layer for item in selected],
            ["raw-visir", "raw-visir-native", "raw-visir"],
        )
        self.assertEqual(
            [item.source_valid_time for item in selected],
            [
                base,
                base + dt.timedelta(minutes=10),
                base + dt.timedelta(minutes=20),
            ],
        )

    def test_proxy_is_exact_stage_size_and_preserves_transparency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "static" / "bc" / "boundaries.png"
            source.parent.mkdir(parents=True)
            image = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.line((20, 10, 80, 70), fill=(255, 255, 255, 255), width=2)
            image.save(source)
            spec = ProfileSpec(
                "bc-northeast-overlay",
                "bc",
                "raw-visir",
                {"left": 0.1, "top": 0.1, "width": 0.8, "height": 0.8},
                60,
                40,
                10,
            )

            entry = _render_proxy(
                root / "source",
                root / "output",
                spec,
                "boundaries",
                "static/bc/boundaries.png?v=1",
                "static/bc/boundaries.png",
                stage_aligned=False,
            )

            proxy = root / "output" / str(entry["path"])
            with Image.open(proxy) as rendered:
                self.assertEqual(rendered.size, (60, 40))
                self.assertEqual(rendered.mode, "RGBA")
                self.assertEqual(rendered.getchannel("A").getextrema(), (0, 255))
            self.assertEqual(entry["byteLength"], proxy.stat().st_size)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class VideoBuildTests(unittest.TestCase):
    def test_composite_cache_prunes_oldest_files_to_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "composite-frame-cache" / "product"
            cache.mkdir(parents=True)
            now = dt.datetime(2026, 9, 3, 22, tzinfo=UTC)
            paths = [cache / f"{index}.png" for index in range(3)]
            for index, path in enumerate(paths):
                path.write_bytes(b"x" * 4)
                stamp = (now - dt.timedelta(minutes=30 - index)).timestamp()
                os.utime(path, (stamp, stamp))

            removed = prune_composite_frame_cache(
                root,
                max_age_hours=2,
                max_bytes=8,
                now=now,
            )

            self.assertEqual(removed, 1)
            self.assertFalse(paths[0].exists())
            self.assertTrue(paths[1].exists())
            self.assertTrue(paths[2].exists())

    def test_composite_renditions_are_high_only_for_bc_and_display_only_for_broad(self) -> None:
        bc = ProfileSpec(
            "bc-large-overlay",
            "bc",
            "eccc-geocolor",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            1920,
            1472,
            10,
        )
        broad = ProfileSpec(
            "north-america-overlay",
            "north-america",
            "westwx-visir",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            1200,
            816,
            20,
        )
        self.assertEqual(_exact_renditions(bc), (("high", 1920, 1472),))
        self.assertEqual(_exact_renditions(broad), (("display", 1200, 816),))

    def test_operational_default_presets_match_fresh_session_controls(self) -> None:
        products = {str(product["id"]): product for product in PRODUCTS}
        for product_id, presets in VIDEO_COMPOSITE_PRESETS.items():
            product = products[product_id]
            expected = {
                str(layer["id"])
                for layer in product["layers"]
                if layer.get("optional")
                and layer.get("defaultEnabled")
                and not layer.get("enabledWith")
                and layer.get("choiceGroup") != "satellite"
            }
            self.assertEqual(presets[0]["id"], "operational-default-v1")
            self.assertEqual(set(presets[0]["optionalLayers"]), expected)
            self.assertEqual(
                len(presets),
                3 if product_id in {
                    "bc-large-overlay",
                    "bc-northeast-overlay",
                    "north-america-overlay",
                }
                else 1,
            )

    def test_hybrid_core_pilots_are_strict_recipe_prefixes(self) -> None:
        products = {str(product["id"]): product for product in PRODUCTS}
        for product_id, satellite_layer_id in (
            ("bc-large-overlay", "eccc-geocolor"),
            ("bc-northeast-overlay", "eccc-geocolor"),
            ("north-america-overlay", "westwx-visir"),
        ):
            for preset_id, expects_smoke in (
                ("weather-smoke-core-v1", True),
                ("weather-core-v1", False),
            ):
                self.assertEqual(
                    video_composite_kind(product_id, preset_id),
                    "hybrid-prefix",
                )
                baked = video_composite_layer_ids(
                    product_id,
                    satellite_layer_id,
                    preset_id,
                )
                upper = video_composite_overlay_layer_ids(
                    product_id,
                    satellite_layer_id,
                    preset_id,
                )
                recipe_order = [
                    str(recipe["id"])
                    for recipe in products[product_id]["layers"]
                    if str(recipe["id"]) in {*baked, *upper}
                ]
                self.assertEqual(list((*baked, *upper)), recipe_order)
                self.assertEqual("smoke" in baked, expects_smoke)
                self.assertIn("radar-rain", baked)
                self.assertEqual(upper[-2:], ("model-mslp", "model-hgt500"))

    def make_source(self, root: Path) -> tuple[dict[str, object], ProfileSpec]:
        source = root / "source"
        write_rgba(source / "static/bc/base-dark.png", (18, 28, 38, 255), (64, 48))
        boundary = source / "static/bc/boundaries.png"
        write_rgba(boundary, (0, 0, 0, 0), (64, 48))
        with Image.open(boundary) as boundary_image:
            draw = ImageDraw.Draw(boundary_image)
            draw.line((0, 0, 63, 47), fill=(255, 255, 255, 255), width=1)
            boundary_image.save(boundary)
        regional_watershed = source / "static/bc/bch-watersheds-region-northeast.png"
        write_rgba(regional_watershed, (0, 0, 0, 0), (64, 48))
        with Image.open(regional_watershed) as watershed_image:
            draw = ImageDraw.Draw(watershed_image)
            draw.line((0, 24, 63, 24), fill=(114, 217, 255, 255), width=2)
            watershed_image.save(regional_watershed)
        base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
        frames: list[dict[str, object]] = []
        for index, minute in enumerate((0, 10, 20)):
            relative = f"frames/bc/raw-visir/{minute}.webp"
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((4 + index * 5, 8, 28 + index * 5, 32), fill=(95, 145, 205, 230))
            image.save(path, "WEBP", lossless=True)
            frames.append(frame(relative, base + dt.timedelta(minutes=minute)))
        catalog: dict[str, object] = {
            "domains": {
                "bc": {
                    "layers": {"raw-visir": {"maxAgeMinutes": 90, "frames": frames}},
                    "staticLayers": {
                        "base-dark": {"path": "static/bc/base-dark.png", "revision": "1"},
                        "watersheds-region-northeast": {
                            "path": "static/bc/bch-watersheds-region-northeast.png",
                            "revision": "1",
                        },
                        "boundaries": {"path": "static/bc/boundaries.png", "revision": "1"},
                    },
                }
            }
        }
        spec = ProfileSpec(
            "bc-northeast-overlay",
            "bc",
            "raw-visir",
            {"left": 0.0, "top": 0.0, "width": 1.0, "height": 1.0},
            64,
            48,
            10,
            crf=18,
            preset="ultrafast",
        )
        return catalog, spec

    def test_segmented_h264_manifest_pts_and_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            output = root / "output"
            now = dt.datetime(2026, 8, 1, 1, tzinfo=UTC)

            result = build_profile(
                root / "source",
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
                now=now,
            )

            self.assertEqual(result["status"], "built")
            manifest = json.loads((output / str(result["manifestPath"])).read_text())
            media = output / manifest["media"]["path"]
            self.assertEqual([item["ptsSeconds"] for item in manifest["frames"]], [0.0, 0.2, 0.4])
            self.assertEqual([item["durationSeconds"] for item in manifest["frames"]], [0.2, 0.2, 0.2])
            self.assertEqual(manifest["transport"], "hls-ts")
            self.assertEqual(manifest["media"]["mimeType"], "application/vnd.apple.mpegurl")
            self.assertEqual(manifest["media"]["width"], 64)
            self.assertEqual(manifest["media"]["height"], 64)
            self.assertEqual(manifest["media"]["contentHeight"], 48)
            self.assertEqual(manifest["media"]["frameRate"], VIDEO_FRAME_RATE)
            self.assertIn("static/bc/boundaries.png?v=1", manifest["proxies"])
            self.assertIn(
                "static/bc/bch-watersheds-region-northeast.png?v=1",
                manifest["proxies"],
            )
            self.assertEqual(
                [layer["id"] for layer in manifest["frames"][0]["proxyLayers"]],
                ["watersheds", "boundaries"],
            )
            self.assertEqual(
                manifest["frames"][0]["proxyLayers"][0]["renderId"],
                "watersheds-region-northeast",
            )
            self.assertIsNone(
                manifest["frames"][0]["proxyLayers"][0]["sourceValidTime"]
            )
            self.assertTrue(media.is_file())
            self.assertIn("#EXT-X-ENDLIST", media.read_text())
            self.assertEqual(len(manifest["media"]["segments"]), 1)
            segment = output / manifest["media"]["segments"][0]["path"]
            self.assertTrue(segment.is_file())

            probe = subprocess.run(
                [
                    str(shutil.which("ffprobe")),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,pix_fmt,color_space,color_transfer,color_primaries,nb_frames",
                    "-of",
                    "json",
                    str(segment),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "h264")
            self.assertEqual(stream["pix_fmt"], "yuv420p")
            self.assertEqual(stream["color_space"], "bt709")
            self.assertEqual(stream["color_transfer"], "bt709")
            self.assertEqual(stream["color_primaries"], "bt709")

            frame_probe = subprocess.run(
                [
                    str(shutil.which("ffprobe")),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "frame=best_effort_timestamp_time,duration_time,pict_type",
                    "-of",
                    "json",
                    str(segment),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            encoded_frames = json.loads(frame_probe.stdout)["frames"]
            self.assertEqual(len(encoded_frames), 3)
            self.assertNotIn("B", {item.get("pict_type") for item in encoded_frames})
            timestamps = [
                float(item["best_effort_timestamp_time"])
                for item in encoded_frames
            ]
            self.assertEqual(
                [round(right - left, 2) for left, right in zip(timestamps, timestamps[1:])],
                [0.2] * 2,
            )

            unchanged = build_profile(
                root / "source",
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
                now=now + dt.timedelta(minutes=1),
            )
            self.assertEqual(unchanged["status"], "unchanged")

    def test_configured_composites_build_shared_segment_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, original_spec = self.make_source(root)
            layers = catalog["domains"]["bc"]["layers"]
            layers["eccc-geocolor"] = layers.pop("raw-visir")
            spec = replace(
                original_spec,
                product_id="bc-large-overlay",
                layer_id="eccc-geocolor",
            )
            output = root / "output"

            first = build_profile(
                root / "source",
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )
            first_manifest = json.loads(
                (output / str(first["manifestPath"])).read_text()
            )
            self.assertEqual(first_manifest["schemaVersion"], 2)
            self.assertEqual(
                [item["id"] for item in first_manifest["composites"]],
                ["operational-default-v1"],
            )
            default_composite = first_manifest["composites"][0]
            self.assertEqual(
                default_composite["layerIds"],
                [
                    "base-dark",
                    "eccc-geocolor",
                    "smoke",
                    "radar-coverage",
                    "radar-rain",
                    "watersheds",
                    "transmission-lines",
                    "boundaries",
                    "lightning-trail",
                    "hotspots",
                    "model-mslp",
                    "model-hgt500",
                ],
            )
            composite = first_manifest["composites"][0]
            self.assertEqual(
                composite["layerIds"],
                [
                    "base-dark",
                    "eccc-geocolor",
                    "smoke",
                    "radar-coverage",
                    "radar-rain",
                    "watersheds",
                    "transmission-lines",
                    "boundaries",
                    "lightning-trail",
                    "hotspots",
                    "model-mslp",
                    "model-hgt500",
                ],
            )
            self.assertEqual(composite["mediaViewport"], spec.viewport)
            self.assertEqual([item["hours"] for item in composite["ranges"]], [3, 6, 12])
            exact_range = composite["ranges"][0]
            rendition = exact_range["renditions"][0]
            self.assertEqual(rendition["id"], "high")
            self.assertEqual(rendition["media"]["width"], 64)
            self.assertEqual(rendition["media"]["contentHeight"], 48)
            self.assertEqual(exact_range["durationsSeconds"], [0.2, 0.2, 0.8])
            self.assertEqual(exact_range["boundaryIntervalMultiplier"], 4)
            self.assertIn(
                "videos/composite-bc-large-overlay-operational-default-v1/eccc-geocolor/day/",
                rendition["media"]["path"],
            )
            self.assertEqual(
                rendition["media"]["mimeType"],
                "application/vnd.apple.mpegurl",
            )
            exact_media = output / rendition["media"]["path"]
            self.assertTrue(exact_media.is_file())
            self.assertTrue(rendition["media"]["segments"])
            self.assertTrue(
                all((output / item["path"]).is_file() for item in rendition["media"]["segments"])
            )
            segment_sets = [
                {item["path"] for item in value["renditions"][0]["media"]["segments"]}
                for value in composite["ranges"]
            ]
            self.assertTrue(all(value == segment_sets[0] for value in segment_sets[1:]))
            self.assertGreater(first["compositeMediaBytes"], 0)

            # A new satellite anchor plus a static-overlay content change must
            # bind the next immutable exact HLS generation to the new proxy.
            boundary = root / "source/static/bc/boundaries.png"
            write_rgba(boundary, (255, 0, 0, 255), (64, 48))
            stat = boundary.stat()
            os.utime(boundary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            new_relative = "frames/bc/raw-visir/30.webp"
            shutil.copy2(root / "source/frames/bc/raw-visir/20.webp", root / "source" / new_relative)
            layers["eccc-geocolor"]["frames"].append(
                frame(new_relative, dt.datetime(2026, 8, 1, 0, 30, tzinfo=UTC))
            )
            second = build_profile(
                root / "source",
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )
            second_manifest = json.loads(
                (output / str(second["manifestPath"])).read_text()
            )
            self.assertNotEqual(second["generation"], first["generation"])
            second_media = second_manifest["composites"][0]["ranges"][0]["renditions"][0]["media"]["path"]
            self.assertNotEqual(second_media, rendition["media"]["path"])
            protected = output / second_media
            orphan = protected.with_name("20260801T0030Z-deadbeefdead.m3u8")
            orphan.write_bytes(b"orphan")
            old = dt.datetime.now(UTC) - dt.timedelta(hours=2)
            os.utime(orphan, (old.timestamp(), old.timestamp()))
            prune_shared_video_orphans(
                output,
                now=dt.datetime.now(UTC) + dt.timedelta(hours=2),
            )
            self.assertTrue(protected.is_file())
            self.assertFalse(orphan.exists())

    def test_composite_sidecars_reuse_cached_frames_and_match_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, original_spec = self.make_source(root)
            layers = catalog["domains"]["bc"]["layers"]
            layers["eccc-geocolor"] = layers.pop("raw-visir")
            spec = replace(
                original_spec,
                product_id="bc-large-overlay",
                layer_id="eccc-geocolor",
            )
            output = root / "output"
            now = dt.datetime(2026, 8, 1, 1, tzinfo=UTC)

            with mock.patch(
                "radarsat.composite_video._render_high_frame",
                wraps=_render_high_frame,
            ) as render:
                first = build_composite_profile(
                    root / "source",
                    output,
                    catalog,
                    spec,
                    ffmpeg=str(shutil.which("ffmpeg")),
                    ranges=(3,),
                    preset_ids=("operational-default-v1",),
                    now=now,
                )
                self.assertEqual(render.call_count, 3)

            self.assertEqual(first["status"], "ok")
            self.assertEqual(len(first["profiles"]), 1)
            profile = first["profiles"][0]
            pointer = json.loads((output / profile["pointerPath"]).read_text())
            self.assertEqual(
                set(pointer),
                {
                    "schemaVersion",
                    "productId",
                    "layerId",
                    "track",
                    "presetId",
                    "layerIds",
                    "renditionPolicy",
                    "rangeHours",
                    "generation",
                    "manifestPath",
                    "generatedAt",
                    "endValidTime",
                    "endSourceTime",
                },
            )
            published_profiles = build_catalog(output)["compositeProfiles"]
            self.assertEqual(
                len(
                    published_profiles["bc-large-overlay"]["eccc-geocolor"]["live"]
                ),
                1,
            )
            manifest = json.loads((output / pointer["manifestPath"]).read_text())
            self.assertEqual(manifest["schemaVersion"], 1)
            self.assertEqual(manifest["generation"], pointer["generation"])
            self.assertEqual(manifest["rangeHours"], 3)
            self.assertEqual(manifest["boundaryIntervalMultiplier"], 4)
            self.assertEqual(
                [value["durationSeconds"] for value in manifest["frames"]],
                [0.2, 0.2, 0.8],
            )
            self.assertEqual(manifest["endValidTime"], "2026-08-01T00:20:00Z")
            self.assertEqual(manifest["endSourceTime"], "2026-08-01T00:20:00Z")
            self.assertEqual(
                manifest["renditions"][0]["media"]["mimeType"],
                "application/vnd.apple.mpegurl",
            )
            self.assertIn("sourceTimes", manifest["frames"][0])
            self.assertEqual(
                manifest["frames"][0]["layerSourceTimes"]["eccc-geocolor"],
                "2026-08-01T00:00:00Z",
            )
            media = output / manifest["renditions"][0]["media"]["path"]
            self.assertTrue(media.is_file())
            self.assertTrue(
                all(
                    (output / value["path"]).is_file()
                    for value in manifest["renditions"][0]["media"]["segments"]
                )
            )

            # The legacy shared-media maintenance pass also scans the
            # composite-* tree.  A sidecar manifest must therefore protect its
            # exact HLS playlist even though it is not part of video-manifests.
            orphan = media.with_name("20260801T0020Z-deadbeefdead.m3u8")
            orphan.write_bytes(b"orphan")
            old = (now - dt.timedelta(hours=1)).timestamp()
            os.utime(orphan, (old, old))
            prune_shared_video_orphans(
                output,
                now=now + dt.timedelta(hours=2),
            )
            self.assertTrue(media.is_file())
            self.assertFalse(orphan.exists())

            manifest_path = output / pointer["manifestPath"]
            range_root = manifest_path.parent
            # Same-anchor generation hashes deliberately sort opposite to
            # commit order. Retention must use generatedAt, never the hash.
            previous = range_root / "20260801T0020Z-000000000000.json"
            expired = range_root / "20260801T0020Z-ffffffffffff.json"
            previous_payload = dict(manifest)
            previous_payload.update({
                "generation": previous.stem,
                "generatedAt": "2026-08-01T00:50:00Z",
            })
            previous.write_text(json.dumps(previous_payload))
            expired_payload = dict(manifest)
            expired_payload.update({
                "generation": expired.stem,
                "generatedAt": "2026-08-01T00:40:00Z",
            })
            expired.write_text(json.dumps(expired_payload))
            old = (now - dt.timedelta(hours=1)).timestamp()
            os.utime(previous, (old, old))
            os.utime(expired, (old, old))
            self.assertEqual(
                prune_composite_sidecar_manifests(
                    output,
                    now=now + dt.timedelta(hours=2),
                ),
                2,
            )
            self.assertTrue(manifest_path.is_file())
            self.assertFalse(previous.exists())
            self.assertFalse(expired.exists())

            with mock.patch(
                "radarsat.composite_video._render_high_frame",
                wraps=_render_high_frame,
            ) as render:
                second = build_composite_profile(
                    root / "source",
                    output,
                    catalog,
                    spec,
                    ffmpeg=str(shutil.which("ffmpeg")),
                    ranges=(3,),
                    preset_ids=("operational-default-v1",),
                    now=now + dt.timedelta(minutes=1),
                )
                self.assertEqual(render.call_count, 0)
            self.assertEqual(
                {value["status"] for value in second["profiles"]},
                {"unchanged"},
            )
            self.assertEqual(
                len(list((output / "composite-frame-cache").rglob("*.png"))),
                3,
            )

    def test_composite_sidecar_failure_preserves_last_good_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, original_spec = self.make_source(root)
            layers = catalog["domains"]["bc"]["layers"]
            layers["eccc-geocolor"] = layers.pop("raw-visir")
            spec = replace(
                original_spec,
                product_id="bc-large-overlay",
                layer_id="eccc-geocolor",
            )
            output = root / "output"
            first = build_composite_profile(
                root / "source",
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                ranges=(3,),
                preset_ids=("operational-default-v1",),
                now=dt.datetime(2026, 8, 1, 1, tzinfo=UTC),
            )
            pointers = {
                value["pointerPath"]: (output / value["pointerPath"]).read_bytes()
                for value in first["profiles"]
            }

            boundary = root / "source/static/bc/boundaries.png"
            write_rgba(boundary, (255, 0, 0, 255), (64, 48))
            stat = boundary.stat()
            os.utime(boundary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            with mock.patch(
                "radarsat.video._encode_ts",
                side_effect=RuntimeError("synthetic encoder failure"),
            ):
                failed = build_composite_profile(
                    root / "source",
                    output,
                    catalog,
                    spec,
                    ffmpeg=str(shutil.which("ffmpeg")),
                    ranges=(3,),
                    preset_ids=("operational-default-v1",),
                    now=dt.datetime(2026, 8, 1, 1, 10, tzinfo=UTC),
                )

            self.assertEqual(failed["status"], "warning")
            self.assertEqual(len(failed["failures"]), 1)
            for relative, previous in pointers.items():
                self.assertEqual((output / relative).read_bytes(), previous)

    def test_weather_smoke_core_freezes_upper_proxies_and_combines_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, original_spec = self.make_source(root)
            source = root / "source"
            layers = catalog["domains"]["bc"]["layers"]
            layers["eccc-geocolor"] = layers.pop("raw-visir")
            static_layers = catalog["domains"]["bc"]["staticLayers"]
            write_rgba(
                source / "static/bc/transmission-lines.png",
                (0, 0, 0, 0),
                (64, 48),
            )
            static_layers["transmission-lines"] = {
                "path": "static/bc/transmission-lines.png",
                "revision": "1",
            }
            base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
            rendered_layers = {
                "smoke": (105, 75, 45, 90),
                "radar-coverage": (255, 255, 255, 20),
                "radar-rain": (0, 180, 255, 100),
                "lightning-trail": (255, 255, 255, 150),
                "hotspots": (255, 90, 60, 180),
                "hrdps-mslp": (210, 40, 180, 150),
                "hrdps-hgt500": (220, 95, 35, 150),
            }
            for layer_id, colour in rendered_layers.items():
                relative = f"frames/bc/{layer_id}/0000.png"
                write_rgba(source / relative, colour, (64, 48))
                layers[layer_id] = {
                    "maxAgeMinutes": 180,
                    "frames": [frame(relative, base)],
                }
            spec = replace(
                original_spec,
                product_id="bc-large-overlay",
                layer_id="eccc-geocolor",
            )
            output = root / "output"
            result = build_composite_profile(
                source,
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                ranges=(3,),
                preset_ids=("weather-smoke-core-v1",),
                now=dt.datetime(2026, 8, 1, 1, tzinfo=UTC),
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["profiles"]), 1)
            pointer = json.loads(
                (output / result["profiles"][0]["pointerPath"]).read_text()
            )
            self.assertEqual(pointer["schemaVersion"], 2)
            self.assertEqual(pointer["compositeKind"], "hybrid-prefix")
            self.assertEqual(pointer["bakedLayerIds"], pointer["layerIds"])
            self.assertEqual(
                pointer["eligibleOverlayLayerIds"],
                ["lightning-trail", "hotspots", "model-mslp", "model-hgt500"],
            )
            manifest = json.loads((output / pointer["manifestPath"]).read_text())
            self.assertEqual(manifest["schemaVersion"], 2)
            self.assertEqual(manifest["renditionPolicy"], "high-only")
            self.assertEqual(
                [value["id"] for value in manifest["renditions"]],
                ["high"],
            )
            first_frame = manifest["frames"][0]
            self.assertEqual(
                {value["id"] for value in first_frame["proxyLayers"]},
                {
                    "lightning-trail",
                    "hotspots",
                    "model-mslp",
                    "model-hgt500",
                    "model-contours",
                },
            )
            combined = next(
                value
                for value in first_frame["proxyLayers"]
                if value["id"] == "model-contours"
            )
            self.assertEqual(combined["ids"], ["model-mslp", "model-hgt500"])
            self.assertEqual(
                set(combined["sourceValidTimes"]),
                {"model-mslp", "model-hgt500"},
            )
            self.assertEqual(
                len(
                    {
                        proxy_layer["sourceKey"]
                        for loop_frame in manifest["frames"]
                        for proxy_layer in loop_frame["proxyLayers"]
                        if proxy_layer["id"] == "model-contours"
                    }
                ),
                1,
            )
            for key, descriptor in manifest["proxies"].items():
                self.assertEqual(key, descriptor["path"])
                self.assertTrue((output / descriptor["path"]).is_file())
                self.assertEqual(len(descriptor["sha256"]), 64)
                self.assertEqual(descriptor["width"], 64)
                self.assertEqual(descriptor["height"], 48)

    def test_composite_prune_retires_an_unconfigured_preset_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            group = (
                output
                / "composite-manifests/bc-large-overlay/eccc-geocolor/live"
                / "retired-preset-v1/3"
            )
            group.mkdir(parents=True)
            generation = "20260801T0000Z-abcdef012345"
            manifest = group / f"{generation}.json"
            manifest.write_text(json.dumps({
                "generation": generation,
                "generatedAt": "2026-08-01T00:00:00Z",
            }))
            pointer = (
                output
                / "composite-index/bc-large-overlay/eccc-geocolor/live"
                / "retired-preset-v1/3.json"
            )
            pointer.parent.mkdir(parents=True)
            pointer.write_text(json.dumps({
                "generation": generation,
                "manifestPath": manifest.relative_to(output).as_posix(),
            }))
            old = dt.datetime(2026, 8, 1, tzinfo=UTC).timestamp()
            os.utime(manifest, (old, old))
            os.utime(pointer, (old, old))

            self.assertEqual(
                prune_composite_sidecar_manifests(
                    output,
                    now=dt.datetime(2026, 8, 1, 1, tzinfo=UTC),
                ),
                1,
            )
            self.assertFalse(manifest.exists())
            self.assertFalse(pointer.exists())

    def test_efficient_cache_is_derived_from_high_composite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            high = root / "high.png"
            image = Image.new("RGB", (64, 64), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 63, 47), fill=(20, 80, 160))
            image.save(high)
            destination = root / "efficient.png"

            _derive_rendition(high, 48, 32, 24, destination)

            with Image.open(destination) as derived:
                self.assertEqual(derived.size, (32, 40))
                self.assertEqual(derived.getpixel((0, 39)), (255, 255, 255))
                self.assertEqual(derived.getpixel((16, 12)), (20, 80, 160))

    def test_nonpilot_profile_keeps_dynamic_proxy_fallback_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            result = build_profile(
                root / "source",
                root / "output",
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )
            manifest = json.loads(
                (root / "output" / str(result["manifestPath"])).read_text()
            )
            self.assertNotIn("composites", manifest)

    def test_satellite_choices_reuse_rendered_proxy_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, first_spec = self.make_source(root)
            layers = catalog["domains"]["bc"]["layers"]
            layers["convective"] = dict(layers["raw-visir"])
            second_spec = ProfileSpec(
                first_spec.product_id,
                first_spec.domain_id,
                "convective",
                first_spec.viewport,
                first_spec.width,
                first_spec.height,
                first_spec.cadence_minutes,
                crf=first_spec.crf,
                preset=first_spec.preset,
            )
            selection_cache = {}
            render_cache = {}
            with mock.patch("radarsat.video._render_proxy", wraps=_render_proxy) as render:
                for spec in (first_spec, second_spec):
                    build_profile(
                        root / "source",
                        root / "output",
                        catalog,
                        spec,
                        ffmpeg=str(shutil.which("ffmpeg")),
                        hours=1,
                        proxy_selection_cache=selection_cache,
                        proxy_render_cache=render_cache,
                    )
            self.assertEqual(render.call_count, 2)

    def test_hls_segments_pack_missing_observations_at_uniform_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
            layers = catalog["domains"]["bc"]["layers"]
            layers["convective"] = layers.pop("raw-visir")
            spec = ProfileSpec(
                spec.product_id,
                spec.domain_id,
                "convective",
                spec.viewport,
                spec.width,
                spec.height,
                spec.cadence_minutes,
                crf=spec.crf,
                preset=spec.preset,
            )
            frames = layers["convective"]["frames"]
            assert isinstance(frames, list)
            layers["convective"]["maxAgeMinutes"] = 0
            # Two complete playback chunks with a skipped UTC segment group.
            # The gap must not change any frame's display duration.
            originals = list(frames)
            frames.clear()
            for index, minute in enumerate((*range(0, 60, 10), *range(180, 240, 10))):
                item = dict(originals[index % len(originals)])
                valid = base + dt.timedelta(minutes=minute)
                item["validTime"] = stamp(valid)
                item["sourceTimes"] = {"GOES-18": stamp(valid)}
                frames.append(item)

            result = build_profile(
                root / "source",
                root / "output",
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=4,
                now=base + dt.timedelta(hours=4),
            )

            manifest = json.loads(
                (root / "output" / str(result["manifestPath"])).read_text()
            )
            segments = manifest["media"]["segments"]
            self.assertEqual(len(segments), 2)
            playlist = (
                root / "output" / str(manifest["media"]["path"])
            ).read_text()
            self.assertIn("#EXT-X-INDEPENDENT-SEGMENTS", playlist)
            self.assertEqual(playlist.count("#EXT-X-DISCONTINUITY"), 1)
            self.assertEqual(
                [item["ptsSeconds"] for item in manifest["frames"]],
                [round(index * 0.2, 2) for index in range(12)],
            )
            self.assertEqual(
                [item["durationSeconds"] for item in manifest["frames"]],
                [0.2] * 12,
            )

            encoded: list[list[dict[str, str]]] = []
            for segment in segments:
                probe = subprocess.run(
                    [
                        str(shutil.which("ffprobe")),
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "frame=best_effort_timestamp_time,duration_time",
                        "-of",
                        "json",
                        str(root / "output" / segment["path"]),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                segment_frames = json.loads(probe.stdout)["frames"]
                self.assertEqual(
                    len(segment_frames),
                    round(float(segment["durationSeconds"]) * VIDEO_FRAME_RATE),
                )
                encoded.append(segment_frames)

            starts = [
                float(segment[0]["best_effort_timestamp_time"])
                for segment in encoded
            ]
            # Adjacent observations remain continuous on the absolute media
            # clock. The skipped group keeps its stable timestamp for segment
            # reuse; EXT-X-DISCONTINUITY instructs HLS to pack it at playback.
            self.assertGreater(starts[1] - starts[0], 1.32)

    def test_shared_orphan_scan_can_be_deferred_to_final_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            segment = output / "video-segments/shared-bc-full/raw-visir/live/old.ts"
            segment.parent.mkdir(parents=True)
            segment.write_bytes(b"old")
            now = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
            old = (now - dt.timedelta(hours=2)).timestamp()
            os.utime(segment, (old, old))

            deferred = prune_local_video_orphans(
                output,
                "bc-large-overlay",
                now=now,
                _prune_shared=False,
            )
            self.assertTrue(segment.is_file())
            self.assertEqual(deferred["removedDependencies"], 0)

            final = prune_local_video_orphans(
                output,
                "north-pacific-overlay",
                now=now,
            )
            self.assertFalse(segment.exists())
            self.assertEqual(final["removedDependencies"], 1)

    def test_local_prune_protects_current_hybrid_proxy_and_removes_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            product = "bc-northeast-overlay"
            proxy_root = output / "video-proxies" / product / "lightning-trail"
            protected = proxy_root / "1111111111111111.webp"
            orphan = proxy_root / "2222222222222222.webp"
            protected.parent.mkdir(parents=True)
            protected.write_bytes(b"current hybrid proxy")
            orphan.write_bytes(b"unreferenced proxy")
            relative = protected.relative_to(output).as_posix()
            manifest = (
                output
                / "composite-manifests"
                / product
                / "eccc-geocolor"
                / "live"
                / "weather-smoke-core-v1"
                / "3"
                / "20260801T1000Z-abcdef012345.json"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({
                "schemaVersion": 2,
                "proxies": {relative: {"path": relative}},
            }))
            now = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
            old = (now - dt.timedelta(hours=2)).timestamp()
            os.utime(protected, (old, old))
            os.utime(orphan, (old, old))

            result = prune_local_video_orphans(
                output,
                product,
                now=now,
                _prune_shared=False,
            )

            self.assertTrue(protected.is_file())
            self.assertFalse(orphan.exists())
            self.assertEqual(result["removedDependencies"], 1)

    def test_shared_proxy_prune_fails_closed_on_ambiguous_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            proxy = (
                output
                / "video-proxies"
                / "bc-northeast-overlay"
                / "lightning-trail"
                / "3333333333333333.webp"
            )
            proxy.parent.mkdir(parents=True)
            proxy.write_bytes(b"old orphan")
            ambiguous = (
                output
                / "composite-manifests"
                / "bc-northeast-overlay"
                / "eccc-geocolor"
                / "live"
                / "weather-smoke-core-v1"
                / "3"
                / "20260801T1000Z-abcdef012345.json"
            )
            ambiguous.parent.mkdir(parents=True)
            ambiguous.write_text("{not-json")
            now = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
            old = (now - dt.timedelta(hours=2)).timestamp()
            os.utime(proxy, (old, old))

            self.assertEqual(prune_shared_video_orphans(output, now=now), 0)
            self.assertTrue(proxy.is_file())

    def test_shared_prune_removes_only_unreferenced_aged_proxies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            proxy_root = (
                output
                / "video-proxies"
                / "bc-northeast-overlay"
                / "lightning-trail"
            )
            protected = proxy_root / "4444444444444444.webp"
            orphan = proxy_root / "5555555555555555.webp"
            recent = proxy_root / "6666666666666666.webp"
            protected.parent.mkdir(parents=True)
            for path in (protected, orphan, recent):
                path.write_bytes(path.name.encode())
            relative = protected.relative_to(output).as_posix()
            manifest = output / "composite-manifests/current.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({
                "schemaVersion": 2,
                "proxies": {relative: {"path": relative}},
            }))
            now = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
            old = (now - dt.timedelta(hours=2)).timestamp()
            os.utime(protected, (old, old))
            os.utime(orphan, (old, old))

            self.assertEqual(prune_shared_video_orphans(output, now=now), 1)
            self.assertTrue(protected.is_file())
            self.assertFalse(orphan.exists())
            self.assertTrue(recent.is_file())

    def test_prune_retires_unoffered_track_pointer_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            product = "bc-northeast-overlay"
            generation = "20260801T1000Z-abcdef012345"
            manifest = (
                output / "video-manifests" / product / "eccc-geocolor"
                / "archive" / f"{generation}.json"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"schemaVersion": 2}))
            index = output / "video-index" / product / "eccc-geocolor.json"
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({
                "schemaVersion": 2,
                "profiles": {
                    "live": {"generation": generation},
                    "archive": {"generation": generation},
                },
            }))
            now = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
            old = (now - dt.timedelta(hours=2)).timestamp()
            os.utime(manifest, (old, old))

            result = prune_local_video_orphans(output, product, now=now)

            self.assertFalse(manifest.exists())
            self.assertEqual(result["removedManifests"], 1)
            self.assertEqual(
                set(json.loads(index.read_text())["profiles"]),
                {"live"},
            )

    def test_parallel_build_can_defer_shared_orphan_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            old_segment = root / "output/video-segments/shared-bc-full/raw-visir/live/old.ts"
            old_segment.parent.mkdir(parents=True)
            old_segment.write_bytes(b"old")
            old = (dt.datetime.now(UTC) - dt.timedelta(hours=2)).timestamp()
            os.utime(old_segment, (old, old))

            with (
                mock.patch("radarsat.video.VIDEO_PROFILES", (spec,)),
                mock.patch("radarsat.video.build_catalog", return_value=catalog),
                mock.patch("radarsat.video.build_profile", return_value={"status": "unchanged"}),
            ):
                build_satellite_videos(
                    root / "source",
                    root / "output",
                    product_ids=(spec.product_id,),
                    track_names=(spec.track,),
                    ffmpeg=str(shutil.which("ffmpeg")),
                    hours=1,
                    prune_shared_assets=False,
                )
            self.assertTrue(old_segment.is_file())
            self.assertEqual(
                prune_shared_video_orphans(root / "output"),
                1,
            )
            self.assertFalse(old_segment.exists())

    def test_build_can_select_only_priority_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            secondary = replace(spec, layer_id="raw-ir")
            with (
                mock.patch("radarsat.video.VIDEO_PROFILES", (spec, secondary)),
                mock.patch("radarsat.video.build_catalog", return_value=catalog),
                mock.patch("radarsat.video.build_profile", return_value={"status": "unchanged"}) as build,
            ):
                result = build_satellite_videos(
                    root / "source",
                    root / "output",
                    product_ids=(spec.product_id,),
                    layer_ids=(spec.layer_id,),
                    track_names=(spec.track,),
                    ffmpeg=str(shutil.which("ffmpeg")),
                    hours=1,
                    prune_shared_assets=False,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(build.call_count, 1)
            self.assertEqual(build.call_args.args[3].layer_id, spec.layer_id)

            with self.assertRaisesRegex(ValueError, "Unsupported video layers"):
                build_satellite_videos(
                    root / "source",
                    root / "output",
                    product_ids=(spec.product_id,),
                    layer_ids=("not-a-layer",),
                    track_names=(spec.track,),
                    ffmpeg="ffmpeg",
                    hours=1,
                    prune_shared_assets=False,
                )

    def test_failed_rebuild_preserves_previous_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            output = root / "output"
            first = build_profile(
                root / "source",
                output,
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )
            index_path = output / "video-index/bc-northeast-overlay/raw-visir.json"
            before = index_path.read_bytes()
            frames = catalog["domains"]["bc"]["layers"]["raw-visir"]["frames"]
            assert isinstance(frames, list)
            frames[-1]["fetchedAt"] = "2026-08-01T01:05:00Z"

            with mock.patch("radarsat.video._encode_ts", side_effect=RuntimeError("encode failed")):
                with self.assertRaisesRegex(RuntimeError, "encode failed"):
                    build_profile(
                        root / "source",
                        output,
                        catalog,
                        spec,
                        ffmpeg=str(shutil.which("ffmpeg")),
                        hours=1,
                    )

            self.assertEqual(index_path.read_bytes(), before)
            self.assertTrue((output / str(first["manifestPath"])).is_file())

    def test_missing_optional_proxy_does_not_abort_satellite_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
            catalog["domains"]["bc"]["layers"]["radar-rain"] = {
                "maxAgeMinutes": 20,
                "frames": [frame("frames/bc/radar-rain/missing.png", base)],
            }

            result = build_profile(
                root / "source",
                root / "output",
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )

            self.assertEqual(result["status"], "built")
            self.assertEqual(result["proxyWarnings"], 1)
            manifest = json.loads(
                (root / "output" / str(result["manifestPath"])).read_text()
            )
            self.assertEqual(
                manifest["proxyWarnings"],
                [
                    {
                        "sourceKey": (
                            "frames/bc/radar-rain/missing.png"
                            "?v=2026-08-01T01%3A00%3A00Z"
                        ),
                        "sourcePath": "frames/bc/radar-rain/missing.png",
                        "renderedLayerId": "radar-rain",
                        "reason": "source-disappeared-during-build",
                    }
                ],
            )
            self.assertTrue(
                all(
                    layer["id"] != "radar-rain"
                    for video_frame in manifest["frames"]
                    for layer in video_frame["proxyLayers"]
                )
            )

    def test_unreadable_optional_proxy_does_not_abort_satellite_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
            relative = "frames/bc/radar-rain/truncated.png"
            source = root / "source" / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"not an image")
            catalog["domains"]["bc"]["layers"]["radar-rain"] = {
                "maxAgeMinutes": 20,
                "frames": [frame(relative, base)],
            }

            result = build_profile(
                root / "source",
                root / "output",
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )

            self.assertEqual(result["status"], "built")
            self.assertEqual(result["proxyWarnings"], 1)
            manifest = json.loads(
                (root / "output" / str(result["manifestPath"])).read_text()
            )
            self.assertEqual(
                manifest["proxyWarnings"][0]["reason"],
                "source-unreadable-during-build",
            )
            self.assertTrue(
                all(
                    layer["id"] != "radar-rain"
                    for video_frame in manifest["frames"]
                    for layer in video_frame["proxyLayers"]
                )
            )

    def test_source_pruned_after_catalog_snapshot_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            missing = root / "source/frames/bc/raw-visir/10.webp"
            missing.unlink()

            result = build_profile(
                root / "source",
                root / "output",
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )

            self.assertEqual(result["status"], "built")
            manifest = json.loads(
                (root / "output" / str(result["manifestPath"])).read_text()
            )
            self.assertEqual(len(manifest["frames"]), 2)
            self.assertNotIn("10.webp", {frame["sourcePath"] for frame in manifest["frames"]})

    def test_source_pruned_during_build_remains_available_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            output = root / "output"
            rotating = root / "source/frames/bc/raw-visir/10.webp"
            source_fingerprint = video_module._source_fingerprint
            removed = False

            def prune_then_fingerprint(
                snapshot_root: Path,
                selected_frame: object,
            ) -> object:
                nonlocal removed
                if not removed:
                    rotating.unlink()
                    removed = True
                return source_fingerprint(snapshot_root, selected_frame)

            with mock.patch(
                "radarsat.video._source_fingerprint",
                side_effect=prune_then_fingerprint,
            ):
                result = build_profile(
                    root / "source",
                    output,
                    catalog,
                    spec,
                    ffmpeg=str(shutil.which("ffmpeg")),
                    hours=1,
                )

            self.assertTrue(removed)
            self.assertFalse(rotating.exists())
            self.assertEqual(result["status"], "built")
            manifest = json.loads((output / str(result["manifestPath"])).read_text())
            self.assertEqual(len(manifest["frames"]), 3)
            self.assertIn(
                "frames/bc/raw-visir/10.webp",
                {frame["sourcePath"] for frame in manifest["frames"]},
            )
            self.assertEqual(list(output.glob(".radarsat-video-source-*")), [])

    def test_manifest_freezes_ordered_dynamic_proxy_selections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            source = root / "source"
            base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
            radar_path = "frames/bc/radar-rain/05.png"
            lightning_a_path = (
                "frames/bc/lightning-trail-region-northeast/10.png"
            )
            lightning_b_path = (
                "frames/bc/lightning-trail-region-northeast/20.png"
            )
            stale_lightning_path = (
                "frames/bc/lightning-trail-region-northeast/15-old-crop.png"
            )
            for relative in (
                radar_path,
                lightning_a_path,
                lightning_b_path,
                stale_lightning_path,
            ):
                write_rgba(source / relative, (255, 255, 255, 96), (64, 48))
            radar = frame(radar_path, base + dt.timedelta(minutes=5))
            lightning_a = frame(
                lightning_a_path,
                base + dt.timedelta(minutes=10),
            )
            lightning_a["sourceTimes"] = {
                "CLDN": stamp(base + dt.timedelta(minutes=4))
            }
            lightning_a["regionalViewport"] = spec.viewport
            lightning_b = frame(
                lightning_b_path,
                base + dt.timedelta(minutes=20),
            )
            lightning_b["sourceTimes"] = {
                "CLDN": stamp(base + dt.timedelta(minutes=8)),
                "GLM": stamp(base + dt.timedelta(minutes=7)),
            }
            lightning_b["regionalViewport"] = spec.viewport
            stale_lightning = frame(
                stale_lightning_path,
                base + dt.timedelta(minutes=15),
            )
            stale_lightning["sourceTimes"] = {
                "CLDN": stamp(base + dt.timedelta(minutes=9))
            }
            stale_lightning["regionalViewport"] = {
                **spec.viewport,
                "left": float(spec.viewport["left"]) + 0.01,
            }
            layers = catalog["domains"]["bc"]["layers"]
            layers["radar-rain"] = {
                "maxAgeMinutes": 12,
                "frames": [radar],
            }
            layers["lightning-trail-region-northeast"] = {
                "maxAgeMinutes": 30,
                "frames": [lightning_a, stale_lightning, lightning_b],
            }

            result = build_profile(
                source,
                root / "output",
                catalog,
                spec,
                ffmpeg=str(shutil.which("ffmpeg")),
                hours=1,
            )

            manifest = json.loads(
                (root / "output" / str(result["manifestPath"])).read_text()
            )
            proxy_layers = [item["proxyLayers"] for item in manifest["frames"]]
            self.assertEqual(
                [[layer["id"] for layer in items] for items in proxy_layers],
                [
                    ["watersheds", "boundaries"],
                    ["radar-rain", "watersheds", "boundaries", "lightning-trail"],
                    ["watersheds", "boundaries", "lightning-trail"],
                ],
            )
            self.assertEqual(
                proxy_layers[1][0]["sourceValidTime"],
                "2026-08-01T00:05:00Z",
            )
            self.assertEqual(
                proxy_layers[1][3]["renderId"],
                "lightning-trail-region-northeast",
            )
            self.assertEqual(
                proxy_layers[1][3]["sourceValidTime"],
                "2026-08-01T00:04:00Z",
            )
            self.assertEqual(
                proxy_layers[2][2]["sourceValidTime"],
                "2026-08-01T00:08:00Z",
            )
            proxy_keys = set(manifest["proxies"])
            self.assertTrue(
                all(
                    layer["sourceKey"] in proxy_keys
                    for items in proxy_layers
                    for layer in items
                )
            )


if __name__ == "__main__":
    unittest.main()
