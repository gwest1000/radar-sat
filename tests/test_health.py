from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from radarsat.health import inspect_health, recent_layer_source_count


UTC = dt.timezone.utc


class HealthTests(unittest.TestCase):
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
