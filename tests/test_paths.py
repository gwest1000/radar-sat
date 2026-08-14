from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radarsat import paths


class RuntimePathTests(unittest.TestCase):
    def test_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(paths.data_root({}, root), root / "data")
            self.assertEqual(paths.output_root({}, root), root / "data" / "output")

    def test_shared_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp)
            data = shared / "radar-sat" / "data"
            (data / "output").mkdir(parents=True)
            env = {"PROJECT_DATA_ROOT": str(shared)}
            self.assertEqual(paths.data_root(env), data.resolve())
            self.assertEqual(paths.output_root(env), (data / "output").resolve())

    def test_project_override_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            override = root / "override"
            shared.mkdir()
            (override / "output").mkdir(parents=True)
            env = {
                "PROJECT_DATA_ROOT": str(shared),
                "RADARSAT_DATA_ROOT": str(override),
            }
            self.assertEqual(paths.data_root(env), override.resolve())

    def test_missing_shared_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with self.assertRaisesRegex(RuntimeError, "configured but unavailable"):
                paths.data_root({"PROJECT_DATA_ROOT": str(missing)})

    def test_machine_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "project-data"
            (shared / "radar-sat" / "data" / "output").mkdir(parents=True)
            config = Path(tmp) / "project-data.env"
            config.write_text(f"PROJECT_DATA_ROOT={shared}\n")
            with mock.patch.dict(
                paths.os.environ,
                {"PROJECT_DATA_CONFIG": str(config)},
                clear=True,
            ):
                self.assertEqual(
                    paths.output_root(),
                    (shared / "radar-sat" / "data" / "output").resolve(),
                )


if __name__ == "__main__":
    unittest.main()
