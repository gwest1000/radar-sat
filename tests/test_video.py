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

from radarsat.video import (
    ProfileSpec,
    VIDEO_FRAME_RATE,
    _render_proxy,
    _selected_satellite_frames,
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
            self.assertEqual(
                [layer["id"] for layer in manifest["frames"][0]["proxyLayers"]],
                ["boundaries"],
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

    def test_default_composite_pilot_bakes_default_stack_and_is_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, original_spec = self.make_source(root)
            spec = replace(original_spec, product_id="bc-large-overlay")
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
            composite = first_manifest["defaultComposite"]
            self.assertEqual(composite["id"], "operational-default-v1")
            self.assertEqual(
                composite["layerIds"],
                [
                    "base-dark",
                    "raw-visir",
                    "radar-coverage",
                    "radar-rain",
                    "watersheds",
                    "transmission-lines",
                    "boundaries",
                    "lightning-trail",
                    "hotspots",
                ],
            )
            self.assertEqual(composite["mediaViewport"], spec.viewport)
            self.assertEqual(composite["media"]["width"], 64)
            self.assertEqual(composite["media"]["contentHeight"], 48)
            self.assertIn(
                "videos/composite-bc-large-overlay/raw-visir/live/",
                composite["media"]["path"],
            )
            first_segment = composite["media"]["segments"][0]
            self.assertIn(
                "video-segments/composite-bc-large-overlay/raw-visir/live/",
                first_segment["path"],
            )
            self.assertTrue((output / first_segment["path"]).is_file())
            self.assertGreater(first["compositeMediaBytes"], 0)

            # A static-overlay content change leaves the satellite-only media
            # reusable, but must produce a new composite segment and manifest.
            boundary = root / "source/static/bc/boundaries.png"
            write_rgba(boundary, (255, 0, 0, 255), (64, 48))
            stat = boundary.stat()
            os.utime(boundary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
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
            self.assertEqual(
                second_manifest["media"]["segments"][0]["path"],
                first_manifest["media"]["segments"][0]["path"],
            )
            self.assertNotEqual(
                second_manifest["defaultComposite"]["media"]["segments"][0]["path"],
                first_segment["path"],
            )
            protected = output / second_manifest["defaultComposite"]["media"][
                "segments"
            ][0]["path"]
            orphan = protected.with_name("orphan.ts")
            orphan.write_bytes(b"orphan")
            old = dt.datetime.now(UTC) - dt.timedelta(hours=2)
            os.utime(orphan, (old.timestamp(), old.timestamp()))
            prune_shared_video_orphans(
                output,
                now=dt.datetime.now(UTC) + dt.timedelta(hours=2),
            )
            self.assertTrue(protected.is_file())
            self.assertFalse(orphan.exists())

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
            self.assertNotIn("defaultComposite", manifest)

    def test_satellite_choices_reuse_rendered_proxy_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, first_spec = self.make_source(root)
            layers = catalog["domains"]["bc"]["layers"]
            layers["daynight"] = dict(layers["raw-visir"])
            second_spec = ProfileSpec(
                first_spec.product_id,
                first_spec.domain_id,
                "daynight",
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
            self.assertEqual(render.call_count, 1)

    def test_hls_segments_pack_missing_observations_at_uniform_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, spec = self.make_source(root)
            base = dt.datetime(2026, 8, 1, 0, tzinfo=UTC)
            layers = catalog["domains"]["bc"]["layers"]
            layers["daynight"] = layers.pop("raw-visir")
            spec = ProfileSpec(
                spec.product_id,
                spec.domain_id,
                "daynight",
                spec.viewport,
                spec.width,
                spec.height,
                spec.cadence_minutes,
                crf=spec.crf,
                preset=spec.preset,
            )
            frames = layers["daynight"]["frames"]
            assert isinstance(frames, list)
            layers["daynight"]["maxAgeMinutes"] = 0
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
