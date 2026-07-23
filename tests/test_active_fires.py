from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radarsat.active_fires import (
    CANADA_WILDFIRE_OF_NOTE_CODE,
    CANADA_SOURCE_CODE,
    STANDARD_FIRE_CODE,
    US_LARGE_INCIDENT_CODE,
    UNITED_STATES_SOURCE_CODE,
    fetch_bc_active_fires,
    fetch_canadian_active_fires,
    project_active_fires,
)
from radarsat.config import DOMAINS, LAYERS, Domain
from radarsat.pipeline import (
    FIRE_OVERLAY_RENDER_VERSION,
    derive_fire_overlays,
    frame_path,
    ingest_active_fire_snapshot,
    metadata_path,
    write_metadata,
)
from radarsat.point_frames import write_point_frame


UTC = dt.timezone.utc
VALID = dt.datetime(2026, 7, 22, 19, 17, tzinfo=UTC)


class JsonResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class ActiveFireTests(unittest.TestCase):
    def test_canadian_query_selects_current_temporal_records(self) -> None:
        request = mock.Mock(
            return_value=JsonResponse({"type": "FeatureCollection", "features": []})
        )

        self.assertEqual(fetch_canadian_active_fires(VALID, request_get=request), [])

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["typeName"], "public:cwfif_national_activefires")
        self.assertIn("record_end > '2026-07-22T19:17:00Z'", params["CQL_FILTER"])

    def test_bcws_query_selects_active_fires_and_official_note_flag(self) -> None:
        request = mock.Mock(
            return_value=JsonResponse({"type": "FeatureCollection", "features": []})
        )

        self.assertEqual(fetch_bc_active_fires(request_get=request), [])

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["where"], "FIRE_STATUS <> 'Out'")
        self.assertIn("FIRE_OF_NOTE_IND", params["outFields"])
        self.assertEqual(params["outSR"], "4326")

    def test_projection_filters_prescribed_fires_and_converts_us_acres(self) -> None:
        domain = DOMAINS["north-america"]
        canadian = [
            {
                "geometry": {"type": "Point", "coordinates": [-120.0, 52.0]},
                "properties": {
                    "fire_was_prescribed": 0,
                    "fire_size": 25.0,
                    "status_date": "2026-07-22T18:17:00Z",
                },
            },
            {
                "geometry": {"type": "Point", "coordinates": [-122.0, 51.0]},
                "properties": {"fire_was_prescribed": 1, "fire_size": 50.0},
            },
        ]
        united_states = [
            {
                "geometry": {"type": "Point", "coordinates": [-119.0, 40.0]},
                "properties": {
                    "IncidentSize": 100.0,
                    "ModifiedOnDateTime_dt": int(
                        (VALID - dt.timedelta(minutes=30)).timestamp() * 1000
                    ),
                },
            }
        ]

        points = project_active_fires(canadian, united_states, domain, VALID)

        self.assertEqual(len(points), 2)
        canada = next(point for point in points if point.source_code == CANADA_SOURCE_CODE)
        united_states_point = next(
            point for point in points if point.source_code == UNITED_STATES_SOURCE_CODE
        )
        self.assertEqual(canada.size_hectares, 25.0)
        self.assertEqual(canada.status_age_minutes, 60.0)
        self.assertEqual(canada.highlight_code, STANDARD_FIRE_CODE)
        self.assertAlmostEqual(united_states_point.size_hectares, 40.468564224)
        self.assertEqual(united_states_point.status_age_minutes, 30.0)
        self.assertEqual(united_states_point.highlight_code, STANDARD_FIRE_CODE)

    def test_projection_uses_authority_flags_instead_of_size_threshold(self) -> None:
        domain = DOMAINS["north-america"]
        canadian = [
            {
                "geometry": {"type": "Point", "coordinates": [-121.0, 50.0]},
                "properties": {
                    "agency_code": "BC",
                    "fire_size": 99_000.0,
                    "status_date": "2026-07-22T18:17:00Z",
                },
            }
        ]
        bcws = [
            {
                "geometry": {"type": "Point", "coordinates": [-121.1, 50.1]},
                "properties": {
                    "CURRENT_SIZE": 250.0,
                    "FIRE_OF_NOTE_IND": "Y",
                },
            },
            {
                "geometry": {"type": "Point", "coordinates": [-122.1, 51.1]},
                "properties": {
                    "CURRENT_SIZE": 13_000.0,
                    "FIRE_OF_NOTE_IND": "N",
                },
            },
        ]
        united_states = [
            {
                "geometry": {"type": "Point", "coordinates": [-119.0, 40.0]},
                "properties": {
                    "IncidentSize": 100.0,
                    "ICS209ReportStatus": "U",
                },
            },
            {
                "geometry": {"type": "Point", "coordinates": [-118.0, 39.0]},
                "properties": {
                    "IncidentSize": 100_000.0,
                    "ICS209ReportStatus": "F",
                },
            },
        ]

        points = project_active_fires(
            canadian,
            united_states,
            domain,
            VALID,
            bc_features=bcws,
        )

        self.assertEqual(len(points), 4)
        canada_codes = {
            point.highlight_code
            for point in points
            if point.source_code == CANADA_SOURCE_CODE
        }
        us_codes = {
            point.highlight_code
            for point in points
            if point.source_code == UNITED_STATES_SOURCE_CODE
        }
        self.assertEqual(canada_codes, {STANDARD_FIRE_CODE, CANADA_WILDFIRE_OF_NOTE_CODE})
        self.assertEqual(us_codes, {STANDARD_FIRE_CODE, US_LARGE_INCIDENT_CODE})
        self.assertNotIn(99_000.0, {point.size_hectares for point in points})

    @mock.patch("radarsat.pipeline.fetch_us_active_fires")
    @mock.patch("radarsat.pipeline.fetch_bc_active_fires")
    @mock.patch("radarsat.pipeline.fetch_canadian_active_fires")
    def test_snapshot_writes_combined_point_frame(
        self,
        fetch_canadian: mock.Mock,
        fetch_bc: mock.Mock,
        fetch_us: mock.Mock,
    ) -> None:
        fetch_canadian.return_value = [
            {
                "geometry": {"type": "Point", "coordinates": [-120.0, 52.0]},
                "properties": {
                    "fire_was_prescribed": 0,
                    "fire_size": 3.5,
                    "status_date": "2026-07-22T18:17:00Z",
                },
            }
        ]
        fetch_bc.return_value = []
        fetch_us.return_value = [
            {
                "geometry": {"type": "Point", "coordinates": [-119.0, 40.0]},
                "properties": {
                    "IncidentSize": 10.0,
                    "ModifiedOnDateTime_dt": int(VALID.timestamp() * 1000),
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["north-america"]

            summary = ingest_active_fire_snapshot(root, domain, VALID)

            valid_time = VALID.replace(minute=10, second=0, microsecond=0)
            point_path = frame_path(root, domain, LAYERS["active-fire-points"], valid_time)
            payload = json.loads(point_path.read_text())
            metadata = json.loads(
                metadata_path(root, domain, LAYERS["active-fire-points"], valid_time).read_text()
            )
            self.assertEqual(summary["pointCount"], 2)
            self.assertEqual(payload["pointSchema"], [
                "x",
                "y",
                "statusAgeMinutes",
                "sizeHectares",
                "sourceCode",
                "highlightCode",
            ])
            self.assertEqual({point[4] for point in payload["points"]}, {1, 2})
            self.assertEqual({point[5] for point in payload["points"]}, {0})
            self.assertEqual(metadata["renderVersion"], 3)
            self.assertEqual(metadata["source"], "NRCan CWFIS + BCWS + NIFC WFIGS")

    @mock.patch("radarsat.pipeline.fetch_us_active_fires")
    @mock.patch("radarsat.pipeline.fetch_bc_active_fires")
    @mock.patch("radarsat.pipeline.fetch_canadian_active_fires")
    def test_snapshot_retains_last_complete_frame_when_one_agency_is_throttled(
        self,
        fetch_canadian: mock.Mock,
        fetch_bc: mock.Mock,
        fetch_us: mock.Mock,
    ) -> None:
        canadian = [{
            "geometry": {"type": "Point", "coordinates": [-120.0, 52.0]},
            "properties": {
                "fire_was_prescribed": 0,
                "fire_size": 3.5,
                "status_date": "2026-07-22T18:17:00Z",
            },
        }]
        united_states = [{
            "geometry": {"type": "Point", "coordinates": [-119.0, 40.0]},
            "properties": {
                "IncidentSize": 10.0,
                "ModifiedOnDateTime_dt": int(VALID.timestamp() * 1000),
            },
        }]
        fetch_canadian.return_value = canadian
        fetch_bc.return_value = []
        fetch_us.side_effect = [united_states, RuntimeError("Too many requests")]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = DOMAINS["north-america"]
            first_valid = VALID.replace(minute=10, second=0, microsecond=0)
            second_requested = VALID + dt.timedelta(minutes=10)
            second_valid = second_requested.replace(minute=20, second=0, microsecond=0)

            first = ingest_active_fire_snapshot(root, domain, VALID)
            retained = ingest_active_fire_snapshot(root, domain, second_requested)

            self.assertEqual(first["status"], "rendered")
            self.assertEqual(retained["status"], "retained")
            self.assertEqual(retained["validTime"], first_valid.isoformat().replace("+00:00", "Z"))
            self.assertEqual(retained["usFeatureCount"], 1)
            self.assertTrue(any("NIFC WFIGS" in warning for warning in retained["warnings"]))
            self.assertFalse(
                frame_path(root, domain, LAYERS["active-fire-points"], second_valid).exists()
            )

    def test_derived_fire_overlay_combines_point_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = Domain(
                id="bc",
                title="BC",
                west=-125.0,
                south=48.0,
                east=-120.0,
                north=53.0,
                crs="EPSG:4326",
                width=240,
                height=200,
                tier="bc",
                projected_bounds=(-125.0, 48.0, -120.0, 53.0),
            )
            valid_time = VALID.replace(minute=10, second=0, microsecond=0)
            hotspot_layer = LAYERS["hotspot-points"]
            active_layer = LAYERS["active-fire-points"]
            hotspot_path = frame_path(root, domain, hotspot_layer, valid_time)
            active_path = frame_path(root, domain, active_layer, valid_time)
            write_point_frame(
                hotspot_path,
                layer=hotspot_layer.id,
                domain=domain,
                valid_time=valid_time,
                window_start=valid_time - dt.timedelta(hours=24),
                window_end=valid_time,
                age_reference_time=valid_time,
                point_schema=hotspot_layer.point_schema,
                points=[[0.25, 0.25, 60.0, 120.0, 1]],
                age_mode="exact-detection-time",
                age_precision_seconds=60,
            )
            write_point_frame(
                active_path,
                layer=active_layer.id,
                domain=domain,
                valid_time=valid_time,
                window_start=valid_time,
                window_end=valid_time,
                age_reference_time=valid_time,
                point_schema=active_layer.point_schema,
                points=[[0.75, 0.75, None, 250.0, 1, 1]],
                age_mode="source-status-time",
                age_precision_seconds=60,
            )
            write_metadata(
                root,
                domain,
                hotspot_layer,
                valid_time,
                hotspot_path,
                extra={"pointCount": 1, "renderVersion": 2},
            )
            write_metadata(
                root,
                domain,
                active_layer,
                valid_time,
                active_path,
                extra={
                    "pointCount": 1,
                    "renderVersion": 3,
                    "canadianFeatureCount": 1,
                    "bcwsFeatureCount": 1,
                    "usFeatureCount": 1,
                    "sourceErrors": [],
                },
            )

            summary = derive_fire_overlays(root, domain, hours=1)

            overlay_path = frame_path(root, domain, LAYERS["hotspots"], valid_time)
            overlay_metadata = json.loads(
                metadata_path(root, domain, LAYERS["hotspots"], valid_time).read_text()
            )
            self.assertEqual(summary["rendered"], 1)
            self.assertTrue(overlay_path.exists())
            self.assertEqual(
                overlay_metadata["fireOverlayRenderVersion"],
                FIRE_OVERLAY_RENDER_VERSION,
            )
            self.assertEqual(overlay_metadata["activeFireDisplayCount"], 1)
            self.assertEqual(overlay_metadata["hotspotDisplayCount"], 1)
            self.assertEqual(overlay_metadata["activeFireValidTime"], "2026-07-22T19:10:00Z")


if __name__ == "__main__":
    unittest.main()
