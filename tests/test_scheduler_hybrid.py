from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import test_ops_scripts as fixtures


class HybridSchedulerTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        environment, calls = fixtures.OpsScriptTests()._video_scheduler_fixture(root)
        environment.update({
            "RADARSAT_HYBRID_CORE_ENABLED": "1",
            "RADARSAT_VIDEO_MAX_HYBRID_UNITS": "3",
            "RADARSAT_VIDEO_FAILURE_BACKOFF_SECONDS": "0",
        })
        return environment, calls

    def run_scheduler(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/zsh", str(fixtures.RUN_VIDEO_SCHEDULER)],
            cwd=fixtures.PROJECT, env=environment, text=True,
            capture_output=True, check=False, timeout=45,
        )

    def test_three_hybrid_units_follow_exact_and_leave_archive_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment, calls = self.fixture(Path(temporary))
            result = self.run_scheduler(environment)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            lines = calls.read_text().splitlines()
        hybrids = [(i, line) for i, line in enumerate(lines) if "--preset " in line]
        exact = [i for i, line in enumerate(lines) if "--range-hours " in line and "--preset " not in line]
        self.assertEqual(len(hybrids), 3)
        self.assertGreater(hybrids[0][0], max(exact))
        self.assertIn("--preset weather-core-v1", hybrids[0][1])
        self.assertIn("--preset weather-smoke-core-v1", hybrids[1][1])
        archive = [i for i, line in enumerate(lines) if "--track archive" in line]
        self.assertEqual(len(archive), 1)
        self.assertGreater(archive[0], hybrids[-1][0])
        self.assertIn("--prune-shared-only", lines[-1])

    def test_short_loop_deadline_precedes_older_long_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self.fixture(root)
            environment["RADARSAT_HYBRID_CORE_ENABLED"] = "0"
            initial = self.run_scheduler(environment)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            state = root / "state" / "state" / "video-scheduler"
            now = int(time.time())
            for family in ("weather-core-v1", "weather-smoke-core-v1"):
                for hours in (3, 6, 12, 24):
                    products = ["bc-large-overlay", "bc-northeast-overlay"]
                    if hours in (12, 24):
                        products.append("north-america-overlay")
                    for product in products:
                        prefix = "hybrid" if family == "weather-smoke-core-v1" else f"hybrid-{family}"
                        (state / f"{prefix}-{hours}-{product}_.success-epoch").write_text(str(now))
            (state / "hybrid-weather-core-v1-3-bc-large-overlay_.success-epoch").write_text(str(now - 1190))
            (state / "hybrid-12-north-america-overlay_.success-epoch").write_text(str(now - 1800))
            environment.update({"RADARSAT_HYBRID_CORE_ENABLED": "1", "RADARSAT_VIDEO_MAX_HYBRID_UNITS": "1"})
            result = self.run_scheduler(environment)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            hybrids = [line for line in calls.read_text().splitlines() if "--preset " in line]
        self.assertEqual(len(hybrids), 1)
        self.assertIn("--range-hours 3", hybrids[0])
        self.assertIn("--product bc-large-overlay", hybrids[0])

    def test_new_exact_work_preempts_remaining_hybrids_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, calls = self.fixture(root)
            driver = Path(environment["RADARSAT_COMPOSITE_VIDEO_BUILDER"])
            with driver.open("a") as stream:
                stream.write(
                    "if '--preset' in sys.argv:\n"
                    "    import json\n"
                    "    from pathlib import Path\n"
                    "    p = Path(os.environ['RADARSAT_OUTPUT_ROOT']) / 'catalog.json'\n"
                    "    catalog = json.loads(p.read_text())\n"
                    "    frame = catalog['domains']['bc']['layers']['eccc-geocolor']['frames'][0]\n"
                    "    frame['validTime'] = '2026-08-21T12:10:00Z'\n"
                    "    frame['sourceValidTime'] = '2026-08-21T12:10:20Z'\n"
                    "    p.write_text(json.dumps(catalog))\n"
                )
            result = self.run_scheduler(environment)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            lines = calls.read_text().splitlines()
        self.assertEqual(sum("--preset " in line for line in lines), 1)
        self.assertFalse(any("--track archive" in line for line in lines))
        self.assertIn("New exact-video work is due", result.stdout)

    def test_hybrid_time_budget_reaps_stuck_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _calls = self.fixture(root)
            environment.update({
                "RADARSAT_VIDEO_HYBRID_BUDGET_SECONDS": "3",
                "RADARSAT_VIDEO_TERMINATE_GRACE_SECONDS": "0",
                "RADARSAT_VIDEO_KILL_REAP_SECONDS": "1",
            })
            driver = Path(environment["RADARSAT_COMPOSITE_VIDEO_BUILDER"])
            with driver.open("a") as stream:
                stream.write(
                    "if '--preset' in sys.argv:\n"
                    "    from pathlib import Path\n"
                    "    Path(os.environ['RADARSAT_TEST_CHILD_PID']).write_text(str(os.getpid()))\n"
                    "    time.sleep(30)\n"
                )
            started = time.monotonic()
            result = self.run_scheduler(environment)
            self.assertLess(time.monotonic() - started, 20)
            self.assertNotEqual(result.returncode, 0)
            child_pid = int(Path(environment["RADARSAT_TEST_CHILD_PID"]).read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertIn("Scheduler deadline reached", result.stdout)


if __name__ == "__main__":
    unittest.main()
