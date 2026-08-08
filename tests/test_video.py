from __future__ import annotations

import datetime as dt
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

from radarsat.video import (
    ProfileSpec,
    _render_proxy,
    _selected_satellite_frames,
    build_profile,
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
    def test_ne_bc_native_preference_and_fallback_match_viewer(self) -> None:
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
                "raw-visir-native",
                "raw-visir-native",
                "raw-visir",
                "raw-visir",
            ],
        )
        self.assertEqual([item.valid_time.minute for item in selected], [0, 10, 20, 30, 40, 50])
        self.assertEqual(selected[3].source_valid_time, base + dt.timedelta(minutes=10))

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
    def make_source(self, root: Path) -> tuple[dict[str, object], ProfileSpec]:
        source = root / "source"
        write_rgba(source / "static/bc/base-dark.png", (18, 28, 38, 255), (64, 48))
        boundary = source / "static/bc/boundaries.png"
        write_rgba(boundary, (0, 0, 0, 0), (64, 48))
        with Image.open(boundary) as boundary_image:
            draw = ImageDraw.Draw(boundary_image)
            draw.line((0, 0, 63, 47), fill=(255, 255, 255, 255), width=1)
            boundary_image.save(boundary)
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

    def test_progressive_mp4_manifest_pts_and_skip(self) -> None:
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
            self.assertEqual([item["ptsSeconds"] for item in manifest["frames"]], [0.0, 0.22, 0.44])
            self.assertEqual([item["durationSeconds"] for item in manifest["frames"]], [0.22] * 3)
            self.assertEqual(manifest["media"]["width"], 64)
            self.assertEqual(manifest["media"]["height"], 48)
            self.assertIn("static/bc/boundaries.png?v=1", manifest["proxies"])
            self.assertEqual(
                [layer["id"] for layer in manifest["frames"][0]["proxyLayers"]],
                ["boundaries"],
            )
            self.assertIsNone(
                manifest["frames"][0]["proxyLayers"][0]["sourceValidTime"]
            )
            self.assertTrue(media.is_file())
            with media.open("rb") as handle:
                data = handle.read()
            self.assertLess(data.index(b"moov"), data.index(b"mdat"))

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
                    str(media),
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
            self.assertEqual(int(stream["nb_frames"]), 4)

            frame_probe = subprocess.run(
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
                    str(media),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            encoded_frames = json.loads(frame_probe.stdout)["frames"]
            self.assertEqual(
                [float(item["best_effort_timestamp_time"]) for item in encoded_frames],
                [0.0, 0.22, 0.44, 0.66],
            )
            self.assertEqual(
                [float(item["duration_time"]) for item in encoded_frames[:3]],
                [0.22, 0.22, 0.22],
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

            with mock.patch("radarsat.video._encode_mp4", side_effect=RuntimeError("encode failed")):
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
            for relative in (radar_path, lightning_a_path, lightning_b_path):
                write_rgba(source / relative, (255, 255, 255, 96), (64, 48))
            radar = frame(radar_path, base + dt.timedelta(minutes=5))
            lightning_a = frame(
                lightning_a_path,
                base + dt.timedelta(minutes=10),
            )
            lightning_a["sourceTimes"] = {
                "CLDN": stamp(base + dt.timedelta(minutes=4))
            }
            lightning_b = frame(
                lightning_b_path,
                base + dt.timedelta(minutes=20),
            )
            lightning_b["sourceTimes"] = {
                "CLDN": stamp(base + dt.timedelta(minutes=8)),
                "GLM": stamp(base + dt.timedelta(minutes=7)),
            }
            layers = catalog["domains"]["bc"]["layers"]
            layers["radar-rain"] = {
                "maxAgeMinutes": 12,
                "frames": [radar],
            }
            layers["lightning-trail-region-northeast"] = {
                "maxAgeMinutes": 30,
                "frames": [lightning_a, lightning_b],
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
                    ["boundaries"],
                    ["radar-rain", "boundaries", "lightning-trail"],
                    ["boundaries", "lightning-trail"],
                ],
            )
            self.assertEqual(
                proxy_layers[1][0]["sourceValidTime"],
                "2026-08-01T00:05:00Z",
            )
            self.assertEqual(
                proxy_layers[1][2]["renderId"],
                "lightning-trail-region-northeast",
            )
            self.assertEqual(
                proxy_layers[1][2]["sourceValidTime"],
                "2026-08-01T00:04:00Z",
            )
            self.assertEqual(
                proxy_layers[2][1]["sourceValidTime"],
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
