from __future__ import annotations

import datetime as dt
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
    retained_times,
    safe_archive_path,
    write_metadata,
)
from radarsat.r2 import (
    PublicationSafetyError,
    R2Config,
    cache_control,
    discover_objects,
    expired_remote_keys,
    publish,
    size_guard,
)
from radarsat.retention import keep_frame


UTC = dt.timezone.utc


def write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 6), (0, 0, 0, 0)).save(path, "PNG")


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


class FakeR2:
    def __init__(self, remote: dict[str, int] | None = None) -> None:
        self.remote = dict(remote or {})
        self.events: list[tuple[str, str | tuple[str, ...]]] = []

    def list_objects_v2(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Contents": [
                {"Key": key, "Size": size} for key, size in sorted(self.remote.items())
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

    def test_latest_only_probe_is_not_removed_by_retention(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        latest = now - dt.timedelta(days=8, minutes=10)
        self.assertEqual(retained_times([latest], 168, True, now, "bc"), [latest])


class LightningCleanupTests(unittest.TestCase):
    def test_orphaned_derived_anchors_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["bc"]
            valid = dt.datetime(2026, 7, 20, 23, 42, tzinfo=UTC)
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
            self.assertEqual(payload["sourceTimes"]["age0"], "2026-07-20T23:42:00Z")

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

            catalog_index = fake.events.index(("put", "catalog.json"))
            self.assertTrue(all(event[0] == "put" for event in fake.events[:catalog_index]))
            self.assertEqual(fake.events[catalog_index + 1][0], "delete")
            self.assertEqual(result["deleted"], 1)
            self.assertTrue(result["catalogLast"])

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

    def test_only_policy_expired_remote_objects_are_selected(self) -> None:
        now = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
        remote = {
            "frames/bc/radar-rain/2026/07/20/20260720T1100Z.png": 1,
            "frames/bc/radar-rain/2026/07/18/20260718T1012Z.png": 1,
            "metadata/bc/radar-rain/2026/07/18/20260718T1030Z.json": 1,
            "static/bc/base-dark.png": 1,
        }
        self.assertEqual(
            expired_remote_keys(remote, now),
            ["frames/bc/radar-rain/2026/07/18/20260718T1012Z.png"],
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


if __name__ == "__main__":
    unittest.main()
