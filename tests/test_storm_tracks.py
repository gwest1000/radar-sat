import unittest

from radarsat.storm_tracks import assemble_tracks, first_approach, storm_region
from pyproj import Transformer
from shapely.geometry import Point


RUN = "2026-10-30T12:00:00Z"


def collection(time, coordinates):
    return {"reference_datetime": RUN, "features": [{
        "geometry": {"type": "Point", "coordinates": coordinates},
        "properties": {"disturbance_id": "storm-a", "forecast_datetime": time,
                       "pressure": 995.5},
    }]}


class StormTracksTests(unittest.TestCase):
    def test_points_keep_their_times_and_pressure_in_all_projections(self):
        first = collection(RUN, [-130, 50])
        last = collection("2026-10-30T15:00:00Z", [-128, 51])
        # Cumulative lines must not duplicate point records or invent pressures.
        first["features"].append({"geometry": {"type": "LineString"}})
        result = assemble_tracks([last, first, first], RUN)
        for tracks in result["domains"].values():
            self.assertEqual(len(tracks), 1)
            self.assertEqual([p["time"] for p in tracks[0]["points"]],
                             [RUN, "2026-10-30T15:00:00Z"])
            self.assertEqual(tracks[0]["points"][0]["pressure"], 995.5)
        point = result["domains"]["bc"][0]["points"][0]
        self.assertTrue(0 < point["x"] < 1 and 0 < point["y"] < 1)

    def test_mixed_runs_rejected(self):
        with self.assertRaises(ValueError):
            assemble_tracks([{"reference_datetime": "wrong-run"}], RUN)

    def test_remote_systems_excluded(self):
        self.assertEqual(assemble_tracks([
            collection(RUN, [-50, 50]),
            collection("2026-10-30T15:00:00Z", [-48, 51]),
        ], RUN)["domains"]["bc"], [])

    def test_four_hundred_km_from_bc_including_islands(self):
        project = Transformer.from_crs(4326, 3005, always_xy=True)
        # Victoria/VI and Fort Nelson are inside; remote Pacific and Saskatchewan are outside.
        for lon, lat, included in [(-123.37, 48.43, True), (-122.7, 58.8, True),
                                   (-140, 48, False), (-105, 54, False)]:
            with self.subTest(lon=lon, lat=lat):
                self.assertEqual(storm_region().covers(Point(project.transform(lon, lat))), included)
        # The northern border is at 60 N: ~200 km north qualifies; ~450 km does not.
        self.assertTrue(storm_region().covers(Point(project.transform(-125, 61.8))))
        self.assertFalse(storm_region().covers(Point(project.transform(-125, 64))))

    def test_offshore_vancouver_island_low_is_included(self):
        # The 30 Oct 2026 12Z RDPS low remains ~328 km from BC at closest approach.
        result = assemble_tracks([
            collection(RUN, [-128.017, 46.336]),
            collection("2026-10-30T18:00:00Z", [-127.902, 46.403]),
        ], RUN)
        self.assertEqual(len(result["domains"]["bc"]), 1)
        self.assertEqual(result["selection"]["bufferKm"], 400)

    def test_track_crossing_between_samples_qualifies(self):
        points = [{"lon": lon, "lat": 54, "time": f"2026-10-30T{hour}:00:00Z"}
                  for lon, hour in [(-150, "12"), (-105, "15")]]
        self.assertEqual(first_approach(points), "2026-10-30T15:00:00Z")
