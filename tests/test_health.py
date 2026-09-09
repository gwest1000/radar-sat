from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from radarsat.health import (
    inspect_health,
    inspect_publication,
    recent_layer_source_count,
    storage_breakdown,
)


UTC = dt.timezone.utc


class HealthTests(unittest.TestCase):
    def test_fresh_local_catalog_does_not_hide_stalled_publication(self) -> None:
        now = dt.datetime(2026, 9, 9, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish = self._fixture(root, now, 1_000_000)
            publish.write_text(json.dumps({
                "status": "ok", "updatedAt": "2026-09-09T14:57:00Z",
                "lastCatalogCommitAt": "2026-09-09T14:57:00Z",
                "catalogGeneratedAt": "2026-09-09T14:53:00Z",
            }))
            publish.with_name("publish-progress.json").write_text(json.dumps({
                "status": "running", "stage": "snapshot",
                "stageUpdatedAt": "2026-09-09T15:10:00Z",
                "lastCatalogCommitAt": "2026-09-09T14:57:00Z",
                "catalogGeneratedAt": "2026-09-09T15:59:00Z",
                "lastCommittedCatalogGeneratedAt": "2026-09-09T14:53:00Z",
            }))
            result = inspect_health(root, publish, now=now, storage_budget_seconds=0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["publication"]["catalogGeneratedAt"], "2026-09-09T14:53:00Z")
        self.assertIn("public catalog has not committed for 63 minutes", result["errors"])
        self.assertIn("publisher has made no progress in snapshot for 50 minutes", result["errors"])
        self.assertEqual(result["storage"]["measurement"], "partial")

    def test_recent_commit_of_old_catalog_is_still_unhealthy(self) -> None:
        now = dt.datetime(2026, 9, 9, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            publish = Path(temporary) / "publish.json"
            publish.write_text(json.dumps({
                "status": "ok", "updatedAt": "2026-09-09T15:59:00Z",
                "lastCatalogCommitAt": "2026-09-09T15:59:00Z",
                "catalogGeneratedAt": "2026-09-09T14:53:00Z",
            }))
            summary, errors = inspect_publication(publish, now=now, max_age_minutes=15)
        self.assertEqual(summary["commitAgeMinutes"], 1)
        self.assertEqual(errors, ["published catalog was generated 67 minutes ago"])

    def test_commit_progress_is_visible_before_cleanup_finishes(self) -> None:
        now = dt.datetime(2026, 9, 9, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            publish = Path(temporary) / "publish.json"
            publish.write_text(json.dumps({
                "status": "ok", "updatedAt": "2026-09-09T14:57:00Z",
            }))
            publish.with_name("publish-progress.json").write_text(json.dumps({
                "status": "running", "stage": "cleanup",
                "stageUpdatedAt": "2026-09-09T15:59:00Z",
                "lastCatalogCommitAt": "2026-09-09T15:58:00Z",
                "lastCommittedCatalogGeneratedAt": "2026-09-09T15:57:00Z",
            }))
            summary, errors = inspect_publication(publish, now=now, max_age_minutes=15)
        self.assertEqual(summary["commitAgeMinutes"], 2)
        self.assertEqual(summary["catalogAgeMinutes"], 3)
        self.assertEqual(errors, [])

    def test_failed_or_dry_run_attempt_does_not_replace_acknowledged_commit(self) -> None:
        now = dt.datetime(2026, 9, 9, 16, tzinfo=UTC)
        for status in ("error", "dry-run"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                publish = Path(temporary) / "publish.json"
                publish.write_text(json.dumps({
                    "status": status, "updatedAt": "2026-09-09T15:59:00Z",
                }))
                publish.with_name("publish-progress.json").write_text(json.dumps({
                    "status": "ok", "stage": "complete",
                    "stageUpdatedAt": "2026-09-09T14:57:00Z",
                    "lastCatalogCommitAt": "2026-09-09T14:57:00Z",
                    "lastCommittedCatalogGeneratedAt": "2026-09-09T14:53:00Z",
                }))
                summary, errors = inspect_publication(publish, now=now, max_age_minutes=15)
                self.assertEqual(summary["lastCatalogCommitAt"], "2026-09-09T14:57:00Z")
                self.assertEqual(summary["commitAgeMinutes"], 63)
                self.assertEqual(summary["catalogAgeMinutes"], 67)
                self.assertIn("public catalog has not committed for 63 minutes", errors)
                self.assertIn("published catalog was generated 67 minutes ago", errors)

    def test_storage_budget_keeps_dated_complete_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "frames").mkdir()
            (root / "frames" / "a.png").write_bytes(b"abc")
            complete = storage_breakdown(root)
            with mock.patch("radarsat.health.os.scandir") as scan:
                cached = storage_breakdown(root, max_seconds=0, previous=complete)
            scan.assert_not_called()
        self.assertEqual(complete["totalBytes"], 3)
        self.assertEqual(cached["totalBytes"], 3)
        self.assertEqual(cached["measuredAt"], complete["measuredAt"])
        self.assertEqual(cached["measurement"], "cached")
        self.assertFalse(cached["scanComplete"])

    def test_storage_does_not_follow_symlink_outside_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            root.mkdir()
            external = Path(temporary) / "external"
            external.mkdir()
            (external / "large.bin").write_bytes(b"x" * 100)
            (root / "linked").symlink_to(external)
            result = storage_breakdown(root)
        self.assertEqual(result["totalBytes"], 0)
        self.assertTrue(result["scanComplete"])

    def test_partial_scan_overrun_is_not_hidden_by_cached_size(self) -> None:
        now = dt.datetime(2026, 9, 9, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish = self._fixture(root, now, 1_000_000)
            with mock.patch("radarsat.health.storage_breakdown", return_value={
                "totalBytes": 10_000_000_000, "partialBytes": 31_000_000_000,
                "scanComplete": False, "measurement": "cached", "measuredAt": "earlier",
            }):
                result = inspect_health(root, publish, now=now)
        self.assertTrue(any("at least 31.00 GB" in value for value in result["errors"]))

    def test_recent_layer_source_count_uses_latest_hour(self) -> None:
        base = dt.datetime(2026, 9, 3, 20, tzinfo=UTC)
        frames = [
            {
                "validTime": (base + dt.timedelta(minutes=index * 6)).isoformat(),
                "layerSourceTimes": {
                    "radar-rain": (base + dt.timedelta(minutes=index * 6)).isoformat(),
                    "eccc-geocolor": (base + dt.timedelta(minutes=(index // 2) * 10)).isoformat(),
                },
            }
            for index in range(21)
        ]
        self.assertEqual(recent_layer_source_count(frames, "radar-rain"), 11)
        self.assertEqual(recent_layer_source_count(frames, "eccc-geocolor"), 6)

    def _fixture(self, root: Path, now: dt.datetime, projected: int) -> Path:
        status = root / "status"
        status.mkdir(parents=True)
        stamp = now.isoformat().replace("+00:00", "Z")
        (status / "ingest.json").write_text(json.dumps({"status": "ok", "updatedAt": stamp}))
        layers = {
            layer_id: {
                "maxAgeMinutes": 40,
                "frames": [{"validTime": stamp}],
            }
            for layer_id in ("eccc-geocolor", "radar-rain", "ptype", "lightning")
        }
        (root / "catalog.json").write_text(json.dumps({
            "generatedAt": stamp,
            "domains": {"bc": {"layers": layers}},
            "videoProfiles": {},
        }))
        publish = root / "publish.json"
        publish.write_text(json.dumps({
            "status": "ok",
            "updatedAt": stamp,
            "projectedBytes": projected,
        }))
        return publish

    def test_msc_primary_and_r2_warning_are_reported(self) -> None:
        now = dt.datetime(2026, 8, 20, 23, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish = self._fixture(root, now, 9_100_000_000)
            result = inspect_health(root, publish, now=now)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frameCounts"]["eccc-geocolor"], 1)
        self.assertTrue(any("projected R2 storage" in value for value in result["warnings"]))
        self.assertIn("bc-large-overlay", result["videoCoverage"])

    def test_r2_guard_is_a_health_error(self) -> None:
        now = dt.datetime(2026, 8, 20, 23, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish = self._fixture(root, now, 9_900_000_000)
            result = inspect_health(root, publish, now=now)

        self.assertEqual(result["status"], "error")
        self.assertTrue(any("projected R2 storage" in value for value in result["errors"]))

    def test_storage_breakdown_and_free_disk_alarm_are_reported(self) -> None:
        now = dt.datetime(2026, 8, 20, 23, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish = self._fixture(root, now, 1_000_000)
            for relative, size in (
                ("composite-frame-cache/a.png", 3),
                ("video-segments/a.ts", 5),
                ("frames/a.png", 7),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size)
            with mock.patch("radarsat.health.shutil.disk_usage") as disk_usage:
                disk_usage.return_value = mock.Mock(
                    total=1_000_000_000_000,
                    used=920_000_000_000,
                    free=80_000_000_000,
                )
                result = inspect_health(
                    root,
                    publish,
                    now=now,
                    disk_warn_free_bytes=200_000_000_000,
                    disk_min_free_bytes=100_000_000_000,
                )

        self.assertEqual(result["storage"]["compositeCacheBytes"], 3)
        self.assertEqual(result["storage"]["videoSegmentBytes"], 5)
        self.assertEqual(result["storage"]["sourceFrameBytes"], 7)
        self.assertTrue(any("disk has 80.0 GB free" in value for value in result["errors"]))

    def test_exact_sidecar_coverage_is_reported_from_composite_profiles(self) -> None:
        now = dt.datetime(2026, 8, 20, 23, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publish = self._fixture(root, now, 1_000_000)
            catalog_path = root / "catalog.json"
            catalog = json.loads(catalog_path.read_text())
            catalog["products"] = [{
                "id": "bc-large-overlay",
                "domain": "bc",
            }]
            catalog["domains"]["bc"]["layers"]["eccc-geocolor"]["frames"][0][
                "sourceValidTime"
            ] = now.isoformat().replace("+00:00", "Z")
            pointers = []
            for preset in (
                "operational-default-v1",
                "weather-smoke-core-v1",
                "weather-core-v1",
            ):
                manifest = root / "composite-manifests" / f"{preset}.json"
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(json.dumps({
                    **(
                        {
                            "compositeKind": "hybrid-prefix",
                            "renditionPolicy": "high-only",
                            "renditions": [{"id": "high"}],
                            "proxies": {},
                        }
                        if preset in {"weather-smoke-core-v1", "weather-core-v1"}
                        else {}
                    ),
                    "frames": [
                        {"sourceValidTime": "2026-08-20T22:40:00Z"},
                        {"sourceValidTime": "2026-08-20T23:00:00Z"},
                    ]
                }))
                pointers.append({
                    "presetId": preset,
                    "rangeHours": 3,
                    "generation": f"20260820T2300Z-{preset[-4:]:0>12}",
                    "manifestPath": manifest.relative_to(root).as_posix(),
                    "endSourceTime": "2026-08-20T23:00:00Z",
                })
            catalog["compositeProfiles"] = {
                "bc-large-overlay": {
                    "eccc-geocolor": {"live": pointers},
                }
            }
            catalog_path.write_text(json.dumps(catalog))

            result = inspect_health(root, publish, now=now)

        exact = result["videoCoverage"]["bc-large-overlay"]["exact"]["3h"]
        self.assertEqual(exact["operational-default-v1"]["frames"], 2)
        hybrid = result["videoCoverage"]["bc-large-overlay"]["hybrid"]["3h"]
        self.assertEqual(hybrid["weather-smoke-core-v1"]["frames"], 2)
        self.assertEqual(hybrid["weather-core-v1"]["frames"], 2)
        self.assertFalse(any(
            "bc-large-overlay/3h" in warning for warning in result["warnings"]
        ))


if __name__ == "__main__":
    unittest.main()
