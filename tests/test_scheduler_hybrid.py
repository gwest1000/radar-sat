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

    def test_retired_hybrid_lane_cannot_be_reenabled_by_stale_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment, calls = self.fixture(Path(temporary))
            result = self.run_scheduler(environment)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            lines = calls.read_text().splitlines()
        self.assertFalse(any("--preset " in line for line in lines))
        self.assertTrue(any("--range-hours " in line for line in lines))
        self.assertTrue(any("--track archive" in line for line in lines))
        self.assertIn("--prune-shared-only", lines[-1])


if __name__ == "__main__":
    unittest.main()
