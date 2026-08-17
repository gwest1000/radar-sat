from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from radarsat.config import Domain
from radarsat.ecmwf_contours import (
    UTC,
    available_interpolation_runs,
    available_runs,
    interpolate_global_field,
    interpolate_in_time,
    update_recent,
)
from radarsat.hrdps_contours import FIELD_STYLES, render_contours


class EcmwfContourTests(unittest.TestCase):
    def test_overview_contours_render_at_double_resolution_with_compact_styling(self) -> None:
        domain = Domain(
            id="north-america",
            title="North America",
            west=-170,
            south=10,
            east=-50,
            north=75,
            crs="EPSG:4326",
            width=160,
            height=120,
            tier="broad",
        )
        y, x = np.mgrid[: domain.height, : domain.width]
        height_values = 552 + x * 0.20 + y * 0.08
        pressure_values = 99_600 + x * 9 + y * 5
        height_style = replace(FIELD_STYLES[0], layer_id="ecmwf-hgt500")
        pressure_style = replace(FIELD_STYLES[1], layer_id="ecmwf-mslp")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            height_path = root / "hgt500.png"
            pressure_path = root / "mslp.png"
            height_summary = render_contours(
                height_values,
                domain,
                height_style,
                height_path,
            )
            pressure_summary = render_contours(
                pressure_values,
                domain,
                pressure_style,
                pressure_path,
            )

            with Image.open(height_path) as rendered:
                self.assertEqual(rendered.size, (320, 240))
            self.assertAlmostEqual(
                height_summary["lineWidth"],
                FIELD_STYLES[0].linewidth * 0.5625,
            )
            self.assertAlmostEqual(
                pressure_summary["lineWidth"],
                FIELD_STYLES[1].linewidth * 0.90,
            )
            self.assertEqual(height_summary["labelScale"], 0.45)
            self.assertEqual(height_summary["centreScale"], 0.50)

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
            hourly = available_interpolation_runs(
                root,
                valid + dt.timedelta(hours=1),
            )
            self.assertEqual(hourly[0][1:5], (
                dt.datetime(2026, 8, 7, 12, tzinfo=UTC),
                10,
                9,
                12,
            ))
            self.assertAlmostEqual(hourly[0][5], 1 / 3)

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

    def test_hourly_field_is_a_linear_blend_of_three_hour_fields(self) -> None:
        lower = np.asarray([[0.0, 30.0], [60.0, np.nan]], dtype=np.float32)
        upper = np.asarray([[30.0, 60.0], [90.0, np.nan]], dtype=np.float32)
        blended = interpolate_in_time(lower, upper, 1 / 3)
        np.testing.assert_allclose(
            blended,
            np.asarray([[10.0, 40.0], [70.0, np.nan]], dtype=np.float32),
            equal_nan=True,
        )

    def test_recovery_renders_hourly_for_one_day_then_three_hourly(self) -> None:
        now = dt.datetime(2026, 8, 7, 12, tzinfo=UTC)
        with mock.patch(
            "radarsat.ecmwf_contours.render_valid_time",
            return_value={"status": "unchanged"},
        ) as render:
            update_recent(Path("output"), Path("data"), hours=30, now=now)

        valid_times = [call.args[2] for call in render.call_args_list]
        self.assertEqual(len(valid_times), 27)
        self.assertEqual(valid_times[0], now)
        self.assertIn(now - dt.timedelta(hours=24), valid_times)
        self.assertIn(dt.datetime(2026, 8, 6, 9, tzinfo=UTC), valid_times)
        self.assertNotIn(dt.datetime(2026, 8, 6, 11, tzinfo=UTC), valid_times)


if __name__ == "__main__":
    unittest.main()
