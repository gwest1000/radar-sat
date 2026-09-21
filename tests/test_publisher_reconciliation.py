from __future__ import annotations

import contextlib
import datetime as dt
import errno
import io
import json
import shutil
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from radarsat.config import DOMAINS, LAYERS
from radarsat.pipeline import frame_path, metadata_path, write_metadata
from radarsat.r2 import (
    LocalObject, PublicationProgress, PublishState, R2Config, _clone_snapshot,
    discover_objects, publication_snapshot, publish,
)


class MemoryR2:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.events: list[tuple[str, str]] = []
        self.fail_catalog = False

    def list_objects_v2(self, **kwargs):
        self.events.append(("inventory", ""))
        return {"Contents": [
            {"Key": key, "Size": len(value)} for key, value in self.objects.items()
        ], "IsTruncated": False}

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if self.fail_catalog and key == "catalog-index.json":
            raise RuntimeError("catalog commit failed")
        body = kwargs["Body"]
        self.objects[key] = body.read() if hasattr(body, "read") else bytes(body)
        self.events.append(("put", key))
        return {}


class PublisherReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = (base / "output").resolve()
        self.state = base / "state" / "r2.sqlite3"
        self.status = base / "status" / "publish.json"
        self.now = dt.datetime(2026, 9, 9, 16, tzinfo=dt.timezone.utc)
        domain, layer = DOMAINS["bc"], LAYERS["radar-rain"]
        self.frame = frame_path(self.root, domain, layer, self.now)
        self.frame.parent.mkdir(parents=True)
        Image.new("RGBA", (8, 6)).save(self.frame)
        write_metadata(self.root, domain, layer, self.now, self.frame)
        self.static = self.root / "static" / "bc" / "base-dark.png"
        self.static.parent.mkdir(parents=True)
        Image.new("RGBA", (8, 6)).save(self.static)
        self.catalog = {
            "schemaVersion": 1,
            "generatedAt": "2026-09-09T16:00:00Z",
            "domains": {"bc": {
                "layers": {"radar-rain": {"frames": [json.loads(
                    metadata_path(self.root, domain, layer, self.now).read_text()
                )]}},
                "staticLayers": {"base-dark": {"path": "static/bc/base-dark.png"}},
            }},
            "legends": {},
        }
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        self.client = MemoryR2()
        self.config = R2Config("account", "access", "secret")
        self.run_publish()
        self.client.events.clear()

    def run_publish(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return publish(
                self.root, self.config, self.state, self.status,
                client=self.client, sync_delete=False, now=self.now,
            )

    def test_reconcile_does_not_copy_already_published_archive_across_disks(self):
        with mock.patch("radarsat.r2.os.link") as link, mock.patch(
            "radarsat.r2.shutil.copy2"
        ) as copy:
            result = self.run_publish()
        link.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(result["uploaded"], 0)
        self.assertGreater(result["unchanged"], 0)
        self.assertEqual(self.client.events[0], ("inventory", ""))
        self.assertEqual(result["catalogUploads"], 0)

    def test_snapshot_on_source_disk_preserves_mutable_content_and_cleans_legacy_layout(self):
        legacy = self.state.parent / "r2-publish-snapshot-interrupted"
        legacy.mkdir()
        (legacy / "old-copy").write_bytes(b"old")
        video = self.root / "videos" / "immutable.mp4"
        video.parent.mkdir()
        video.write_bytes(b"immutable video")
        assets, catalog = discover_objects(self.root)
        assets.append(LocalObject("videos/immutable.mp4", video, video.stat().st_size, video.stat().st_mtime_ns))
        original_frame = self.frame.read_bytes()
        snapshot, objects, _ = publication_snapshot(
            self.root, self.state, whole_frame_only=True, minimum_valid_time=None,
            initial_discovery=(assets, catalog),
        )
        self.assertEqual(snapshot.parent, self.root.parent / ".output-r2-publish-snapshots")
        self.assertEqual(snapshot.stat().st_dev, self.root.stat().st_dev)
        self.assertFalse(legacy.exists())
        self.frame.write_bytes(b"corrected in place")
        self.assertEqual((snapshot / self.frame.relative_to(self.root)).read_bytes(), original_frame)
        self.assertNotEqual((snapshot / self.frame.relative_to(self.root)).stat().st_ino, self.frame.stat().st_ino)
        self.assertEqual((snapshot / "videos/immutable.mp4").stat().st_ino, video.stat().st_ino)
        self.assertGreaterEqual(video.stat().st_nlink, 2)

    @unittest.skipUnless(sys.platform == "darwin", "APFS clonefile is macOS-specific")
    def test_apfs_clone_keeps_bytes_when_original_is_overwritten(self):
        destination = self.root / "clone-test.png"
        original = self.frame.read_bytes()
        if not _clone_snapshot(self.frame, destination):
            self.skipTest("temporary filesystem does not support APFS clones")
        self.frame.write_bytes(b"replacement")
        self.assertEqual(destination.read_bytes(), original)
        self.assertNotEqual(destination.stat().st_ino, self.frame.stat().st_ino)

    def test_read_only_source_snapshot_locations_fall_back_to_state_disk(self):
        source_directories = {
            self.root.parent / ".output-r2-publish-snapshots",
            self.root / ".r2-publish-snapshots",
        }
        mkdir = Path.mkdir

        def writable_state_only(path, *args, **kwargs):
            if path in source_directories:
                raise PermissionError("source archive is read-only")
            return mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", writable_state_only):
            snapshot, objects, _ = publication_snapshot(
                self.root, self.state, whole_frame_only=True, minimum_valid_time=None,
            )
        self.assertEqual(snapshot.parent, self.state.parent)
        self.assertTrue(all(item.path.exists() for item in objects))

    def test_immutable_video_cross_device_link_uses_independent_copy(self):
        video = self.root / "videos" / "immutable.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video data")
        item = LocalObject("videos/immutable.mp4", video, video.stat().st_size, video.stat().st_mtime_ns)
        with mock.patch("radarsat.r2.os.link", side_effect=OSError(errno.EXDEV, "cross-device")), mock.patch(
            "radarsat.r2.shutil.copy2", wraps=shutil.copy2
        ) as copy:
            snapshot, _, _ = publication_snapshot(
                self.root, self.state, whole_frame_only=True, minimum_valid_time=None,
                initial_discovery=([item], b"{}"),
            )
        self.assertEqual(copy.call_count, 1)
        self.assertEqual((snapshot / item.key).read_bytes(), video.read_bytes())
        self.assertNotEqual((snapshot / item.key).stat().st_ino, video.stat().st_ino)

    def test_missing_remote_asset_is_copied_and_repaired_despite_local_pruning(self):
        key = self.frame.relative_to(self.root).as_posix()
        expected = self.client.objects.pop(key)
        copy2 = shutil.copy2
        copied = []

        def copy_then_prune(source, destination):
            self.assertEqual(self.client.events[0], ("inventory", ""))
            copy2(source, destination)
            copied.append(source.relative_to(self.root).as_posix())
            source.unlink()

        with mock.patch("radarsat.r2._clone_snapshot", return_value=False), mock.patch(
            "radarsat.r2.shutil.copy2", side_effect=copy_then_prune
        ):
            result = self.run_publish()
        self.assertEqual(copied, [key])
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(self.client.objects[key], expected)
        self.assertFalse(self.frame.exists())
        self.assertEqual(result["catalogUploads"], 0)

    def test_remote_wrong_size_and_changed_local_assets_are_repaired(self):
        remote_key = self.frame.relative_to(self.root).as_posix()
        local_key = self.static.relative_to(self.root).as_posix()
        expected_frame = self.client.objects[remote_key]
        self.client.objects[remote_key] = b"truncated"
        self.static.write_bytes(self.static.read_bytes() + b"new revision")
        with mock.patch("radarsat.r2._clone_snapshot", return_value=False), mock.patch(
            "radarsat.r2.shutil.copy2", wraps=shutil.copy2
        ) as copy:
            result = self.run_publish()
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(copy.call_count, 2)
        self.assertEqual(self.client.objects[remote_key], expected_frame)
        self.assertEqual(self.client.objects[local_key], self.static.read_bytes())

    def test_progress_preserves_last_committed_generation_when_new_commit_fails(self):
        committed = json.loads(self.status.read_text())
        self.catalog["generatedAt"] = "2026-09-09T16:10:00Z"
        self.catalog["domains"]["bc"]["layers"]["radar-rain"]["frames"][0]["fetchedAt"] = "2026-09-09T16:09:00Z"
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        self.client.fail_catalog = True
        with mock.patch("radarsat.r2.retry", side_effect=lambda operation, *args: operation()):
            with self.assertRaisesRegex(RuntimeError, "catalog commit failed"):
                self.run_publish()
        progress_path = self.status.with_name("publish-progress.json")
        progress = json.loads(progress_path.read_text())
        self.assertEqual(progress["status"], "error")
        self.assertEqual(progress["catalogGeneratedAt"], "2026-09-09T16:10:00Z")
        self.assertEqual(progress["lastCatalogCommitAt"], committed["lastCatalogCommitAt"])
        self.assertEqual(progress["lastCommittedCatalogGeneratedAt"], "2026-09-09T16:00:00Z")
        self.status.write_text(json.dumps({"status": "error", "updatedAt": "2026-09-09T16:11:00Z"}))
        next_attempt = PublicationProgress(self.status, fast=True)
        self.assertEqual(next_attempt.values["lastCatalogCommitAt"], committed["lastCatalogCommitAt"])
        self.assertEqual(next_attempt.values["lastCommittedCatalogGeneratedAt"], "2026-09-09T16:00:00Z")

    def test_interrupted_catalog_handoff_protects_both_generations_then_retires_old(self):
        old = {"composite-manifests/old.json", "video-segments/old.ts"}
        new = {"composite-manifests/new.json", "video-segments/new.ts"}
        state = PublishState(self.state, "account/radar-sat")
        state.protect_catalog(old, committed=True, now=self.now)
        state.protect_catalog(new)
        state.close()
        # Simulate interruption after a pointer PUT, before its local commit
        # record: either generation may be public and neither may be deleted.
        state = PublishState(self.state, "account/radar-sat")
        self.addCleanup(state.close)
        replacement_time = self.now + dt.timedelta(hours=1)
        self.assertEqual(state.deletable_keys(old | new, replacement_time), [])
        state.protect_catalog(new, committed=True, now=replacement_time)
        self.assertEqual(state.deletable_keys(old | new, replacement_time + dt.timedelta(minutes=14)), [])
        self.assertEqual(set(state.deletable_keys(old | new, replacement_time + dt.timedelta(minutes=16))), old)


if __name__ == "__main__":
    unittest.main()
