from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
RUN_CYCLE = PROJECT / "scripts" / "ops" / "run_cycle.zsh"
RUN_VIDEO_SCHEDULER = PROJECT / "scripts" / "ops" / "run_video_scheduler.zsh"
CONFIGURE_R2 = PROJECT / "scripts" / "configure_r2.py"


class OpsScriptTests(unittest.TestCase):
    def _video_scheduler_fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        output = root / "output"
        output.mkdir(parents=True)
        products = [
            ("bc-large-overlay", "bc", "eccc-geocolor"),
            ("bc-small-overlay", "bc", "eccc-geocolor"),
            ("bc-southwest-overlay", "bc", "eccc-geocolor"),
            ("bc-southeast-overlay", "bc", "eccc-geocolor"),
            ("bc-northeast-overlay", "bc", "eccc-geocolor"),
            ("bc-south-coast-overlay", "bc", "eccc-geocolor"),
            ("pacific-wna-overlay", "north-pacific", "raw-visir"),
            ("north-america-overlay", "north-america", "westwx-visir"),
            ("north-pacific-overlay", "north-pacific", "raw-visir"),
        ]
        domains: dict[str, dict[str, object]] = {}
        for _product, domain, layer in products:
            domains.setdefault(domain, {
                "layers": {},
                "staticLayers": {
                    "base-dark": {
                        "path": f"static/{domain}/base-dark.png",
                        "revision": "base-v1",
                    },
                    "boundaries": {
                        "path": f"static/{domain}/boundaries.png",
                        "revision": "boundaries-v1",
                    },
                    "transmission-lines": {
                        "path": f"static/{domain}/transmission-lines.png",
                        "revision": "transmission-v1",
                    },
                    "watersheds": {
                        "path": f"static/{domain}/watersheds.png",
                        "revision": "watersheds-v1",
                    },
                },
            })
            domains[domain]["layers"][layer] = {
                "frames": [{
                    "validTime": "2026-08-21T12:00:00Z",
                    "sourceValidTime": "2026-08-21T12:00:20Z",
                    "fetchedAt": "2026-08-21T12:08:00Z",
                    "path": f"frames/{domain}/{layer}/latest.webp",
                }]
            }
        (output / "catalog.json").write_text(json.dumps({
            "products": [
                {
                    "id": product,
                    "domain": domain,
                    "anchorLayer": layer,
                    "layers": [
                        {
                            "id": layer,
                            "choiceGroup": "satellite",
                            "defaultEnabled": True,
                        },
                        {"id": "radar-coverage", "enabledWith": "radar-rain"},
                        {"id": "radar-rain", "optional": True},
                    ],
                }
                for product, domain, layer in products
            ],
            "domains": domains,
        }))
        calls = root / "video-calls.log"
        driver = root / "fake-video-driver.py"
        driver.write_text(
            "import os, sys, time\n"
            "import signal\n"
            "arguments = ' '.join(sys.argv[1:])\n"
            "with open(os.environ['RADARSAT_TEST_CALLS'], 'a') as f:\n"
            "    f.write('driver ' + arguments + '\\n')\n"
            "sleep_track = os.environ.get('RADARSAT_TEST_SLEEP_TRACK')\n"
            "if sleep_track and ('--track ' + sleep_track) in arguments:\n"
            "    if os.environ.get('RADARSAT_TEST_IGNORE_TERM') == '1':\n"
            "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    with open(os.environ['RADARSAT_TEST_CHILD_PID'], 'w') as f:\n"
            "        f.write(str(os.getpid()))\n"
            "    time.sleep(float(os.environ.get('RADARSAT_TEST_SLEEP_SECONDS', '30')))\n"
            "if os.environ.get('RADARSAT_TEST_FAIL_RANGE') and "
            "('--range-hours ' + os.environ['RADARSAT_TEST_FAIL_RANGE']) in arguments:\n"
            "    raise SystemExit(1)\n"
            "if os.environ.get('RADARSAT_TEST_FAIL_PRODUCT') and "
            "('--product ' + os.environ['RADARSAT_TEST_FAIL_PRODUCT']) in arguments:\n"
            "    raise SystemExit(1)\n"
        )
        publisher = root / "fake-publisher.zsh"
        publisher.write_text(
            "#!/bin/zsh\n"
            'print -r -- "publish $*" >> "${RADARSAT_TEST_CALLS}"\n'
            'if [[ "${RADARSAT_TEST_SLEEP_PUBLISHER:-0}" == "1" ]]; then\n'
            '  [[ "${RADARSAT_TEST_IGNORE_TERM:-0}" == "1" ]] && trap "" TERM\n'
            '  print -r -- "$$" > "${RADARSAT_TEST_CHILD_PID}"\n'
            '  sleep "${RADARSAT_TEST_SLEEP_SECONDS:-30}"\n'
            'fi\n'
            'if [[ "${RADARSAT_TEST_FAIL_PUBLISH_ONCE:-0}" == "1" '
            '&& ! -e "${RADARSAT_TEST_PUBLISH_MARKER}" ]]; then\n'
            '  : > "${RADARSAT_TEST_PUBLISH_MARKER}"\n'
            "  exit 9\n"
            "fi\n"
        )
        publisher.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "RADARSAT_PYTHON": sys.executable,
            "RADARSAT_STATE_ROOT": str(root / "state"),
            "RADARSAT_OUTPUT_ROOT": str(output),
            "RADARSAT_ENV_FILE": str(root / "missing.env"),
            "RADARSAT_VIDEO_ENABLED": "1",
            "RADARSAT_HYBRID_CORE_ENABLED": "0",
            "RADARSAT_COMPOSITE_VIDEO_BUILDER": str(driver),
            "RADARSAT_LEGACY_VIDEO_BUILDER": str(driver),
            "RADARSAT_VIDEO_CATALOG_WRITER": str(driver),
            "RADARSAT_VIDEO_PUBLISHER": str(publisher),
            "RADARSAT_TEST_CALLS": str(calls),
            "RADARSAT_TEST_PUBLISH_MARKER": str(root / "publish-failed"),
            "RADARSAT_TEST_CHILD_PID": str(root / "video-child.pid"),
            "RADARSAT_VIDEO_PRUNE_INTERVAL_SECONDS": "3600",
        })
        return environment, calls

    def test_video_scheduler_runs_hybrid_core_after_exact_work_for_pilot_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            environment["RADARSAT_HYBRID_CORE_ENABLED"] = "1"

            result = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)],
                cwd=PROJECT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = calls.read_text().splitlines()
            exact_calls = [
                (index, line)
                for index, line in enumerate(lines)
                if "--range-hours " in line
                and "--preset " not in line
            ]
            hybrid_calls = [
                (index, line)
                for index, line in enumerate(lines)
                if "--preset weather-core-v1" in line
            ]
            self.assertTrue(exact_calls)
            self.assertEqual(len(hybrid_calls), 1)
            self.assertGreater(hybrid_calls[0][0], max(index for index, _ in exact_calls))
            self.assertIn("--product bc-large-overlay", hybrid_calls[0][1])
            self.assertIn("--range-hours 3", hybrid_calls[0][1])
            self.assertTrue(all("--preset " not in line for _, line in exact_calls))

    def test_video_scheduler_prioritizes_ranges_coalesces_and_prunes_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)],
                cwd=PROJECT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            lines = calls.read_text().splitlines()
            ranges = [
                int(line.split("--range-hours ", 1)[1].split()[0])
                for line in lines
                if "--range-hours " in line
            ]
            range_batches = [
                value for index, value in enumerate(ranges)
                if index == 0 or value != ranges[index - 1]
            ]
            self.assertEqual(range_batches, [3, 6, 12, 24])
            archive = next(index for index, line in enumerate(lines) if "--track archive" in line)
            self.assertGreater(archive, max(
                index for index, line in enumerate(lines) if "--range-hours " in line
            ))
            first_prune = next(index for index, line in enumerate(lines) if "--prune-cache-only" in line)
            self.assertGreater(first_prune, archive)
            shared_prune = next(
                index for index, line in enumerate(lines)
                if "--prune-shared-only" in line
            )
            self.assertGreater(shared_prune, first_prune)
            self.assertEqual(sum(line.startswith("publish ") for line in lines), 5)

            before_lines = calls.read_text().splitlines()
            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)],
                cwd=PROJECT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            after_lines = calls.read_text().splitlines()
            # Exact work coalesces completely. One additional low-priority
            # archive product rotates through on each scheduler invocation.
            self.assertEqual(
                sum("--range-hours " in line for line in after_lines),
                sum("--range-hours " in line for line in before_lines),
            )
            self.assertEqual(
                sum("--track archive" in line for line in after_lines),
                sum("--track archive" in line for line in before_lines) + 1,
            )

    def test_video_scheduler_archive_uses_public_layer_per_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            for _ in range(4):
                result = subprocess.run(
                    ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)],
                    cwd=PROJECT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            archive_calls = [
                line for line in calls.read_text().splitlines()
                if "--track archive" in line
            ]
            expected = [
                ("bc-large-overlay", "eccc-geocolor"),
                ("pacific-wna-overlay", "raw-visir"),
                ("north-america-overlay", "westwx-visir"),
                ("north-pacific-overlay", "raw-visir"),
            ]
            self.assertEqual(len(archive_calls), len(expected))
            for call, (product, layer) in zip(archive_calls, expected, strict=True):
                self.assertIn(f"--product {product}", call)
                self.assertIn(f"--layer {layer}", call)
                self.assertEqual(call.count("--layer "), 1)

    def test_video_scheduler_retries_dirty_publish_without_rebuilding_completed_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            environment["RADARSAT_TEST_FAIL_PUBLISH_ONCE"] = "1"
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 1)
            first_lines = calls.read_text().splitlines()
            self.assertEqual(sum("--range-hours 3" in line for line in first_lines), 6)

            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            all_lines = calls.read_text().splitlines()
            self.assertEqual(sum("--range-hours 3" in line for line in all_lines), 6)
            self.assertGreaterEqual(sum(line.startswith("publish ") for line in all_lines), 2)

    def test_video_scheduler_never_overlaps_a_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            lock = root / "state" / "run" / "video-worker.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{os.getpid()}\n")
            result = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already running", result.stdout)
            self.assertFalse(calls.exists())
            self.assertTrue(lock.exists())

    def test_video_scheduler_retries_only_the_failed_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            environment.update({
                "RADARSAT_TEST_FAIL_PRODUCT": "bc-northeast-overlay",
                "RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS": "0",
            })
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 1)
            environment.pop("RADARSAT_TEST_FAIL_PRODUCT")
            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            range_three = [
                line for line in calls.read_text().splitlines()
                if "--range-hours 3" in line
            ]
            self.assertEqual(len(range_three), 7)
            self.assertEqual(
                sum("--product bc-northeast-overlay" in line for line in range_three),
                2,
            )

    def test_video_scheduler_fingerprint_rebuilds_when_overlay_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            before = calls.read_text().splitlines()

            catalog_path = root / "output" / "catalog.json"
            catalog = json.loads(catalog_path.read_text())
            catalog["domains"]["north-america"]["layers"]["radar-rain"] = {
                "maxAgeMinutes": 20,
                "frames": [{
                    "validTime": "2026-08-21T12:00:00Z",
                    "sourceValidTime": "2026-08-21T12:00:00Z",
                    "fetchedAt": "2026-08-21T12:09:00Z",
                    "path": "frames/north-america/radar-rain/latest.png",
                }],
            }
            # Reformatting must not matter, while the new operational radar
            # input itself must change the canonical product fingerprint.
            catalog_path.write_text(json.dumps(catalog, indent=3, sort_keys=True))
            environment["RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS"] = "0"
            scheduler_state = root / "state" / "state" / "video-scheduler"
            for hours in (12, 24):
                (scheduler_state / (
                    f"exact-{hours}-north-america-overlay_.success-epoch"
                )).write_text("0\n")

            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            after = calls.read_text().splitlines()
            for hours in (12, 24):
                self.assertEqual(
                    sum(
                        f"--range-hours {hours}" in line
                        and "--product north-america-overlay" in line
                        for line in after
                    ),
                    sum(
                        f"--range-hours {hours}" in line
                        and "--product north-america-overlay" in line
                        for line in before
                    ) + 1,
                )

    def test_video_scheduler_fingerprint_is_range_window_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            catalog_path = root / "output" / "catalog.json"
            catalog = json.loads(catalog_path.read_text())
            catalog["domains"]["bc"]["layers"]["radar-rain"] = {
                "maxAgeMinutes": 20,
                "frames": [
                    {
                        "validTime": "2026-08-21T08:00:00Z",
                        "fetchedAt": "2026-08-21T08:05:00Z",
                        "path": "frames/bc/radar-rain/old-v1.png",
                    },
                    {
                        "validTime": "2026-08-21T12:00:00Z",
                        "fetchedAt": "2026-08-21T12:05:00Z",
                        "path": "frames/bc/radar-rain/current.png",
                    },
                ],
            }
            catalog_path.write_text(json.dumps(catalog))
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            before = calls.read_text().splitlines()

            catalog = json.loads(catalog_path.read_text())
            old_frame = catalog["domains"]["bc"]["layers"]["radar-rain"]["frames"][0]
            old_frame["fetchedAt"] = "2026-08-21T12:10:00Z"
            old_frame["path"] = "frames/bc/radar-rain/old-corrected.png"
            catalog_path.write_text(json.dumps(catalog, sort_keys=True))
            environment["RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS"] = "0"
            scheduler_state = root / "state" / "state" / "video-scheduler"
            for hours in (3, 24):
                (scheduler_state / (
                    f"exact-{hours}-bc-large-overlay_.success-epoch"
                )).write_text("0\n")

            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            after = calls.read_text().splitlines()
            self.assertEqual(
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in after
                ),
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in before
                ),
            )
            self.assertEqual(
                sum(
                    "--range-hours 24" in line
                    and "--product bc-large-overlay" in line
                    for line in after
                ),
                sum(
                    "--range-hours 24" in line
                    and "--product bc-large-overlay" in line
                    for line in before
                ) + 1,
            )
            token_three = (
                scheduler_state / "exact-3-bc-large-overlay_.token"
            ).read_text()
            token_day = (
                scheduler_state / "exact-24-bc-large-overlay_.token"
            ).read_text()
            self.assertNotEqual(token_three, token_day)

    def test_video_scheduler_render_revision_invalidates_exact_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            environment["RADARSAT_VIDEO_BUILD_REVISION"] = "render-policy-a"
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            before = calls.read_text().splitlines()

            environment.update({
                "RADARSAT_VIDEO_BUILD_REVISION": "render-policy-b",
                "RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS": "0",
            })
            scheduler_state = root / "state" / "state" / "video-scheduler"
            (scheduler_state / (
                "exact-3-bc-large-overlay_.success-epoch"
            )).write_text("0\n")
            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            after = calls.read_text().splitlines()
            self.assertEqual(
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in after
                ),
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in before
                ) + 1,
            )

    def test_video_scheduler_msc_anchor_ignores_ineligible_newer_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self._video_scheduler_fixture(root)
            environment.update({
                "RADARSAT_VIDEO_TOKEN_NOW_EPOCH": "1787319000",  # 13:30Z
                "RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS": "0",
            })
            catalog_path = root / "output" / "catalog.json"
            catalog = json.loads(catalog_path.read_text())
            for product in catalog["products"]:
                if product["domain"] == "bc":
                    product["layers"].append({
                        "id": "lightning-trail",
                        "optional": True,
                    })
            catalog["domains"]["bc"]["layers"].update({
                "raw-visir": {
                    "frames": [{
                        "validTime": "2026-08-21T13:00:21Z",
                        "fetchedAt": "2026-08-21T13:10:00Z",
                        "path": "frames/bc/raw-visir/1300-v1.webp",
                    }],
                },
                "radar-rain": {
                    "maxAgeMinutes": 20,
                    "frames": [{
                        "validTime": "2026-08-21T13:00:00Z",
                        "fetchedAt": "2026-08-21T13:02:00Z",
                        "path": "frames/bc/radar-rain/1300-v1.png",
                    }],
                },
                "lightning-trail": {
                    "maxAgeMinutes": 30,
                    "frames": [{
                        "validTime": "2026-08-21T13:00:00Z",
                        "fetchedAt": "2026-08-21T13:03:00Z",
                        "path": "frames/bc/lightning-trail/1300-v1.png",
                    }],
                },
            })
            catalog_path.write_text(json.dumps(catalog))
            first = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            before = calls.read_text().splitlines()

            # NOAA, radar, and lightning are all newer than the 12Z MSC
            # endpoint, while NOAA 13Z is only 30 minutes old. Revisions to
            # those future inputs must remain outside the immutable 12Z token.
            catalog = json.loads(catalog_path.read_text())
            for layer_id in ("raw-visir", "radar-rain", "lightning-trail"):
                frame = catalog["domains"]["bc"]["layers"][layer_id]["frames"][0]
                frame["fetchedAt"] = "2026-08-21T13:31:00Z"
                frame["path"] = frame["path"].replace("-v1", "-v2")
            catalog_path.write_text(json.dumps(catalog, sort_keys=True))
            second = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            after_ineligible = calls.read_text().splitlines()
            self.assertEqual(
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in after_ineligible
                ),
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in before
                ),
            )

            # At 13:36Z the absent 13Z MSC slot is beyond its 35-minute
            # deadline, so the exact-slot NOAA frame becomes the selected
            # endpoint and must invalidate the 3h token.
            environment["RADARSAT_VIDEO_TOKEN_NOW_EPOCH"] = "1787319360"
            third = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(third.returncode, 0, third.stderr)
            after_eligible = calls.read_text().splitlines()
            self.assertEqual(
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in after_eligible
                ),
                sum(
                    "--range-hours 3" in line
                    and "--product bc-large-overlay" in line
                    for line in before
                ) + 1,
            )

    def _assert_scheduler_deadline_kills_child(
        self, root: Path, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        marker = Path(environment["RADARSAT_TEST_CHILD_PID"])
        started = time.monotonic()
        result = subprocess.run(
            ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
            text=True, capture_output=True, check=False, timeout=8,
        )
        self.assertLess(time.monotonic() - started, 6)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "deadline fixture child did not start")
        child_pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertFalse((root / "state" / "run" / "video-worker.lock").exists())
        return result

    def test_video_scheduler_deadline_bounds_exact_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _calls = self._video_scheduler_fixture(root)
            environment.update({
                "RADARSAT_VIDEO_MAX_EXACT_WORKERS": "1",
                "RADARSAT_VIDEO_SCHEDULER_MAX_RUNTIME_SECONDS": "3",
                "RADARSAT_VIDEO_TERMINATE_GRACE_SECONDS": "0",
                "RADARSAT_VIDEO_KILL_REAP_SECONDS": "2",
                "RADARSAT_TEST_SLEEP_TRACK": "live",
                "RADARSAT_TEST_SLEEP_SECONDS": "30",
                "RADARSAT_TEST_IGNORE_TERM": "1",
            })
            self._assert_scheduler_deadline_kills_child(root, environment)

    def test_video_scheduler_deadline_bounds_archive_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _calls = self._video_scheduler_fixture(root)
            initial = subprocess.run(
                ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)
            Path(environment["RADARSAT_TEST_CHILD_PID"]).unlink(missing_ok=True)
            environment.update({
                "RADARSAT_VIDEO_SCHEDULER_MAX_RUNTIME_SECONDS": "3",
                "RADARSAT_VIDEO_TERMINATE_GRACE_SECONDS": "0",
                "RADARSAT_VIDEO_KILL_REAP_SECONDS": "2",
                "RADARSAT_TEST_SLEEP_TRACK": "archive",
                "RADARSAT_TEST_SLEEP_SECONDS": "30",
                "RADARSAT_TEST_IGNORE_TERM": "1",
            })
            self._assert_scheduler_deadline_kills_child(root, environment)

    def test_video_scheduler_deadline_bounds_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _calls = self._video_scheduler_fixture(root)
            state = root / "state" / "state" / "video-scheduler"
            state.mkdir(parents=True)
            (state / "publication-dirty").write_text("dirty\n")
            environment.update({
                # Give catalog startup enough headroom on a busy CI/Mac host;
                # the sleeping publisher still proves the hard deadline.
                "RADARSAT_VIDEO_SCHEDULER_MAX_RUNTIME_SECONDS": "3",
                "RADARSAT_VIDEO_TERMINATE_GRACE_SECONDS": "0",
                "RADARSAT_VIDEO_KILL_REAP_SECONDS": "2",
                "RADARSAT_TEST_SLEEP_PUBLISHER": "1",
                "RADARSAT_TEST_SLEEP_SECONDS": "30",
                "RADARSAT_TEST_IGNORE_TERM": "1",
            })
            self._assert_scheduler_deadline_kills_child(root, environment)

    def _terminate_scheduler_and_assert_reaped(
        self, root: Path, environment: dict[str, str]
    ) -> None:
        marker = Path(environment["RADARSAT_TEST_CHILD_PID"])
        process = subprocess.Popen(
            ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "encoder child did not start")
        child_pid = int(marker.read_text())
        lock = root / "state" / "run" / "video-worker.lock"
        self.assertTrue(lock.exists())
        process.terminate()
        _stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, stderr)
        self.assertFalse(lock.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_video_scheduler_reaps_exact_and_archive_children_before_unlock(self) -> None:
        with self.subTest(worker="exact"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment, _calls = self._video_scheduler_fixture(root)
                environment.update({
                    "RADARSAT_VIDEO_MAX_EXACT_WORKERS": "1",
                    "RADARSAT_TEST_SLEEP_TRACK": "live",
                    "RADARSAT_TEST_SLEEP_SECONDS": "30",
                })
                self._terminate_scheduler_and_assert_reaped(root, environment)

        with self.subTest(worker="archive"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment, _calls = self._video_scheduler_fixture(root)
                initial = subprocess.run(
                    ["/bin/zsh", str(RUN_VIDEO_SCHEDULER)], cwd=PROJECT, env=environment,
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(initial.returncode, 0, initial.stderr)
                marker = Path(environment["RADARSAT_TEST_CHILD_PID"])
                marker.unlink(missing_ok=True)
                environment.update({
                    "RADARSAT_TEST_SLEEP_TRACK": "archive",
                    "RADARSAT_TEST_SLEEP_SECONDS": "30",
                })
                self._terminate_scheduler_and_assert_reaped(root, environment)

    def test_video_proxy_lifecycle_is_reachability_managed(self) -> None:
        source = CONFIGURE_R2.read_text()
        self.assertIn('"ID": "expire-video-media"', source)
        self.assertIn('"ID": "expire-video-segments"', source)
        self.assertIn('"ID": "expire-video-manifests"', source)
        self.assertNotIn('"ID": "expire-video-proxies"', source)
        self.assertNotIn('"ID": "expire-video-static-overlays"', source)

    def _fake_python(self, root: Path) -> tuple[Path, Path]:
        executable = root / "fake-python.zsh"
        log = root / "calls.log"
        executable.write_text(
            "#!/bin/zsh\n"
            'print -r -- "$*" >> "${RADARSAT_TEST_CALLS}"\n'
            'if [[ "${RADARSAT_TEST_FAIL_INGEST:-0}" == "1" '
            '&& "$*" == *scripts/run_ingest.py* ]]; then exit 6; fi\n'
            'if [[ "${RADARSAT_TEST_FAIL_WESTWX:-0}" == "1" '
            '&& "$*" == *backfill_westwx_satellite.py* ]]; then exit 7; fi\n'
        )
        executable.chmod(0o755)
        return executable, log

    def _environment(self, root: Path, executable: Path, log: Path) -> dict[str, str]:
        publisher = root / "fake-cycle-publisher.zsh"
        publisher.write_text(
            "#!/bin/zsh\n"
            'print -r -- "scripts/publish_r2.py $*" >> "${RADARSAT_TEST_CALLS}"\n'
        )
        publisher.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "RADARSAT_PYTHON": str(executable),
                "RADARSAT_TEST_CALLS": str(log),
                "RADARSAT_STATE_ROOT": str(root / "state"),
                "RADARSAT_OUTPUT_ROOT": str(root / "output"),
                "RADARSAT_SPOOL_ROOT": str(root / "spool"),
                "RADARSAT_RAW_RETENTION_HOURS": "4",
                "RADARSAT_ENV_FILE": str(root / "missing.env"),
                "RADARSAT_CYCLE_PUBLISHER": str(publisher),
            }
        )
        return environment

    def test_cycle_recovers_dead_pid_and_prunes_only_after_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, log = self._fake_python(root)
            lock = root / "state" / "run" / "cycle.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text("99999999\n")

            result = subprocess.run(
                ["/bin/zsh", str(RUN_CYCLE)],
                cwd=PROJECT,
                env=self._environment(root, executable, log),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 3)
            self.assertIn("scripts/run_ingest.py", calls[0])
            self.assertIn("scripts/prune_eccc_spool.py", calls[1])
            self.assertIn("--older-than-hours 4", calls[1])
            self.assertTrue(calls[1].endswith("--apply"))
            self.assertIn("--ingest-status", calls[1])
            self.assertIn("scripts/publish_r2.py", calls[2])
            self.assertFalse(lock.exists())

    def test_opt_in_westwx_scan_is_bounded_and_failure_does_not_block_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, log = self._fake_python(root)
            environment = self._environment(root, executable, log)
            environment.update(
                {
                    "RADARSAT_WESTWX_SATELLITE_ENABLED": "1",
                    "RADARSAT_TEST_FAIL_WESTWX": "1",
                }
            )

            result = subprocess.run(
                ["/bin/zsh", str(RUN_CYCLE)],
                cwd=PROJECT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 9)
            self.assertIn("scripts/backfill_noaa_star_geocolor.py", calls[0])
            self.assertIn("--sector full-disk", calls[0])
            self.assertIn("--max-download-gb 0.1", calls[0])
            self.assertIn("scripts/backfill_westwx_satellite.py", calls[1])
            self.assertIn("--max-frames 1", calls[1])
            self.assertIn("--max-download-gb 0.8", calls[1])
            self.assertIn("--max-source-mb 400", calls[1])
            self.assertTrue(calls[1].endswith("--apply"))
            self.assertIn("scripts/backfill_noaa_star_geocolor.py", calls[2])
            self.assertIn("--sector pacus", calls[2])
            self.assertIn("--max-download-gb 0.06", calls[2])
            self.assertIn("scripts/backfill_five_minute_bc_satellite.py", calls[3])
            self.assertIn("--max-frames 2", calls[3])
            self.assertIn("--max-download-gb 0.15", calls[3])
            self.assertIn("--max-source-mb 100", calls[3])
            self.assertIn("scripts/backfill_native_bc_satellite.py", calls[4])
            self.assertIn("--max-frames 1", calls[4])
            self.assertIn("--max-download-gb 0.7", calls[4])
            self.assertIn("--max-source-mb 700", calls[4])
            self.assertIn("scripts/publish_r2.py", calls[5])
            self.assertIn("scripts/run_ingest.py", calls[6])
            self.assertIn("scripts/prune_eccc_spool.py", calls[7])
            self.assertIn("scripts/publish_r2.py", calls[8])
            self.assertIn("isolated WestWX", result.stderr)

    def test_failed_primary_still_runs_westwx_and_publish_without_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, log = self._fake_python(root)
            environment = self._environment(root, executable, log)
            environment.update(
                {
                    "RADARSAT_WESTWX_SATELLITE_ENABLED": "1",
                    "RADARSAT_TEST_FAIL_INGEST": "1",
                }
            )

            result = subprocess.run(
                ["/bin/zsh", str(RUN_CYCLE)],
                cwd=PROJECT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 6, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 8)
            self.assertIn("scripts/backfill_noaa_star_geocolor.py", calls[0])
            self.assertIn("scripts/backfill_westwx_satellite.py", calls[1])
            self.assertIn("scripts/backfill_noaa_star_geocolor.py", calls[2])
            self.assertIn("scripts/backfill_five_minute_bc_satellite.py", calls[3])
            self.assertIn("scripts/backfill_native_bc_satellite.py", calls[4])
            self.assertIn("scripts/publish_r2.py", calls[5])
            self.assertIn("scripts/run_ingest.py", calls[6])
            self.assertIn("scripts/publish_r2.py", calls[7])
            self.assertFalse(any("scripts/prune_eccc_spool.py" in call for call in calls))
            self.assertIn("primary Radar-Sat ingest failed with status 6", result.stderr)
            self.assertIn("skipping raw spool prune", result.stderr)
            self.assertFalse((root / "state" / "run" / "cycle.lock").exists())

    def test_cycle_does_not_overlap_a_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, log = self._fake_python(root)
            lock = root / "state" / "run" / "cycle.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{os.getpid()}\n")

            result = subprocess.run(
                ["/bin/zsh", str(RUN_CYCLE)],
                cwd=PROJECT,
                env=self._environment(root, executable, log),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already running", result.stdout)
            self.assertFalse(log.exists())
            self.assertTrue(lock.exists())

    def test_split_workers_keep_rapid_satellite_isolated_from_slow_renderers(self) -> None:
        satellite = (PROJECT / "scripts" / "ops" / "run_satellite_cycle.zsh").read_text()
        five_minute = (
            PROJECT / "scripts" / "ops" / "run_five_minute_satellite_cycle.zsh"
        ).read_text()
        observations = (PROJECT / "scripts" / "ops" / "run_observation_cycle.zsh").read_text()
        archive = (PROJECT / "scripts" / "ops" / "run_archive_cycle.zsh").read_text()
        model_contours = (
            PROJECT / "scripts" / "ops" / "run_model_contour_cycle.zsh"
        ).read_text()
        model_contours_plist = (
            PROJECT / "ops" / "com.greg.radar-sat.model-contours.plist.template"
        ).read_text()
        heavy_lock = (PROJECT / "scripts" / "ops" / "heavy_satellite_lock.zsh").read_text()
        observation_plist = (
            PROJECT / "ops" / "com.greg.radar-sat.observations.plist.template"
        ).read_text()
        install = (PROJECT / "scripts" / "ops" / "install_launchd.zsh").read_text()

        self.assertIn("backfill_westwx_satellite.py", satellite)
        self.assertIn("backfill_noaa_star_geocolor.py", satellite)
        video = (PROJECT / "scripts" / "ops" / "run_video_scheduler.zsh").read_text()
        video_plist = (
            PROJECT / "ops" / "com.greg.radar-sat.video-scheduler.plist.template"
        ).read_text()
        lightning_edge = (
            PROJECT / "scripts" / "ops" / "run_lightning_edge_cycle.zsh"
        ).read_text()
        radar_edge = (
            PROJECT / "scripts" / "ops" / "run_radar_edge_cycle.zsh"
        ).read_text()
        msc_edge = (
            PROJECT / "scripts" / "ops" / "run_msc_satellite_edge_cycle.zsh"
        ).read_text()
        self.assertNotIn("build_satellite_video.py", satellite)
        self.assertIn("build_composite_video.py", video)
        self.assertIn("build_satellite_video.py", video)
        self.assertIn("--defer-shared-prune", video)
        self.assertIn("--defer-cache-prune", video)
        self.assertIn("--prune-shared-only", video)
        self.assertIn("--prune-cache-only", video)
        self.assertIn("RADARSAT_VIDEO_ENABLED", video)
        self.assertIn('--range-hours "${range}"', video)
        self.assertIn('for candidate in 3 6 12 24', video)
        self.assertIn("MAX_EXACT_WORKERS", video)
        self.assertIn("video-worker.lock", video)
        self.assertIn("publication-dirty", video)
        self.assertIn("FAILURE_BACKOFF_SECONDS", video)
        self.assertIn("refresh_lightning_edge.py", lightning_edge)
        self.assertIn("refresh_radar_edge.py", radar_edge)
        self.assertIn("refresh_msc_satellite_edge.py", msc_edge)
        self.assertIn("live_edge_publish.zsh", lightning_edge)
        self.assertIn("live_edge_publish.zsh", radar_edge)
        self.assertIn("live_edge_publish.zsh", msc_edge)
        self.assertIn("refresh_status=0", radar_edge)
        self.assertLess(
            radar_edge.index("refresh_radar_edge.py"),
            radar_edge.index("live_edge_publish.zsh"),
        )
        self.assertLess(
            radar_edge.index("live_edge_publish.zsh"),
            radar_edge.index('exit "${refresh_status}"'),
        )
        self.assertIn("--archive-hours", video)
        self.assertIn("last-good", video)
        self.assertIn("<integer>60</integer>", video_plist)
        self.assertIn("video-scheduler", install)
        self.assertIn("video-day", install)
        self.assertIn("msc-satellite-edge", install)
        self.assertIn("--sector full-disk", satellite)
        self.assertNotIn("backfill_five_minute_bc_satellite.py", satellite)
        self.assertNotIn("backfill_native_bc_satellite.py", satellite)
        self.assertNotIn("scripts/run_ingest.py", satellite)
        self.assertIn("publish_locked.zsh", satellite)
        self.assertIn(
            "publish_locked.zsh\" --fast --existing-video-only --whole-frame-only --recovery-hours 24",
            satellite,
        )
        self.assertNotIn("build_raster_tiles.py", satellite)
        self.assertIn("try_acquire_heavy_satellite_lock", satellite)

        self.assertNotIn("backfill_five_minute_bc_satellite.py", five_minute)
        self.assertIn("backfill_noaa_star_geocolor.py", five_minute)
        self.assertIn("--sector pacus", five_minute)
        self.assertIn("RADARSAT_NOAA_STAR_PACUS_MAX_FRAMES:-4", five_minute)
        self.assertIn(
            "publish_locked.zsh\" --fast --existing-video-only --whole-frame-only --recovery-hours 24",
            five_minute,
        )
        self.assertNotIn("build_raster_tiles.py", five_minute)
        self.assertIn("five-minute-satellite-cycle.lock", five_minute)
        self.assertNotIn("try_acquire_heavy_satellite_lock", five_minute)

        self.assertIn("RADARSAT_RAW_SAT_ENABLED=0", observations)
        self.assertIn("--spool-mode only", observations)
        self.assertIn("publish_locked.zsh", observations)
        self.assertIn(
            "publish_locked.zsh\" --fast --existing-video-only --whole-frame-only --recovery-hours 24",
            observations,
        )
        self.assertIn("<string>10</string>", observation_plist)

        self.assertIn("--domain north-pacific", archive)
        self.assertNotIn("backfill_model_contours.py", archive)
        self.assertIn("--latest-only", archive)
        self.assertIn("RADARSAT_GOES_HAZARDS_ENABLED=0", archive)
        self.assertIn("RADARSAT_ARCHIVE_START_DELAY_SECONDS", archive)
        self.assertIn("try_acquire_heavy_satellite_lock", archive)
        self.assertIn("build_raster_tiles.py", archive)
        self.assertIn('RADARSAT_WEB_TILES_ENABLED:-0', archive)
        self.assertIn(
            'publish_locked.zsh" --whole-frame-only --recovery-hours 24',
            archive,
        )
        self.assertLess(
            archive.index("release_heavy_satellite_lock"),
            archive.index("build_raster_tiles.py"),
        )
        self.assertIn("backfill_model_contours.py", model_contours)
        self.assertIn("RADARSAT_MODEL_CONTOURS_ENABLED", model_contours)
        self.assertIn("RADARSAT_ECMWF_DATA_ROOT", model_contours)
        self.assertIn("RADARSAT_HRDPS_CONTOUR_RECOVERY_HOURS:-0", model_contours)
        self.assertIn("RADARSAT_MODEL_PUBLISH_LOCK_WAIT_SECONDS:-900", model_contours)
        self.assertIn("model-contour-cycle.lock", model_contours)
        self.assertNotIn("try_acquire_heavy_satellite_lock", model_contours)
        self.assertNotIn("scripts/run_ingest.py", model_contours)
        self.assertIn("publish_locked.zsh", model_contours)
        self.assertIn(
            "--fast --existing-video-only --whole-frame-only --recovery-hours 24",
            model_contours,
        )
        self.assertIn("<integer>1800</integer>", model_contours_plist)
        self.assertIn("heavy-satellite.lock", heavy_lock)
        publish_locked = (
            PROJECT / "scripts" / "ops" / "publish_locked.zsh"
        ).read_text()
        self.assertIn("RADARSAT_PUBLISH_LOCK_WAIT_SECONDS:-300", publish_locked)
        self.assertIn("/usr/bin/lockf -k -t", publish_locked)
        self.assertNotIn("LOCK_OWNER", publish_locked)
        self.assertIn(
            "lightning-edge radar-edge model-contours video-scheduler",
            install,
        )

    def test_setup_installs_renderer_and_feed_requirements(self) -> None:
        setup = (PROJECT / "scripts" / "ops" / "setup_local.zsh").read_text()
        self.assertIn('requirements.txt"', setup)
        self.assertIn('requirements-feeds.txt"', setup)
        self.assertIn("sys.version_info < (3, 11)", setup)
        self.assertIn("/opt/homebrew/bin/python3.12", setup)
        self.assertTrue((PROJECT / "scripts" / "sr3-radarsat").exists())
        sr3_entry = (PROJECT / "scripts" / "sr3_entry.py").read_text()
        self.assertIn("socket.getfqdn = _stable_getfqdn", sr3_entry)
        self.assertIn("_filter_sr_proc_case_insensitive_python", sr3_entry)
        self.assertIn('command.insert(start_index, "sanity")', sr3_entry)

    def test_sr3_child_runtime_removes_forwarded_sanity_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary) / "instance.py"
            instance.write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n")
            environment = os.environ.copy()
            runtime = str(PROJECT / "scripts" / "sr3_runtime")
            environment["PYTHONPATH"] = runtime + (
                os.pathsep + environment["PYTHONPATH"]
                if environment.get("PYTHONPATH")
                else ""
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(instance),
                    "--no",
                    "1",
                    "sanity",
                    "start",
                    "subscribe/radarsat_lightning",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                ["--no", "1", "start", "subscribe/radarsat_lightning"],
            )

    def test_spool_pruner_never_touches_dot_prefixed_inflight_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = Path(temporary) / "spool"
            satellite = spool / "satellite"
            satellite.mkdir(parents=True)
            old = satellite / "20260720T0000Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            newest = satellite / "20260720T0010Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            inflight = satellite / ".20260720T0020Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            for path in (old, newest, inflight):
                path.write_bytes(b"II*\x00test-data")
            old_time = time.time() - 3 * 3600
            os.utime(old, (old_time, old_time))
            os.utime(inflight, (old_time, old_time))

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT / "scripts" / "prune_eccc_spool.py"),
                    "--spool",
                    str(spool),
                    "--older-than-hours",
                    "1",
                    "--apply",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(old.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(inflight.exists())

    def test_spool_pruner_preserves_rejected_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = root / "spool"
            satellite = spool / "satellite"
            satellite.mkdir(parents=True)
            rejected = satellite / "20260720T0000Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            newest = satellite / "20260720T0010Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            for path in (rejected, newest):
                path.write_bytes(b"II*\x00test-data")
            old_time = time.time() - 3 * 3600
            os.utime(rejected, (old_time, old_time))
            status = root / "ingest.json"
            status.write_text(json.dumps({
                "spool": {
                    "domains": {
                        "bc": {"preserveFiles": [rejected.name]},
                    }
                }
            }))

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT / "scripts" / "prune_eccc_spool.py"),
                    "--spool",
                    str(spool),
                    "--older-than-hours",
                    "1",
                    "--ingest-status",
                    str(status),
                    "--apply",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(rejected.exists())
            self.assertIn("preserved=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
