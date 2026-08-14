from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from radarsat.config import DOMAINS, LAYERS
from radarsat.pipeline import (
    derive_lightning_trails,
    frame_path,
    metadata_path,
    prune,
    retained_times,
    safe_archive_path,
    write_metadata,
)
from radarsat.r2 import (
    PublicationSafetyError,
    R2Config,
    build_catalog_index,
    cache_control,
    content_type,
    discover_objects,
    expired_remote_keys,
    expired_video_keys,
    publish,
    size_guard,
)
from radarsat.r2 import _default_composite_paths
from radarsat.retention import keep_frame, keep_layer_frame


UTC = dt.timezone.utc


def write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 6), (0, 0, 0, 0)).save(path, "PNG")


def write_webp(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 6), (0, 0, 0, 0)).save(path, "WEBP", lossless=True)


def make_archive(root: Path, valid: dt.datetime) -> None:
    domain = DOMAINS["bc"]
    layer = LAYERS["radar-rain"]
    frame = frame_path(root, domain, layer, valid)
    write_png(frame)
    write_metadata(root, domain, layer, valid, frame)
    static = root / "static" / "bc" / "base-dark.png"
    write_png(static)
    catalog = {
        "schemaVersion": 1,
        "generatedAt": valid.isoformat().replace("+00:00", "Z"),
        "domains": {
            "bc": {
                "layers": {
                    "radar-rain": {
                        "frames": [json.loads(metadata_path(root, domain, layer, valid).read_text())]
                    }
                },
                "staticLayers": {"base-dark": {"path": "static/bc/base-dark.png"}},
            }
        },
        "legends": {},
    }
    (root / "catalog.json").write_text(json.dumps(catalog))


def add_video_profile(
    root: Path,
    generation: str = "20260720T2340Z-abcdef012345",
    *,
    product_id: str = "bc-northeast-overlay",
) -> set[str]:
    layer_id = "raw-visir"
    track = "live"
    media_generation = f"{generation[:14]}-fedcba543210"
    media_relative = f"videos/{product_id}/{layer_id}/{track}/{media_generation}.mp4"
    manifest_relative = (
        f"video-manifests/{product_id}/{layer_id}/{track}/{generation}.json"
    )
    proxy_relative = (
        f"video-proxies/{product_id}/radar-rain/abcdef0123456789.webp"
    )
    static_relative = (
        f"video-static-overlays/{product_id}/abcdef0123456789.png"
    )
    media = root / media_relative
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"test-fast-start-mp4")
    write_webp(root / proxy_relative)
    write_png(root / static_relative)
    manifest = root / manifest_relative
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "schemaVersion": 1,
        "generation": generation,
        "generatedAt": "2026-07-20T23:42:00Z",
        "productId": product_id,
        "layerId": layer_id,
        "track": track,
        "transport": "progressive-mp4",
        "cadenceMinutes": 10,
        "media": {
            "path": media_relative,
            "mimeType": "video/mp4",
            "byteLength": media.stat().st_size,
        },
        "frames": [
            {
                "index": 0,
                "validTime": "2026-07-20T23:30:00Z",
                "sourceValidTime": "2026-07-20T23:30:21Z",
                "ptsSeconds": 0.0,
                "durationSeconds": 0.22,
                "proxyLayers": [{
                    "id": "radar-rain",
                    "renderId": "radar-rain",
                    "sourceKey": "frames/bc/radar-rain/frame.png?v=revision",
                    "sourceValidTime": "2026-07-20T23:30:00Z",
                }],
            },
            {
                "index": 1,
                "validTime": "2026-07-20T23:40:00Z",
                "sourceValidTime": "2026-07-20T23:40:21Z",
                "ptsSeconds": 0.22,
                "durationSeconds": 0.22,
                "proxyLayers": [{
                    "id": "radar-rain",
                    "renderId": "radar-rain",
                    "sourceKey": "frames/bc/radar-rain/frame.png?v=revision",
                    "sourceValidTime": "2026-07-20T23:30:00Z",
                }],
            },
        ],
        "proxies": {
            "frames/bc/radar-rain/frame.png?v=revision": {
                "path": proxy_relative,
                "width": 1280,
                "height": 860,
                "byteLength": (root / proxy_relative).stat().st_size,
            }
        },
        "staticOverlay": {
            "path": static_relative,
            "byteLength": (root / static_relative).stat().st_size,
        },
    }))
    catalog_path = root / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    catalog.setdefault("products", []).append({
        "id": product_id,
        "layers": [{"id": layer_id}],
    })
    catalog["videoProfiles"] = {
        product_id: {
            layer_id: {
                track: {
                    "generation": generation,
                    "manifestPath": manifest_relative,
                }
            }
        }
    }
    catalog_path.write_text(json.dumps(catalog))
    return {media_relative, manifest_relative, proxy_relative, static_relative}


def add_hls_video_profile(
    root: Path,
    generation: str = "20260720T2340Z-abcdef012345",
    *,
    product_id: str = "bc-northeast-overlay",
) -> set[str]:
    expected = add_video_profile(root, generation, product_id=product_id)
    manifest_relative = next(key for key in expected if key.startswith("video-manifests/"))
    manifest_path = root / manifest_relative
    payload = json.loads(manifest_path.read_text())
    old_media = root / payload["media"]["path"]
    old_media.unlink()
    media_relative = (
        "videos/shared-bc-regional-hires/raw-visir/live/"
        "20260720T2340Z-fedcba543210.m3u8"
    )
    segment_relative = (
        "video-segments/shared-bc-regional-hires/raw-visir/live/"
        "20260720T2300Z-0123456789abcdef.ts"
    )
    media = root / media_relative
    segment = root / segment_relative
    media.parent.mkdir(parents=True, exist_ok=True)
    segment.parent.mkdir(parents=True, exist_ok=True)
    segment.write_bytes(b"test-mpeg-ts")
    media.write_text("#EXTM3U\n#EXTINF:1.0,\nsegment.ts\n#EXT-X-ENDLIST\n")
    payload["transport"] = "hls-ts"
    payload["domainId"] = "bc"
    payload["media"] = {
        "path": media_relative,
        "mimeType": "application/vnd.apple.mpegurl",
        "byteLength": media.stat().st_size,
        "segments": [
            {
                "path": segment_relative,
                "byteLength": segment.stat().st_size,
                "durationSeconds": 1.0,
                "firstFrame": 0,
                "lastFrame": 1,
            }
        ],
    }
    manifest_path.write_text(json.dumps(payload))
    expected.remove(old_media.relative_to(root).as_posix())
    expected.update({media_relative, segment_relative})
    return expected


def add_default_composite_profile(root: Path) -> set[str]:
    product_id = "bc-large-overlay"
    expected = add_hls_video_profile(root, product_id=product_id)
    manifest_relative = next(key for key in expected if key.startswith("video-manifests/"))
    manifest_path = root / manifest_relative
    payload = json.loads(manifest_path.read_text())
    payload["viewport"] = {"left": 0.0, "top": 0.05, "width": 1.0, "height": 0.9}
    payload["width"] = 1280
    payload["height"] = 860
    owner = f"composite-{product_id}"
    media_relative = (
        f"videos/{owner}/raw-visir/live/"
        "20260720T2340Z-123456789abc.m3u8"
    )
    segment_relative = (
        f"video-segments/{owner}/raw-visir/live/"
        "20260720T2300Z-1234567890abcdef.ts"
    )
    media = root / media_relative
    segment = root / segment_relative
    media.parent.mkdir(parents=True, exist_ok=True)
    segment.parent.mkdir(parents=True, exist_ok=True)
    segment.write_bytes(b"fully-composited-mpeg-ts")
    relative_segment = os.path.relpath(segment, media.parent)
    media.write_text(
        f"#EXTM3U\n#EXTINF:1.0,\n{relative_segment}\n#EXT-X-ENDLIST\n"
    )
    payload["defaultComposite"] = {
        "id": "operational-default-v1",
        "layerIds": [
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
        "mediaViewport": payload["viewport"],
        "media": {
            "path": media_relative,
            "mimeType": "application/vnd.apple.mpegurl",
            "codec": "avc1",
            "width": 1280,
            "height": 876,
            "contentHeight": 860,
            "frameRate": 5,
            "byteLength": media.stat().st_size,
            "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
            "segments": [{
                "path": segment_relative,
                "byteLength": segment.stat().st_size,
                "sha256": hashlib.sha256(segment.read_bytes()).hexdigest(),
                "durationSeconds": 1.0,
                "firstFrame": 0,
                "lastFrame": 1,
            }],
        },
    }
    manifest_path.write_text(json.dumps(payload))
    return {*expected, media_relative, segment_relative}


class FakeR2:
    def __init__(
        self,
        remote: dict[str, int] | None = None,
        modified: dict[str, dt.datetime] | None = None,
    ) -> None:
        self.remote = dict(remote or {})
        self.modified = dict(modified or {})
        self.events: list[tuple[str, str | tuple[str, ...]]] = []

    def list_objects_v2(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": size,
                    **(
                        {"LastModified": self.modified[key]}
                        if key in self.modified
                        else {}
                    ),
                }
                for key, size in sorted(self.remote.items())
            ],
            "IsTruncated": False,
        }

    def put_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        if hasattr(body, "read"):
            payload = body.read()
        else:
            payload = bytes(body)
        self.remote[key] = len(payload)
        self.events.append(("put", key))
        return {}

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        delete = kwargs["Delete"]
        keys = tuple(str(item["Key"]) for item in delete["Objects"])
        for key in keys:
            self.remote.pop(key, None)
            self.modified.pop(key, None)
        self.events.append(("delete", keys))
        return {}


class RetentionTests(unittest.TestCase):
    def test_bc_and_broad_archive_cadence(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        self.assertTrue(keep_frame(now - dt.timedelta(hours=23, minutes=59), now, "bc"))
        self.assertTrue(
            keep_frame(dt.datetime(2026, 7, 18, 10, 30, tzinfo=UTC), now, "bc")
        )
        self.assertFalse(
            keep_frame(dt.datetime(2026, 7, 18, 10, 20, tzinfo=UTC), now, "bc")
        )
        self.assertTrue(
            keep_frame(dt.datetime(2026, 7, 18, 10, 0, tzinfo=UTC), now, "broad")
        )
        self.assertFalse(
            keep_frame(dt.datetime(2026, 7, 18, 10, 30, tzinfo=UTC), now, "broad")
        )
        self.assertFalse(keep_frame(now - dt.timedelta(days=8), now, "bc"))

    def test_bootstrap_selection_applies_archive_cadence_before_download(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        values = [
            now - dt.timedelta(hours=25, minutes=minute)
            for minute in (0, 10, 20, 30, 40, 50)
        ] + [now - dt.timedelta(hours=3, minutes=10)]

        selected = retained_times(values, 48, False, now, "bc")

        self.assertEqual(
            selected,
            [
                now - dt.timedelta(hours=25, minutes=30),
                now - dt.timedelta(hours=25),
                now - dt.timedelta(hours=3, minutes=10),
            ],
        )

    def test_broad_bootstrap_keeps_six_minute_radar_for_first_day(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        recent = sorted(
            now - dt.timedelta(minutes=minute)
            for minute in (6, 12, 18, 24, 30)
        )
        old_hour = now - dt.timedelta(hours=25)
        old_off_hour = old_hour - dt.timedelta(minutes=6)

        self.assertEqual(
            retained_times([old_off_hour, old_hour, *recent], 48, False, now, "broad"),
            [old_hour, *recent],
        )

    def test_latest_only_probe_is_not_removed_by_retention(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        latest = now - dt.timedelta(days=8, minutes=10)
        self.assertEqual(retained_times([latest], 168, True, now, "bc"), [latest])

    def test_ecmwf_is_hourly_for_one_day_then_three_hourly(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        recent_off_cycle = dt.datetime(2026, 7, 19, 13, tzinfo=UTC)
        old_off_cycle = dt.datetime(2026, 7, 19, 11, tzinfo=UTC)
        old_synoptic = dt.datetime(2026, 7, 19, 9, tzinfo=UTC)

        self.assertTrue(
            keep_layer_frame(recent_off_cycle, now, "broad", "ecmwf-hgt500")
        )
        self.assertFalse(
            keep_layer_frame(old_off_cycle, now, "broad", "ecmwf-hgt500")
        )
        self.assertTrue(
            keep_layer_frame(old_synoptic, now, "broad", "ecmwf-hgt500")
        )
        self.assertTrue(
            keep_layer_frame(old_off_cycle, now, "broad", "radar-rain")
        )

    def test_local_prune_thins_old_ecmwf_interpolated_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["north-america"]
            layer = LAYERS["ecmwf-hgt500"]
            now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
            old_off_cycle = dt.datetime(2026, 7, 19, 11, tzinfo=UTC)
            old_synoptic = dt.datetime(2026, 7, 19, 9, tzinfo=UTC)
            for valid in (old_off_cycle, old_synoptic):
                frame = frame_path(root, domain, layer, valid)
                write_png(frame)
                write_metadata(root, domain, layer, valid, frame)

            self.assertEqual(prune(root, now), 1)
            self.assertFalse(frame_path(root, domain, layer, old_off_cycle).exists())
            self.assertTrue(frame_path(root, domain, layer, old_synoptic).exists())


class LightningCleanupTests(unittest.TestCase):
    def test_orphaned_derived_anchors_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            valid = dt.datetime(2026, 7, 20, 23, 40, tzinfo=UTC)
            for layer_id in ("lightning", "radar-rain"):
                layer = LAYERS[layer_id]
                path = frame_path(root, domain, layer, valid)
                write_png(path)
                write_metadata(root, domain, layer, valid, path)

            invalid = valid - dt.timedelta(minutes=6)
            derived = LAYERS["lightning-trail"]
            invalid_frame = frame_path(root, domain, derived, invalid)
            write_png(invalid_frame)
            write_metadata(root, domain, derived, invalid, invalid_frame)

            derive_lightning_trails(root, domain, {}, hours=1)

            self.assertFalse(invalid_frame.exists())
            self.assertFalse(metadata_path(root, domain, derived, invalid).exists())
            self.assertTrue(frame_path(root, domain, derived, valid).exists())
            payload = json.loads(metadata_path(root, domain, derived, valid).read_text())
            self.assertEqual(payload["sourceTimes"]["age0"], "2026-07-20T23:40:00Z")

    def test_archive_metadata_cannot_escape_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                safe_archive_path(Path(temporary) / "output", "../../outside.png")


class ConfigurationTests(unittest.TestCase):
    def test_mutable_frames_are_not_cached_as_immutable(self) -> None:
        self.assertEqual(
            cache_control("frames/bc/daynight/2026/07/20/frame.webp"),
            "public, max-age=60, must-revalidate",
        )
        self.assertNotIn("immutable", cache_control("metadata/bc/daynight/frame.json"))
        self.assertEqual(cache_control("live-edge.json"), "no-cache, max-age=0, must-revalidate")

    def test_versioned_video_assets_are_immutable_with_video_mime(self) -> None:
        for key in (
            "videos/bc-northeast-overlay/raw-visir/live/generation.mp4",
            "video-manifests/bc-northeast-overlay/raw-visir/live/generation.json",
            "video-proxies/bc-northeast-overlay/radar-rain/hash.png",
            "video-static-overlays/bc-northeast-overlay/hash.png",
            "video-segments/shared-bc-full/raw-visir/live/hash.ts",
        ):
            self.assertEqual(cache_control(key), "public, max-age=31536000, immutable")
        self.assertEqual(content_type(Path("satellite.MP4")), "video/mp4")
        self.assertEqual(content_type(Path("satellite.m3u8")), "application/vnd.apple.mpegurl")
        self.assertEqual(content_type(Path("satellite.ts")), "video/mp2t")

    @mock.patch("radarsat.r2.keychain_password")
    def test_environment_precedes_scoped_keychain(self, password: mock.Mock) -> None:
        password.side_effect = lambda service: {
            "radar-sat-r2-account-id": "keychain-account",
            "radar-sat-r2-access-key-id": "keychain-access",
            "radar-sat-r2-secret-access-key": "keychain-secret",
            "radar-sat-r2-bucket": "keychain-bucket",
            "radar-sat-r2-public-base-url": "https://keychain.example",
        }.get(service, "")
        environment = {
            "RADARSAT_R2_ACCOUNT_ID": "environment-account",
            "RADARSAT_R2_ACCESS_KEY_ID": "environment-access",
            "RADARSAT_R2_SECRET_ACCESS_KEY": "environment-secret",
            "RADARSAT_R2_BUCKET": "radar-sat",
            "RADARSAT_R2_PUBLIC_BASE_URL": "https://public.example/",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            config = R2Config.from_environment()
        self.assertEqual(config.account_id, "environment-account")
        self.assertEqual(config.bucket, "radar-sat")
        self.assertEqual(config.public_base_url, "https://public.example")


class PublisherTests(unittest.TestCase):
    def config(self, **updates: object) -> R2Config:
        values: dict[str, object] = {
            "account_id": "account",
            "access_key_id": "access",
            "secret_access_key": "secret",
            "bucket": "radar-sat",
            "warn_bytes": 1_000_000,
            "max_bytes": 2_000_000,
        }
        values.update(updates)
        return R2Config(**values)

    def test_catalog_is_uploaded_after_assets_and_before_expiry_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            root.mkdir()
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expired = "frames/bc/radar-rain/2026/07/10/20260710T2300Z.png"
            fake = FakeR2({expired: 123})
            result = publish(
                root,
                self.config(),
                base / "state.sqlite3",
                base / "publish.json",
                client=fake,
                now=now,
            )

            full_catalog_index = fake.events.index(("put", "catalog.json"))
            catalog_index = fake.events.index(("put", "catalog-index.json"))
            self.assertTrue(all(event[0] == "put" for event in fake.events[:catalog_index]))
            self.assertLess(full_catalog_index, catalog_index)
            self.assertEqual(fake.events[catalog_index + 1][0], "delete")
            self.assertEqual(result["deleted"], 1)
            self.assertTrue(result["catalogLast"])

    def test_catalog_index_keeps_only_the_newest_frame_per_layer(self) -> None:
        catalog = {
            "schemaVersion": 1,
            "generatedAt": "2026-07-20T23:42:00Z",
            "domains": {
                "bc": {
                    "layers": {
                        "radar-rain": {
                            "frames": [
                                {"validTime": "2026-07-20T23:40:00Z", "path": "new.png"},
                                {"validTime": "2026-07-20T23:30:00Z", "path": "old.png"},
                            ]
                        },
                        "empty": {"frames": []},
                    }
                }
            },
            "products": [{"id": "bc", "domain": "bc", "anchorLayer": "radar-rain"}],
            "videoProfiles": {"bc": {"raw-visir": {"live": {"generation": "g"}}}},
        }

        index = json.loads(build_catalog_index(json.dumps(catalog).encode()))

        self.assertEqual(index["catalogMode"], "index")
        self.assertEqual(index["fullCatalogPath"], "catalog.json")
        self.assertEqual(
            index["domains"]["bc"]["layers"]["radar-rain"]["frames"],
            [{"validTime": "2026-07-20T23:40:00Z", "path": "new.png"}],
        )
        self.assertEqual(index["domains"]["bc"]["layers"]["empty"]["frames"], [])
        self.assertIn("videoProfiles", index)

    def test_video_manifest_media_proxies_and_static_overlay_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_video_profile(root)

            objects, payload = discover_objects(root)

            keys = {item.key for item in objects}
            self.assertTrue(expected.issubset(keys))
            published = json.loads(payload)
            self.assertIn("videoProfiles", published)

    def test_live_edge_pointer_is_preserved_by_full_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            (root / "live-edge.json").write_text(json.dumps({
                "schemaVersion": 1,
                "generatedAt": "2026-07-20T23:42:00Z",
                "domains": {},
            }))

            objects, _payload = discover_objects(root)

            self.assertIn("live-edge.json", {item.key for item in objects})

    def test_hls_manifest_playlist_and_segments_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_hls_video_profile(root)

            objects, payload = discover_objects(root)

            self.assertTrue(expected.issubset({item.key for item in objects}))
            self.assertIn("videoProfiles", json.loads(payload))

    def test_default_composite_playlist_and_segments_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_default_composite_profile(root)

            objects, payload = discover_objects(root)

            keys = {item.key for item in objects}
            self.assertTrue(expected.issubset(keys))
            composite_keys = {
                key
                for key in expected
                if "/composite-bc-large-overlay/" in key
            }
            self.assertEqual(len(composite_keys), 2)
            self.assertTrue(composite_keys.issubset(keys))
            self.assertIn("videoProfiles", json.loads(payload))

    def test_default_composite_assets_upload_before_catalog_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            root.mkdir()
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_default_composite_profile(root)
            objects, catalog = discover_objects(root)
            expected_local_bytes = (
                sum(item.size for item in objects)
                + len(catalog)
                + len(build_catalog_index(catalog))
            )
            fake = FakeR2()

            result = publish(
                root,
                self.config(max_bytes=10_000_000),
                base / "state.sqlite3",
                base / "publish.json",
                client=fake,
                now=now,
            )

            catalog_index = fake.events.index(("put", "catalog.json"))
            uploaded_before_catalog = {
                str(event[1]) for event in fake.events[:catalog_index]
            }
            composite_keys = {
                key for key in expected if "/composite-bc-large-overlay/" in key
            }
            self.assertTrue(composite_keys.issubset(uploaded_before_catalog))
            self.assertEqual(result["localBytes"], expected_local_bytes)

    def test_referenced_default_composite_is_protected_from_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            add_default_composite_profile(root)
            objects, _catalog = discover_objects(root)
            desired = {item.key for item in objects}
            composite_keys = {
                key for key in desired if "/composite-bc-large-overlay/" in key
            }
            remote = {key: (root / key).stat().st_size for key in composite_keys}
            modified = {
                key: now - dt.timedelta(days=2) for key in composite_keys
            }

            expired = expired_video_keys(
                remote,
                now,
                desired_keys=desired,
                modified_at=modified,
            )

            self.assertTrue(composite_keys)
            self.assertTrue(composite_keys.isdisjoint(expired))

    def test_incomplete_default_composite_fails_open_to_base_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_default_composite_profile(root)
            composite_segment = next(
                key
                for key in expected
                if key.startswith("video-segments/composite-")
            )
            (root / composite_segment).unlink()

            objects, payload = discover_objects(root)

            keys = {item.key for item in objects}
            published = json.loads(payload)
            self.assertIn("videoProfiles", published)
            self.assertTrue(any(key.startswith("videos/shared-") for key in keys))
            self.assertTrue(any(key.startswith("video-segments/shared-") for key in keys))
            self.assertFalse(
                any("/composite-bc-large-overlay/" in key for key in keys)
            )

    def test_default_composite_rejects_misaligned_viewport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_default_composite_profile(root)
            manifest_relative = next(
                key for key in expected if key.startswith("video-manifests/")
            )
            manifest_path = root / manifest_relative
            manifest = json.loads(manifest_path.read_text())
            manifest["defaultComposite"]["mediaViewport"]["left"] = 0.1
            manifest_path.write_text(json.dumps(manifest))

            objects, payload = discover_objects(root)

            keys = {item.key for item in objects}
            self.assertIn("videoProfiles", json.loads(payload))
            self.assertFalse(
                any("/composite-bc-large-overlay/" in key for key in keys)
            )

    def test_default_composite_rejects_non_pilot_satellite_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            expected = add_default_composite_profile(root)
            manifest_relative = next(
                key for key in expected if key.startswith("video-manifests/")
            )
            manifest = json.loads((root / manifest_relative).read_text())

            self.assertEqual(
                _default_composite_paths(
                    root,
                    "bc-large-overlay",
                    "raw-ir",
                    "live",
                    manifest,
                ),
                [],
            )

    def test_incomplete_video_profile_fails_open_to_image_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            video_keys = add_video_profile(root)
            proxy = next(key for key in video_keys if key.startswith("video-proxies/"))
            (root / proxy).unlink()

            objects, payload = discover_objects(root)

            self.assertNotIn("videoProfiles", json.loads(payload))
            self.assertTrue(any(item.key.startswith("frames/") for item in objects))
            self.assertFalse(any(item.key.startswith("videos/") for item in objects))
            self.assertFalse(any(item.key.startswith("video-manifests/") for item in objects))

    def test_existing_video_filter_retains_last_uploaded_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            previous_generation = "20260720T2320Z-111111111111"
            previous = add_video_profile(root, previous_generation)
            current_generation = "20260720T2340Z-222222222222"
            current = add_video_profile(root, current_generation)

            objects, payload = discover_objects(
                root,
                existing_video_keys=previous,
            )

            published = json.loads(payload)
            pointer = published["videoProfiles"]["bc-northeast-overlay"]["raw-visir"]["live"]
            self.assertEqual(pointer["generation"], previous_generation)
            keys = {item.key for item in objects}
            self.assertTrue(previous.issubset(keys))
            self.assertFalse(
                any(key in keys for key in current.difference(previous))
            )

    def test_video_profile_cannot_escape_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            add_video_profile(root)
            catalog_path = root / "catalog.json"
            catalog = json.loads(catalog_path.read_text())
            pointer = catalog["videoProfiles"]["bc-northeast-overlay"]["raw-visir"]["live"]
            pointer["manifestPath"] = "../outside.json"
            catalog_path.write_text(json.dumps(catalog))

            _objects, payload = discover_objects(root)

            self.assertNotIn("videoProfiles", json.loads(payload))

    def test_video_retention_keeps_newest_three_and_one_hour_grace(self) -> None:
        now = dt.datetime(2026, 7, 21, 12, tzinfo=UTC)
        generations = [
            "20260721T0800Z-000000000000",
            "20260721T0900Z-111111111111",
            "20260721T1000Z-222222222222",
            "20260721T1100Z-333333333333",
            "20260721T1200Z-444444444444",
        ]
        remote: dict[str, int] = {}
        modified: dict[str, dt.datetime] = {}
        for generation in generations:
            for prefix, suffix in (("videos", "mp4"), ("video-manifests", "json")):
                key = f"{prefix}/north-america-overlay/westwx-visir/live/{generation}.{suffix}"
                remote[key] = 1
                modified[key] = now - dt.timedelta(hours=8)
        grace_proxy = "video-proxies/north-america-overlay/radar-rain/aaaaaaaaaaaaaaaa.png"
        old_proxy = "video-proxies/north-america-overlay/radar-rain/bbbbbbbbbbbbbbbb.png"
        desired_proxy = "video-proxies/north-america-overlay/radar-rain/cccccccccccccccc.png"
        remote.update({grace_proxy: 1, old_proxy: 1, desired_proxy: 1})
        modified.update({
            grace_proxy: now - dt.timedelta(minutes=59),
            old_proxy: now - dt.timedelta(hours=1, minutes=1),
            desired_proxy: now - dt.timedelta(days=1),
        })

        active_generation_keys = {
            key for key in remote if generations[-1] in key
        }
        expired = expired_video_keys(
            remote,
            now,
            desired_keys={desired_proxy, *active_generation_keys},
            modified_at=modified,
        )

        self.assertIn(old_proxy, expired)
        self.assertNotIn(grace_proxy, expired)
        self.assertNotIn(desired_proxy, expired)
        for generation in generations[-3:]:
            self.assertFalse(any(generation in key for key in expired))
        for generation in generations[:2]:
            self.assertTrue(any(generation in key for key in expired))

        retired = expired_video_keys(remote, now, modified_at=modified)
        for generation in generations:
            self.assertTrue(any(generation in key for key in retired))

    def test_expired_video_generation_is_deleted_only_after_catalog_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            root.mkdir()
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            current_video_keys = add_video_profile(root)
            generations = [
                "20260720T1800Z-000000000000",
                "20260720T1900Z-111111111111",
                "20260720T2000Z-222222222222",
                "20260720T2100Z-333333333333",
            ]
            remote: dict[str, int] = {}
            modified: dict[str, dt.datetime] = {}
            for generation in generations:
                for prefix, suffix in (("videos", "mp4"), ("video-manifests", "json")):
                    key = f"{prefix}/bc-northeast-overlay/raw-visir/live/{generation}.{suffix}"
                    remote[key] = 1
                    modified[key] = now - dt.timedelta(hours=8)
            for key in current_video_keys:
                remote[key] = (root / key).stat().st_size
                modified[key] = now - dt.timedelta(days=2)
            fake = FakeR2(remote, modified)

            publish(
                root,
                self.config(max_bytes=10_000_000),
                base / "state.sqlite3",
                base / "publish.json",
                client=fake,
                now=now,
            )

            catalog_index = fake.events.index(("put", "catalog.json"))
            delete_index = next(
                index for index, event in enumerate(fake.events) if event[0] == "delete"
            )
            self.assertGreater(delete_index, catalog_index)
            deleted = fake.events[delete_index][1]
            self.assertTrue(
                all(
                    any(generation in key for generation in generations[:2])
                    for key in deleted
                )
            )
            self.assertTrue(current_video_keys.isdisjoint(deleted))

    def test_size_guard_refuses_growth_above_bucket_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            objects, catalog = discover_objects(root)
            with self.assertRaises(PublicationSafetyError):
                size_guard(
                    objects,
                    catalog,
                    {},
                    self.config(warn_bytes=1, max_bytes=10),
                )

    def test_size_guard_allows_recovery_peak_when_expired_objects_restore_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            objects, catalog = discover_objects(root)
            local_bytes = (
                sum(item.size for item in objects)
                + len(catalog)
                + len(build_catalog_index(catalog))
            )
            expired = "frames/bc/radar-rain/2026/07/01/20260701T0000Z.png"
            result = size_guard(
                objects,
                catalog,
                {expired: local_bytes},
                self.config(
                    warn_bytes=1,
                    max_bytes=local_bytes + 1,
                ),
                pending_delete=(expired,),
            )

            self.assertLessEqual(result["projectedBytes"], local_bytes + 1)
            self.assertGreater(result["peakProjectedBytes"], local_bytes + 1)
            self.assertEqual(result["pendingDeleteBytes"], local_bytes)

    def test_only_policy_expired_remote_objects_are_selected(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        remote = {
            "frames/bc/radar-rain/2026/07/20/20260720T1100Z.png": 1,
            "frames/bc/radar-rain/2026/07/18/20260718T1012Z.png": 1,
            "metadata/bc/radar-rain/2026/07/18/20260718T1030Z.json": 1,
            "frames/north-america/ecmwf-hgt500/2026/07/19/20260719T0900Z.png": 1,
            "frames/north-america/ecmwf-hgt500/2026/07/19/20260719T1100Z.png": 1,
            "metadata/north-america/ecmwf-hgt500/2026/07/19/20260719T1100Z.json": 1,
            "static/bc/base-dark.png": 1,
        }
        self.assertEqual(
            expired_remote_keys(remote, now),
            [
                "frames/bc/radar-rain/2026/07/18/20260718T1012Z.png",
                "frames/north-america/ecmwf-hgt500/2026/07/19/20260719T1100Z.png",
                "metadata/north-america/ecmwf-hgt500/2026/07/19/20260719T1100Z.json",
            ],
        )

    def test_catalog_referenced_object_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            root.mkdir()
            old = dt.datetime(2026, 7, 10, 23, 0, tzinfo=UTC)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, old)
            objects, _ = discover_objects(root)
            remote = {item.key: item.size for item in objects}
            fake = FakeR2(remote)
            result = publish(
                root,
                self.config(),
                base / "state.sqlite3",
                base / "publish.json",
                client=fake,
                now=now,
            )
            self.assertEqual(result["deleted"], 0)
            self.assertFalse(any(event[0] == "delete" for event in fake.events))

    def test_whole_frame_recovery_catalog_omits_optional_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            catalog_path = root / "catalog.json"
            catalog = json.loads(catalog_path.read_text())
            frame = catalog["domains"]["bc"]["layers"]["radar-rain"]["frames"][0]
            tile = root / "tiles/bc/radar-rain/tile.webp"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b"tile")
            manifest = root / "tile-manifests/bc/radar-rain/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"files": [tile.relative_to(root).as_posix()]}))
            frame["tiles"] = {
                "manifest": manifest.relative_to(root).as_posix(),
                "template": "tiles/bc/radar-rain/{z}/{x}/{y}.webp",
            }
            catalog_path.write_text(json.dumps(catalog))

            objects, payload = discover_objects(root, whole_frame_only=True)
            published = json.loads(payload)
            published_frame = published["domains"]["bc"]["layers"]["radar-rain"]["frames"][0]

            self.assertNotIn("tiles", published_frame)
            self.assertNotIn(tile.relative_to(root).as_posix(), {item.key for item in objects})
            self.assertNotIn(manifest.relative_to(root).as_posix(), {item.key for item in objects})

    def test_fast_publication_uses_successful_upload_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            root.mkdir()
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)
            video_keys = add_video_profile(root)
            fake = FakeR2()
            state = base / "state.sqlite3"
            status = base / "publish.json"

            first = publish(
                root,
                self.config(),
                state,
                status,
                client=fake,
                now=now,
                fast=True,
            )
            fake.events.clear()
            with mock.patch("radarsat.r2.os.link", wraps=os.link) as link:
                second = publish(
                    root,
                    self.config(),
                    state,
                    status,
                    client=fake,
                    now=now,
                    fast=True,
                )

            self.assertTrue(first["fast"])
            self.assertEqual(first["uploaded"], 3 + len(video_keys))
            self.assertEqual(second["uploaded"], 0)
            link.assert_not_called()
            self.assertEqual(fake.events, [
                ("put", "westwx-catalog.json"),
                ("put", "catalog.json"),
                ("put", "catalog-index.json"),
            ])

    def test_fast_over_cap_publish_refuses_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            root.mkdir()
            now = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
            make_archive(root, now)

            with mock.patch("radarsat.r2.publication_snapshot") as snapshot:
                with self.assertRaises(PublicationSafetyError):
                    publish(
                        root,
                        self.config(warn_bytes=1, max_bytes=10),
                        base / "state.sqlite3",
                        base / "publish.json",
                        client=FakeR2(),
                        now=now,
                        fast=True,
                    )

            snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
