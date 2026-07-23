from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.transform import from_bounds
import shapefile

from radarsat.config import LAYERS, Domain
from radarsat.hotspots import render_hotspots
from radarsat.images import lightning_trail, render_watershed_overlay
from radarsat.pipeline import (
    derive_lightning_trails,
    frame_path,
    ingest_hotspot_snapshot,
    metadata_path,
    parse_args,
    run,
    write_metadata,
)
from radarsat.spool import (
    NATIVE_LAYER_IDS,
    NATIVE_SOURCE,
    SpoolIngestResult,
    _lightning_rgba,
    discover_spool,
    ingest_spool,
)


UTC = dt.timezone.utc
VALID = dt.datetime(2026, 7, 21, 0, 12, tzinfo=UTC)


def test_domain(width: int = 120, height: int = 90) -> Domain:
    return Domain(
        id="bc-test",
        title="test",
        west=-125,
        south=48,
        east=-120,
        north=53,
        crs="EPSG:3857",
        width=width,
        height=height,
        tier="bc",
        projected_bounds=(0.0, 0.0, 120_000.0, 90_000.0),
    )


def write_satellite(path: Path, valid: dt.datetime = VALID) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = 45, 60
    y, x = np.indices((height, width))
    values = np.stack(
        (
            (x * 4).astype(np.uint8),
            (y * 5).astype(np.uint8),
            ((x + y) * 2).astype(np.uint8),
        )
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(0, 0, 120_000, 90_000, width, height),
    ) as dataset:
        dataset.write(values)
        dataset.update_tags(VALIDITY_DATETIME=valid.strftime("%Y-%m-%dT%H:%M:%SZ"))


def write_lightning(path: Path, valid: dt.datetime = VALID) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.zeros((45, 60), dtype=np.float32)
    values[20:23, 30:33] = 1.5
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=60,
        height=45,
        count=1,
        dtype="float32",
        nodata=-999.0,
        crs="EPSG:3857",
        transform=from_bounds(0, 0, 120_000, 90_000, 60, 45),
    ) as dataset:
        dataset.write(values, 1)
        dataset.update_tags(1, VALIDITY_DATETIME=valid.strftime("%Y-%m-%dT%H:%M:%SZ"))


def write_gif(path: Path, colour: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (58, 48), colour).save(path, "GIF")


class NativeDiscoveryTests(unittest.TestCase):
    def test_discovery_accepts_only_completed_regular_recognized_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = Path(temporary)
            completed = spool / "satellite" / "20260721T0012Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            write_satellite(completed)
            hidden = spool / "satellite" / ".20260721T0012Z_MSC_GOES-West_NaturalColor_1km.tif"
            write_satellite(hidden)
            symlink = spool / "lightning" / "20260721T0012Z_MSC_Lightning_2.5km.tif"
            symlink.parent.mkdir(parents=True)
            symlink.symlink_to(completed)
            corrupt = spool / "satellite" / "20260721T0012Z_MSC_GOES-West_SnowFog-NightMicrophysics_1km.tif"
            corrupt.write_bytes(b"not-a-geotiff")

            files, rejected = discover_spool(spool, now=VALID)

            self.assertEqual([(item.layer_id, item.path.name) for item in files], [("daynight", completed.name)])
            self.assertEqual(len(rejected), 2)
            self.assertTrue(any("non-symlink" in value for value in rejected))
            self.assertTrue(any("signature" in value for value in rejected))

    def test_standalone_ir_requires_its_documented_two_kilometre_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = Path(temporary)
            correct = spool / "satellite" / "20260721T0012Z_MSC_GOES-West_NightIR_2km.tif"
            wrong = spool / "satellite" / "20260721T0022Z_MSC_GOES-West_NightIR_1km.tif"
            write_satellite(correct)
            write_satellite(wrong, VALID + dt.timedelta(minutes=10))

            files, rejected = discover_spool(spool, now=VALID + dt.timedelta(minutes=10))

            self.assertEqual([(item.layer_id, item.path.name) for item in files], [("ir", correct.name)])
            self.assertTrue(any("expected 2km resolution" in value for value in rejected))


class NativeRenderTests(unittest.TestCase):
    def test_hotspots_render_as_age_coloured_diamonds(self) -> None:
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
                width=120,
                height=100,
                tier="bc",
                projected_bounds=(-125.0, 48.0, -120.0, 53.0),
            )
            now = dt.datetime(2026, 7, 21, 6, 14, tzinfo=UTC)
            features = []
            for index, hours in enumerate((2, 8, 20)):
                features.append(
                    {
                        "properties": {
                            "lat": 49.0 + index,
                            "lon": -124.0 + index,
                            "rep_date": (now - dt.timedelta(hours=hours)).isoformat(),
                            "frp": 10 * (index + 1),
                        }
                    }
                )
            destination = root / "hotspots.png"

            summary = render_hotspots(features, domain, destination, now)

            rendered = np.asarray(Image.open(destination).convert("RGBA"))
            self.assertEqual(summary["detectionCount"], 3)
            self.assertTrue(np.any(np.all(rendered[:, :, :3] == (255, 229, 92), axis=2)))
            self.assertTrue(np.any(np.all(rendered[:, :, :3] == (255, 148, 31), axis=2)))
            self.assertTrue(np.any(np.all(rendered[:, :, :3] == (217, 75, 61), axis=2)))

    @mock.patch("radarsat.pipeline.fetch_hotspots")
    def test_hotspot_snapshot_uses_ten_minute_archive_clock(self, fetch: mock.Mock) -> None:
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
                width=120,
                height=100,
                tier="bc",
                projected_bounds=(-125.0, 48.0, -120.0, 53.0),
            )
            now = dt.datetime(2026, 7, 21, 6, 17, tzinfo=UTC)
            fetch.return_value = [
                {
                    "properties": {
                        "lat": 50.0,
                        "lon": -123.0,
                        "rep_date": (now - dt.timedelta(hours=1)).isoformat(),
                        "frp": 12,
                    }
                }
            ]

            summary = ingest_hotspot_snapshot(root, domain, now)

            self.assertEqual(summary["validTime"], "2026-07-21T06:10:00Z")
            meta = metadata_path(root, domain, LAYERS["hotspots"], now.replace(minute=10))
            payload = json.loads(meta.read_text())
            self.assertEqual(payload["source"], "NRCan CWFIS")
            self.assertEqual(payload["detectionCount"], 1)
            self.assertEqual(payload["sourceLayer"], "public:hotspots_24h")
            self.assertEqual(payload["renderVersion"], 4)

    def test_local_bch_watershed_shapefile_renders_to_aligned_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "watersheds.shp"
            writer = shapefile.Writer(str(source))
            writer.field("ID", "N")
            writer.poly([[(-130.0, 48.0), (-130.0, 50.0), (-128.5, 50.0), (-128.5, 48.0), (-130.0, 48.0)]])
            writer.record(1)
            writer.close()
            source.with_suffix(".prj").write_text(CRS.from_epsg(4326).to_wkt())

            transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
            xmin, ymin = transformer.transform(-131.0, 47.0)
            xmax, ymax = transformer.transform(-128.0, 51.0)
            domain = Domain(
                id="test",
                title="Test",
                west=-131.0,
                south=47.0,
                east=-128.0,
                north=51.0,
                crs="EPSG:3857",
                width=120,
                height=100,
                tier="bc",
                projected_bounds=(xmin, ymin, xmax, ymax),
            )
            destination = root / "bch-watersheds.png"

            render_watershed_overlay(domain, destination, source)

            rendered = np.asarray(Image.open(destination).convert("RGBA"))
            self.assertEqual(rendered.shape, (100, 120, 4))
            self.assertTrue(np.any(rendered[:, :, 3] > 0))
            cyan = (
                (rendered[:, :, 0] > 90)
                & (rendered[:, :, 1] > 190)
                & (rendered[:, :, 2] > 230)
                & (rendered[:, :, 3] > 0)
            )
            self.assertTrue(np.any(cyan))

    def test_lightning_trail_uses_haloed_bolts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            destination = root / "trail.png"
            image = Image.new("RGBA", (80, 60), (0, 0, 0, 0))
            image.putpixel((40, 30), (127, 0, 0, 255))
            image.save(source, "PNG")

            lightning_trail([source, None, None], destination)

            rendered = np.asarray(Image.open(destination).convert("RGBA"))
            alpha = rendered[:, :, 3]
            y, x = np.where(alpha > 0)
            self.assertGreater(len(x), 230)
            self.assertLess(len(x), 800)
            # The asymmetric lightning silhouette is taller than it is wide.
            self.assertGreater(y.max() - y.min(), x.max() - x.min())
            white_core = (
                (rendered[:, :, 0] >= 240)
                & (rendered[:, :, 1] >= 240)
                & (rendered[:, :, 2] >= 240)
                & (alpha > 0)
            )
            self.assertTrue(np.any(white_core))
            self.assertTrue(np.any((alpha > 0) & (alpha < 120)))

    def test_lightning_density_palette_is_transparent_at_zero_and_red_at_legend_ceiling(self) -> None:
        rgba = _lightning_rgba(np.asarray([[np.nan, 0.0, 0.2, 1.0, 2.0, 5.0]], dtype=np.float32))

        self.assertTrue(np.all(rgba[0, :2, 3] == 0))
        self.assertTrue(np.all(rgba[0, 2:, 3] == 255))
        self.assertTrue(np.array_equal(rgba[0, 2], (0, 2, 204, 255)))
        self.assertTrue(np.array_equal(rgba[0, 3], (148, 252, 105, 255)))
        self.assertTrue(np.array_equal(rgba[0, 4], (127, 0, 0, 255)))
        self.assertTrue(np.array_equal(rgba[0, 4], rgba[0, 5]))

    def test_stale_lightning_is_not_restamped_on_a_current_radar_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            domain = test_domain()
            old = VALID - dt.timedelta(hours=2)
            lightning = frame_path(output, domain, LAYERS["lightning"], old)
            lightning.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (domain.width, domain.height), (0, 45, 255, 255)).save(lightning, "PNG")
            write_metadata(output, domain, LAYERS["lightning"], old, lightning)
            radar = frame_path(output, domain, LAYERS["radar-rain"], VALID)
            radar.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0)).save(radar, "PNG")
            write_metadata(output, domain, LAYERS["radar-rain"], VALID, radar)

            # Simulate an incorrectly stamped asset left by an older renderer.
            stale_trail = frame_path(output, domain, LAYERS["lightning-trail"], VALID)
            stale_trail.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (domain.width, domain.height), (255, 255, 255, 255)).save(stale_trail, "PNG")
            write_metadata(output, domain, LAYERS["lightning-trail"], VALID, stale_trail)

            derive_lightning_trails(output, domain, {}, hours=3)

            self.assertFalse(stale_trail.exists())
            self.assertFalse(metadata_path(output, domain, LAYERS["lightning-trail"], VALID).exists())

    def test_recovered_lightning_gap_gets_a_derived_trail_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            domain = test_domain()
            old = VALID - dt.timedelta(hours=8)
            for valid in (old, VALID):
                source = frame_path(output, domain, LAYERS["lightning"], valid)
                source.parent.mkdir(parents=True, exist_ok=True)
                image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
                image.putpixel((10, 10), (0, 45, 255, 255))
                image.save(source, "PNG")
                write_metadata(output, domain, LAYERS["lightning"], valid, source)
            radar = frame_path(output, domain, LAYERS["radar-rain"], VALID)
            radar.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0)).save(radar, "PNG")
            write_metadata(output, domain, LAYERS["radar-rain"], VALID, radar)

            derive_lightning_trails(output, domain, {}, hours=12)

            self.assertTrue(
                frame_path(output, domain, LAYERS["lightning-trail"], old).exists()
            )

    def test_native_recovery_window_renders_backlog_older_than_geomet_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool, output = root / "spool", root / "output"
            old = VALID - dt.timedelta(hours=8)
            for valid in (old, VALID):
                path = spool / "satellite" / (
                    f"{valid:%Y%m%dT%H%MZ}_MSC_GOES-West_DayVis-NightIR_1km.tif"
                )
                write_satellite(path, valid)

            result = ingest_spool(
                spool,
                output,
                test_domain(),
                hours=12,
                latest_only=False,
                now=VALID,
            )

            self.assertEqual(result.rendered["daynight"], 2)
            self.assertTrue(
                frame_path(output, test_domain(), LAYERS["daynight"], old).exists()
            )

    def test_native_geotiffs_replace_wms_frames_and_write_standard_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool, output = root / "spool", root / "output"
            satellite = spool / "satellite" / "20260721T0012Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            lightning = spool / "lightning" / "20260721T0012Z_MSC_Lightning_2.5km.tif"
            write_satellite(satellite)
            write_lightning(lightning)
            domain = test_domain()

            # A same-time WMS bootstrap frame must be replaced when native data arrives.
            old_frame = frame_path(output, domain, LAYERS["daynight"], VALID)
            old_frame.parent.mkdir(parents=True)
            Image.new("RGB", (domain.width, domain.height), "red").save(old_frame, "WEBP")
            write_metadata(output, domain, LAYERS["daynight"], VALID, old_frame)

            result = ingest_spool(spool, output, domain, 1, False, now=VALID)

            self.assertEqual(result.rendered, {"daynight": 1, "lightning": 1})
            day_meta = json.loads(metadata_path(output, domain, LAYERS["daynight"], VALID).read_text())
            self.assertEqual(day_meta["source"], NATIVE_SOURCE)
            self.assertEqual(day_meta["sourceFormat"], "GeoTIFF")
            self.assertEqual(day_meta["sourceTimes"]["native"], "2026-07-21T00:12:00Z")
            with Image.open(frame_path(output, domain, LAYERS["daynight"], VALID)) as image:
                self.assertEqual(image.size, (domain.width, domain.height))
                self.assertNotEqual(image.convert("RGB").getpixel((domain.width // 2, domain.height // 2)), (255, 0, 0))
            with Image.open(frame_path(output, domain, LAYERS["lightning"], VALID)) as image:
                rgba = np.asarray(image.convert("RGBA"))
                self.assertGreater(int(np.count_nonzero(rgba[:, :, 3])), 0)
                self.assertLess(int(np.count_nonzero(rgba[:, :, 3])), domain.width * domain.height // 10)
            lightning_points = frame_path(
                output,
                domain,
                LAYERS["lightning-points"],
                VALID,
            )
            point_payload = json.loads(lightning_points.read_text())
            self.assertEqual(point_payload["pointSchema"], ["x", "y", "ageMinutes", "count"])
            self.assertEqual(point_payload["ageReferenceTime"], "2026-07-21T00:12:00Z")
            self.assertGreater(len(point_payload["points"]), 0)
            point_metadata = json.loads(
                metadata_path(output, domain, LAYERS["lightning-points"], VALID).read_text()
            )
            self.assertIn("not strokes", point_metadata["countMeaning"])

    def test_site_montage_requires_an_exact_four_station_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool, output = root / "spool", root / "output"
            colours = ((190, 30, 30), (30, 190, 30), (30, 30, 190), (190, 190, 30))
            stations = ("CASAG", "CASHP", "CASSS", "CASPG")
            for station, colour in zip(stations, colours):
                suffix = "-Contingency" if station == "CASHP" else ""
                write_gif(
                    spool / "radar" / f"20260721T0012Z_MSC_Radar-DPQPE_{station}_Rain{suffix}.gif",
                    colour,
                )
            # A newer scan at only one site must not manufacture an asynchronous montage.
            write_gif(
                spool / "radar" / "20260721T0018Z_MSC_Radar-DPQPE_CASAG_Rain.gif",
                (255, 255, 255),
            )
            domain = test_domain(400, 300)

            result = ingest_spool(
                spool,
                output,
                domain,
                hours=1,
                latest_only=False,
                now=VALID + dt.timedelta(minutes=6),
            )

            self.assertEqual(result.timelines["site-radar"], [VALID])
            self.assertEqual(result.rendered["site-radar"], 1)
            self.assertFalse(frame_path(output, domain, LAYERS["site-radar"], VALID + dt.timedelta(minutes=6)).exists())
            montage = frame_path(output, domain, LAYERS["site-radar"], VALID)
            with Image.open(montage) as image:
                self.assertEqual(image.size, (400, 300))
            payload = json.loads(metadata_path(output, domain, LAYERS["site-radar"], VALID).read_text())
            self.assertEqual(payload["source"], NATIVE_SOURCE)
            self.assertEqual(payload["contingencySites"], ["CASHP"])
            self.assertEqual(payload["synchronization"], "exact source timestamp")
            self.assertEqual(set(payload["sourceTimes"]), set(stations))


class PipelineIntegrationTests(unittest.TestCase):
    def test_native_rejection_is_visible_while_good_catalog_still_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            native_result = SpoolIngestResult(
                rejected=["source.tif: RasterioIOError: transient read failure"],
                preserve_files={"source.tif"},
            )
            catalog = output / "catalog.json"
            with (
                mock.patch("radarsat.pipeline.GeoMetClient") as client_class,
                mock.patch("radarsat.pipeline.ensure_static_assets"),
                mock.patch("radarsat.pipeline.ingest_geomet", return_value={}),
                mock.patch("radarsat.pipeline.derive_lightning_trails"),
                mock.patch("radarsat.pipeline.ingest_raw_satellite", return_value={"status": "unchanged"}),
                mock.patch(
                    "radarsat.pipeline.ingest_goes_hazards",
                    return_value={"status": "unchanged"},
                ) as hazard_ingest,
                mock.patch("radarsat.pipeline.prune"),
                mock.patch("radarsat.pipeline.write_catalog", return_value=catalog),
                mock.patch("radarsat.spool.ingest_spool", return_value=native_result),
            ):
                client_class.return_value.__enter__.return_value = object()
                run(output, ["bc"], 3, False, Path(temporary) / "spool", "auto")

            hazard_ingest.assert_called_once_with(output, ["bc"])
            status = json.loads((output / "status" / "ingest.json").read_text())
            self.assertEqual(status["status"], "warning")
            self.assertEqual(
                status["spool"]["domains"]["bc"]["preserveFiles"],
                ["source.tif"],
            )

    def test_only_mode_preserves_geomet_for_composite_and_ptype(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            native_result = SpoolIngestResult(rendered={"daynight": 1})
            catalog = output / "catalog.json"
            with (
                mock.patch("radarsat.pipeline.GeoMetClient") as client_class,
                mock.patch("radarsat.pipeline.ensure_static_assets"),
                mock.patch("radarsat.pipeline.ingest_geomet", return_value={}) as geomet,
                mock.patch("radarsat.pipeline.derive_lightning_trails"),
                mock.patch("radarsat.pipeline.ingest_raw_satellite", return_value={"status": "unchanged"}),
                mock.patch("radarsat.pipeline.ingest_goes_hazards", return_value={"status": "unchanged"}),
                mock.patch("radarsat.pipeline.prune"),
                mock.patch("radarsat.pipeline.write_catalog", return_value=catalog),
                mock.patch(
                    "radarsat.spool.ingest_spool", return_value=native_result
                ) as native_ingest,
            ):
                client_class.return_value.__enter__.return_value = object()
                run(output, ["bc"], 1, True, Path(temporary) / "spool", "only")

            self.assertEqual(native_ingest.call_args.args[3], 12.0)
            self.assertEqual(geomet.call_args.args[3], 1)
            excluded = geomet.call_args.kwargs["exclude_layers"]
            self.assertEqual(excluded, set(NATIVE_LAYER_IDS))
            self.assertNotIn("radar-rain", excluded)
            self.assertNotIn("ptype", excluded)
            status = json.loads((output / "status" / "ingest.json").read_text())
            self.assertEqual(status["spool"]["mode"], "only")
            self.assertEqual(status["spool"]["ingestHours"], 12.0)

    def test_cli_exposes_spool_controls(self) -> None:
        args = parse_args(
            [
                "--spool-root",
                "/tmp/radarsat-spool",
                "--spool-mode",
                "off",
                "--spool-hours",
                "9",
            ]
        )
        self.assertEqual(args.spool_root, Path("/tmp/radarsat-spool"))
        self.assertEqual(args.spool_mode, "off")
        self.assertEqual(args.spool_hours, 9)


if __name__ == "__main__":
    unittest.main()
