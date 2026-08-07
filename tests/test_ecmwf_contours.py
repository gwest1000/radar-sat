from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np

from radarsat.config import Domain
from radarsat.ecmwf_contours import UTC, available_runs, interpolate_global_field


class EcmwfContourTests(unittest.TestCase):
    def test_newest_three_hour_covering_run_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for date, cycle in (("20260807", "00"), ("20260807", "12")):
                run = root / date / cycle
                run.mkdir(parents=True)
                (run / "pl_cf.grib2").touch()
                (run / "sfc_cf.grib2").touch()
            valid = dt.datetime(2026, 8, 7, 21, tzinfo=UTC)
            runs = available_runs(root, valid)
            self.assertEqual(runs[0][1:], (dt.datetime(2026, 8, 7, 12, tzinfo=UTC), 9))
            self.assertEqual(available_runs(root, valid + dt.timedelta(hours=1)), [])

    def test_global_interpolation_wraps_across_zero_longitude(self) -> None:
        domain = Domain(
            id="test",
            title="test",
            west=-1,
            south=-1,
            east=1,
            north=1,
            crs="EPSG:4326",
            width=5,
            height=3,
            tier="broad",
        )
        latitudes = np.array([-1.0, 0.0, 1.0])
        longitudes = np.array([0.0, 1.0, 359.0])
        values = np.tile(np.array([10.0, 11.0, 9.0]), (3, 1))
        rendered = interpolate_global_field(values, latitudes, longitudes, domain)
        self.assertTrue(np.isfinite(rendered).all())
        self.assertAlmostEqual(float(rendered[1, 2]), 10.0, places=4)


if __name__ == "__main__":
    unittest.main()
