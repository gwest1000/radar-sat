from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from radarsat.health import inspect_health


UTC = dt.timezone.utc


class HealthTests(unittest.TestCase):
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
                "operational-core-v1",
                "weather-smoke-core-v1",
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
                        if preset == "weather-smoke-core-v1"
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
        self.assertEqual(exact["operational-core-v1"]["frames"], 2)
        hybrid = result["videoCoverage"]["bc-large-overlay"]["hybrid"]["3h"]
        self.assertEqual(hybrid["weather-smoke-core-v1"]["frames"], 2)
        self.assertFalse(any(
            "bc-large-overlay/3h" in warning for warning in result["warnings"]
        ))


if __name__ == "__main__":
    unittest.main()
