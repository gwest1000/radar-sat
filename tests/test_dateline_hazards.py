from __future__ import annotations

import datetime as dt
import unittest

from radarsat.active_fires import project_active_fires
from radarsat.config import DOMAINS
from radarsat.hotspots import project_hotspots


UTC = dt.timezone.utc
VALID = dt.datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


class DatelineHazardTests(unittest.TestCase):
    def test_western_longitude_hotspot_projects_onto_pacific_grid(self) -> None:
        features = [{
            "properties": {
                "rep_date": "2026-07-23T01:00:00Z",
                "lon": -120.0,
                "lat": 50.0,
                "frp": 25.0,
            }
        }]

        points = project_hotspots(features, DOMAINS["north-pacific"], VALID)

        self.assertEqual(len(points), 1)
        self.assertGreaterEqual(points[0].x, 0)
        self.assertLess(points[0].x, DOMAINS["north-pacific"].width)

    def test_western_longitude_active_fire_projects_onto_pacific_grid(self) -> None:
        features = [{
            "geometry": {"type": "Point", "coordinates": [-120.0, 50.0]},
            "properties": {
                "fire_was_prescribed": 0,
                "fire_size": 100.0,
                "status_date": "2026-07-23T01:00:00Z",
            },
        }]

        points = project_active_fires(
            features,
            [],
            DOMAINS["north-pacific"],
            VALID,
        )

        self.assertEqual(len(points), 1)
        self.assertGreaterEqual(points[0].x, 0)
        self.assertLess(points[0].x, DOMAINS["north-pacific"].width)


if __name__ == "__main__":
    unittest.main()
