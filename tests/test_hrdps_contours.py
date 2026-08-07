from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np

from radarsat.hrdps_contours import (
    FIELD_STYLES,
    UTC,
    available_runs,
    model_filename,
    significant_centres,
)


class HrdpsContourTests(unittest.TestCase):
    def test_modern_hourly_field_names(self) -> None:
        height, mslp = FIELD_STYLES
        self.assertEqual(
            model_filename("20260807T18Z", 4, height),
            "20260807T18Z_MSC_HRDPS_HGT_ISBL_0500_RLatLon0.0225_PT004H.grib2",
        )
        self.assertEqual(
            model_filename("20260807T18Z", 4, mslp),
            "20260807T18Z_MSC_HRDPS_PRMSL_MSL_RLatLon0.0225_PT004H.grib2",
        )
        self.assertAlmostEqual(height.linewidth, 2.15 * 1.75)
        self.assertAlmostEqual(mslp.linewidth, 1.05 * 0.75)
        self.assertEqual((height.lower_colour, height.upper_colour), ("#c98735", "#b95750"))
        self.assertGreater(height.label_size, 7.2)
        self.assertGreater(mslp.label_size, 6.4)

    def test_newest_covering_run_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for stamp in ("20260807T12Z", "20260807T18Z", "not-a-run"):
                (root / stamp).mkdir()
            valid = dt.datetime(2026, 8, 7, 22, tzinfo=UTC)
            runs = available_runs(root, valid)
            self.assertEqual(runs[0][:2], ("20260807T18Z", dt.datetime(2026, 8, 7, 18, tzinfo=UTC)))
            self.assertEqual(runs[0][2], 4)

    def test_prominence_filter_rejects_small_wiggles(self) -> None:
        y, x = np.mgrid[:121, :161]
        values = (
            560.0
            + 12.0 * np.exp(-((x - 45) ** 2 + (y - 55) ** 2) / 300.0)
            - 10.0 * np.exp(-((x - 118) ** 2 + (y - 67) ** 2) / 350.0)
            + 0.25 * np.sin(x / 2.0) * np.sin(y / 3.0)
        ).astype(np.float32)
        centres = significant_centres(
            values,
            10.0,
            smooth_km=30.0,
            background_km=180.0,
            search_radius_km=250.0,
            prominence=2.0,
        )
        self.assertEqual({centre.kind for centre in centres}, {"H", "L"})
        self.assertLessEqual(len(centres), 2)
        high = next(centre for centre in centres if centre.kind == "H")
        low = next(centre for centre in centres if centre.kind == "L")
        self.assertLess(abs(high.x - 45), 15)
        self.assertLess(abs(low.x - 118), 15)


if __name__ == "__main__":
    unittest.main()
