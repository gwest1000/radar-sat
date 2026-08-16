from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radarsat.config import Domain
from radarsat.geomet import GeoMetClient, LayerTimeline
from radarsat.pipeline import ingest_geomet


UTC = dt.timezone.utc
VALID = dt.datetime(2026, 8, 16, 22, 48, tzinfo=UTC)


class GeoMetClientTests(unittest.TestCase):
    def test_timeline_is_reused_within_a_cycle(self) -> None:
        response = mock.Mock()
        response.content = b"""<?xml version="1.0"?>
        <WMS_Capabilities xmlns="http://www.opengis.net/wms">
          <Capability><Layer><Layer>
            <Name>RADAR_1KM_RRAI</Name>
            <Dimension name="time" units="ISO8601" default="2026-08-16T22:48:00Z">
              2026-08-16T22:36:00Z/2026-08-16T22:48:00Z/PT6M
            </Dimension>
          </Layer></Layer></Capability>
        </WMS_Capabilities>"""
        response.raise_for_status.return_value = None
        with GeoMetClient() as client:
            client.session.get = mock.Mock(return_value=response)
            first = client.timeline("RADAR_1KM_RRAI")
            second = client.timeline("RADAR_1KM_RRAI")

            self.assertIs(first, second)
            self.assertEqual(first.default, VALID)
            self.assertEqual(client.session.get.call_count, 1)

    def test_empty_explicit_layer_selection_does_not_expand_to_defaults(self) -> None:
        domain = Domain(
            id="test",
            title="Test",
            west=-125,
            south=48,
            east=-120,
            north=53,
            crs="EPSG:3857",
            width=120,
            height=90,
            tier="bc",
            projected_bounds=(0, 0, 120_000, 90_000),
        )
        client = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result = ingest_geomet(
                client,
                Path(temporary),
                domain,
                1,
                True,
                include_layers=(),
            )
        self.assertEqual(result, {})
        client.timeline.assert_not_called()
        client.get_map.assert_not_called()

    def test_operational_mode_isolates_a_failed_radar_layer(self) -> None:
        domain = Domain(
            id="test",
            title="Test",
            west=-125,
            south=48,
            east=-120,
            north=53,
            crs="EPSG:3857",
            width=120,
            height=90,
            tier="bc",
            projected_bounds=(0, 0, 120_000, 90_000),
        )
        timelines = {
            "RADAR_1KM_RRAI": LayerTimeline("RADAR_1KM_RRAI", (VALID,), VALID),
            "RADAR_COVERAGE_RRAI.INV": LayerTimeline(
                "RADAR_COVERAGE_RRAI.INV", (VALID,), VALID
            ),
        }
        client = mock.Mock()

        def get_map(layer, _domain, _valid_time):
            if layer.id == "radar-rain":
                raise ConnectionError("temporary refusal")
            return b"coverage"

        client.get_map.side_effect = get_map
        errors: list[str] = []
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch("radarsat.pipeline.save_coverage") as save_coverage,
            mock.patch("radarsat.pipeline.write_metadata"),
        ):
            result = ingest_geomet(
                client,
                Path(temporary),
                domain,
                1,
                True,
                include_layers=("radar-rain", "radar-coverage"),
                preloaded_timelines=timelines,
                continue_on_error=True,
                errors=errors,
            )

        self.assertEqual(set(result), {"radar-rain", "radar-coverage"})
        self.assertEqual(len(errors), 1)
        self.assertIn("test/radar-rain 2026-08-16T22:48:00Z", errors[0])
        save_coverage.assert_called_once()


if __name__ == "__main__":
    unittest.main()
