from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
WORKER = PROJECT / "scripts/ops/run_full_publisher.zsh"


class PublisherWorkerTests(unittest.TestCase):
    def fixture(self, root: Path):
        requests = root / "state/state/full-publish-requests"
        requests.mkdir(parents=True)
        calls = root / "calls"
        driver = root / "publish.zsh"
        driver.write_text(
            '#!/bin/zsh\nprint -r -- "$*" >> "${TEST_CALLS}"\n'
            'if [[ "$*" != *--fast* && "${TEST_FAIL_MAINTENANCE:-0}" == 1 ]]; then\n'
            '  exit 9\nfi\n'
            'if [[ "$*" == *--fast* && "${TEST_FAIL_FAST:-0}" == 1 ]]; then\n'
            '  exit 8\nfi\n'
            'if [[ "$*" != *--fast* && -n "${TEST_NEW_REQUEST:-}" ]]; then\n'
            '  : > "${TEST_NEW_REQUEST}"\nfi\n'
        )
        driver.chmod(0o755)
        env = {**os.environ, "RADARSAT_PYTHON": sys.executable,
               "RADARSAT_STATE_ROOT": str(root / "state"),
               "RADARSAT_OUTPUT_ROOT": str(root / "output"),
               "RADARSAT_ENV_FILE": str(root / "missing.env"),
               "RADARSAT_FULL_PUBLISH_DRIVER": str(driver),
               "RADARSAT_FULL_PUBLISH_KICKSTART": "0", "TEST_CALLS": str(calls)}
        return requests, calls, driver, env

    def run_worker(self, env):
        return subprocess.run(["/bin/zsh", str(WORKER)], env=env, cwd=PROJECT,
                              capture_output=True, text=True, timeout=15)

    def test_fresh_video_commits_before_coalesced_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            requests, calls, _, env = self.fixture(Path(temporary))
            for name in ("fast-existing", "fast-video", "reconcile"):
                (requests / f"1.{name}.request").touch()
            result = self.run_worker(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                "--fast --whole-frame-only --recovery-hours 24",
                "--whole-frame-only --recovery-hours 24"])
            self.assertEqual(list(requests.glob("*.request")), [])

    def test_failed_maintenance_keeps_requests_but_allows_new_fast_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            requests, calls, _, env = self.fixture(Path(temporary))
            maintenance = requests / "1.reconcile.request"
            maintenance.touch()
            (requests / "1.fast-video.request").touch()
            env["TEST_FAIL_MAINTENANCE"] = "1"
            self.assertEqual(self.run_worker(env).returncode, 1)
            self.assertEqual(list(requests.glob("*.request")), [maintenance])
            # Backoff must prevent repeated maintenance and synthetic fast
            # work, without holding up newly completed video generations.
            self.assertEqual(self.run_worker(env).returncode, 0)
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            (requests / "2.fast-video.request").touch()
            self.assertEqual(self.run_worker(env).returncode, 0)
            self.assertEqual(len(calls.read_text().splitlines()), 3)
            self.assertEqual(list(requests.glob("*.request")), [maintenance])

    def test_failed_fast_attempt_allows_queued_repair_and_preserves_new_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            requests, calls, _, env = self.fixture(Path(temporary))
            for profile in ("fast-video", "reconcile"):
                (requests / f"1.{profile}.request").touch()
            newer = requests / "2.fast-video.request"
            env["TEST_FAIL_FAST"] = "1"
            env["TEST_NEW_REQUEST"] = str(newer)
            result = self.run_worker(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("attempting queued reconcile repair", result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                "--fast --whole-frame-only --recovery-hours 24",
                "--whole-frame-only --recovery-hours 24"])
            self.assertEqual(list(requests.glob("*.request")), [newer])

    def test_failed_fast_and_repair_retain_requests_and_back_off_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            requests, calls, _, env = self.fixture(Path(temporary))
            originals = {requests / f"1.{profile}.request"
                         for profile in ("fast-video", "reconcile")}
            for request in originals:
                request.touch()
            env["TEST_FAIL_FAST"] = "1"
            env["TEST_FAIL_MAINTENANCE"] = "1"
            self.assertEqual(self.run_worker(env).returncode, 1)
            self.assertEqual(set(requests.glob("*.request")), originals)
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            # New attempts may retry fresh publication, but a failed repair
            # must not get another maintenance pass during its backoff.
            self.assertEqual(self.run_worker(env).returncode, 1)
            self.assertEqual(set(requests.glob("*.request")), originals)
            self.assertEqual(calls.read_text().splitlines()[2:], [
                "--fast --whole-frame-only --recovery-hours 24"])

    def test_timeout_kills_descendants_retains_request_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests, calls, driver, env = self.fixture(root)
            child = root / "stuck.py"
            marker = root / "child.pid"
            child.write_text(
                "import os,signal,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({str(marker)!r},'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n")
            driver.write_text(f"#!/bin/zsh\n{sys.executable} {child}\n")
            env["RADARSAT_FULL_PUBLISH_MAX_RUNTIME_SECONDS"] = "1"
            request = requests / "1.fast-video.request"
            request.touch()
            started = time.monotonic()
            result = self.run_worker(env)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("exceeded 1s", result.stderr)
            self.assertLess(time.monotonic() - started, 12)
            self.assertTrue(request.exists())
            self.assertTrue(marker.exists())
            child_pid = int(marker.read_text())
            # The whole process group must die, including a child ignoring TERM.
            for _ in range(50):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("timed-out publisher descendant is still alive")
            driver.write_text('#!/bin/zsh\nprint ok >> "${TEST_CALLS}"\n')
            self.assertEqual(self.run_worker(env).returncode, 0)
            self.assertFalse(request.exists())
            self.assertEqual(calls.read_text().strip(), "ok")


if __name__ == "__main__":
    unittest.main()
