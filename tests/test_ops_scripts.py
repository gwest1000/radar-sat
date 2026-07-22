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


class OpsScriptTests(unittest.TestCase):
    def _fake_python(self, root: Path) -> tuple[Path, Path]:
        executable = root / "fake-python.zsh"
        log = root / "calls.log"
        executable.write_text(
            "#!/bin/zsh\n"
            'print -r -- "$*" >> "${RADARSAT_TEST_CALLS}"\n'
            'if [[ "${RADARSAT_TEST_FAIL_WESTWX:-0}" == "1" '
            '&& "$*" == *backfill_westwx_satellite.py* ]]; then exit 7; fi\n'
        )
        executable.chmod(0o755)
        return executable, log

    def _environment(self, root: Path, executable: Path, log: Path) -> dict[str, str]:
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
            self.assertEqual(len(calls), 4)
            self.assertIn("scripts/run_ingest.py", calls[0])
            self.assertIn("scripts/backfill_westwx_satellite.py", calls[1])
            self.assertIn("--max-frames 1", calls[1])
            self.assertIn("--max-download-gb 0.4", calls[1])
            self.assertTrue(calls[1].endswith("--apply"))
            self.assertIn("scripts/prune_eccc_spool.py", calls[2])
            self.assertIn("scripts/publish_r2.py", calls[3])
            self.assertIn("isolated WestWX", result.stderr)

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
