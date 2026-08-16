from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.transform import from_bounds
import shapefile

from radarsat.config import LAYERS, VIEWPORTS, Domain, regional_layer_id
from radarsat.hotspots import render_fire_overlay, render_hotspots
from radarsat.images import lightning_trail, render_transmission_overlay, render_watershed_overlay
from radarsat.pipeline import (
    BC_LIGHTNING_WIDTH,
    derive_lightning_trails,
    frame_path,
    geomet_render_version,
    ingest_hotspot_snapshot,
    metadata_path,
    parse_args,
    precipitation_render_domain,
    run,
    write_metadata,
)
from radarsat.spool import (
    NATIVE_LAYER_IDS,
    NATIVE_SOURCE,
    NativeFile,
    SpoolIngestResult,
    _lightning_rgba,
    discover_spool,
    ingest_spool,
    render_satellite,
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
            geocolor = spool / "satellite" / "20260721T0012Z_MSC_GOES-West_GeoColor_1km.tif"
            write_satellite(geocolor)
            hidden = spool / "satellite" / ".20260721T0012Z_MSC_GOES-West_NaturalColor_1km.tif"
            write_satellite(hidden)
            symlink = spool / "lightning" / "20260721T0012Z_MSC_Lightning_2.5km.tif"
            symlink.parent.mkdir(parents=True)
            symlink.symlink_to(completed)
            corrupt = spool / "satellite" / "20260721T0012Z_MSC_GOES-West_SnowFog-NightMicrophysics_1km.tif"
            corrupt.write_bytes(b"not-a-geotiff")

            files, rejected = discover_spool(spool, now=VALID)

            self.assertEqual(
                [(item.layer_id, item.path.name) for item in files],
                [("daynight", completed.name), ("eccc-geocolor", geocolor.name)],
            )
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
    def test_msc_geocolor_uses_its_higher_resolution_bc_render_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "20260721T0012Z_MSC_GOES-West_GeoColor_1km.tif"
            destination = root / "geocolor.webp"
            write_satellite(source)
            native = NativeFile(
                path=source,
                valid_time=VALID,
                layer_id="eccc-geocolor",
                source_layer="satellite/goes/west/GeoColor",
            )
            with (
                mock.patch("radarsat.spool.MSC_GEOCOLOR_WIDTH", 150),
                mock.patch("radarsat.spool.MSC_GEOCOLOR_HEIGHT", 115),
            ):
                render_satellite(native, destination, test_domain())
            with Image.open(destination) as image:
                self.assertEqual(image.size, (150, 115))

    def test_precipitation_overlays_use_screen_sharp_render_grids(self) -> None:
        bc = precipitation_render_domain(Domain(
            id="bc",
            title="BC",
            west=-145,
            south=45,
            east=-108,
            north=63,
            crs="EPSG:3005",
            width=1920,
            height=1472,
            tier="bc",
            projected_bounds=(-550000, -100000, 2450000, 2200000),
        ))
        north_america = precipitation_render_domain(Domain(
            id="north-america",
            title="North America",
            west=-180,
            south=5,
            east=-50,
            north=75,
            crs="EPSG:3857",
            width=1280,
            height=960,
            tier="broad",
        ))
        self.assertEqual((bc.width, bc.height), (3000, 2300))
        self.assertEqual((north_america.width, north_america.height), (2560, 1920))
        self.assertEqual(geomet_render_version("radar-rain"), 1)
        self.assertEqual(geomet_render_version("radar-coverage"), 3)
        self.assertIsNone(geomet_render_version("daynight"))

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

    def test_fire_overlay_renders_filled_and_hollow_flames_on_transparency(self) -> None:
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
            destination = root / "fire-overlay.png"
            summary = render_fire_overlay(
                [
                    [0.20, 0.20, 120.0, 140.0, 1],
                    [0.35, 0.35, 800.0, 150.0, 1],
                ],
                [
                    [0.55, 0.55, None, 25.0, 1, 0],
                    [0.80, 0.80, None, 250.0, 1, 1],
                ],
                domain,
                destination,
            )

            rendered = np.asarray(Image.open(destination).convert("RGBA"))
            alpha = rendered[:, :, 3]
            self.assertEqual(summary["activeFireDisplayCount"], 2)
            self.assertEqual(summary["hotspotDisplayCount"], 2)
            self.assertTrue(np.any(alpha == 0))
            self.assertTrue(np.any(alpha > 0))
            coral = (
                (rendered[:, :, 0] > 245)
                & (rendered[:, :, 1] > 90)
                & (rendered[:, :, 1] < 145)
                & (rendered[:, :, 2] < 110)
                & (alpha > 0)
            )
            yellow_outline = (
                (rendered[:, :, 0] > 245)
                & (rendered[:, :, 1] > 205)
                & (rendered[:, :, 2] < 130)
                & (alpha > 0)
            )
            self.assertTrue(np.any(coral))
            self.assertTrue(np.any(yellow_outline))
            # The newest thermal detection is an outlined flame, not a filled
            # marker, so its centre remains transparent.
            self.assertEqual(alpha[round(0.20 * 199) + 1, round(0.20 * 239)], 0)

            broad_destination = root / "fire-overlay-broad.png"
            render_fire_overlay(
                [],
                [[0.55, 0.55, None, 25.0, 1, 1]],
                domain,
                broad_destination,
                output_width=480,
                symbol_reference_width=360,
                blur_glow=False,
            )
            with Image.open(broad_destination) as broad_image:
                self.assertEqual(broad_image.size, (480, 400))

            notable_row = [[0.55, 0.55, None, 250.0, 1, 1]]
            bc_notable_destination = root / "fire-overlay-bc-notable.png"
            render_fire_overlay(
                [],
                notable_row,
                domain,
                bc_notable_destination,
                output_width=480,
                symbol_reference_width=360,
                blur_glow=False,
            )
            overview_domain = replace(domain, id="north-america")
            overview_destination = root / "fire-overlay-overview.png"
            render_fire_overlay(
                [],
                notable_row,
                overview_domain,
                overview_destination,
                output_width=480,
                symbol_reference_width=360,
                blur_glow=False,
            )
            broad_bbox = Image.open(bc_notable_destination).convert("RGBA").getbbox()
            overview_bbox = Image.open(overview_destination).convert("RGBA").getbbox()
            self.assertIsNotNone(broad_bbox)
            self.assertIsNotNone(overview_bbox)
            assert broad_bbox is not None and overview_bbox is not None
            broad_height = broad_bbox[3] - broad_bbox[1]
            overview_height = overview_bbox[3] - overview_bbox[1]
            self.assertLessEqual(overview_height, round(broad_height * 0.60))

            smaller_destination = root / "fire-overlay-bc-notable-smaller.png"
            render_fire_overlay(
                [],
                notable_row,
                domain,
                smaller_destination,
                output_width=480,
                symbol_reference_width=360,
                notable_size_scale=0.85,
                blur_glow=False,
            )
            smaller_bbox = Image.open(smaller_destination).convert("RGBA").getbbox()
            self.assertIsNotNone(smaller_bbox)
            assert smaller_bbox is not None
            smaller_height = smaller_bbox[3] - smaller_bbox[1]
            self.assertLess(smaller_height, broad_height)
            self.assertLessEqual(smaller_height, round(broad_height * 0.90))

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
            _, x = np.where(alpha > 0)
            self.assertGreater(len(x), 400)
            self.assertLess(len(x), 1_000)
            # The opaque bolt core remains distinct inside the diffuse halo.
            _, core_x = np.where(alpha > 220)
            self.assertGreater(len(core_x), 20)
            self.assertLess(len(core_x), len(x))
            white_core = (
                (rendered[:, :, 0] >= 240)
                & (rendered[:, :, 1] >= 240)
                & (rendered[:, :, 2] >= 240)
                & (alpha > 0)
            )
            self.assertTrue(np.any(white_core))
            # The age-zero bolt and its diffuse halo share one raster, so
            # their timestamp and position cannot drift independently.
            diffuse = (alpha > 0) & (alpha < 120)
            self.assertTrue(np.any(diffuse))
            self.assertTrue(np.all(rendered[diffuse, :3] >= 230))

            hires_destination = root / "trail-hires.png"
            lightning_trail([source, None, None], hires_destination, scale=2)
            with Image.open(hires_destination) as hires_image:
                self.assertEqual(hires_image.size, (160, 120))

            broad_destination = root / "trail-broad.png"
            lightning_trail(
                [source, None, None],
                broad_destination,
                output_width=160,
                symbol_reference_width=120,
                blur_glow=False,
            )
            with Image.open(broad_destination) as broad_image:
                self.assertEqual(broad_image.size, (160, 120))

            flash_destination = root / "arrival-flash.png"
            lightning_trail(
                [source, None, None],
                flash_destination,
                arrival_only=True,
            )
            flash_alpha = np.asarray(
                Image.open(flash_destination).convert("RGBA")
            )[:, :, 3]
            halo_pixels = np.count_nonzero(flash_alpha > 8)
            self.assertGreater(halo_pixels, 60)
            self.assertLess(
                halo_pixels,
                np.count_nonzero(alpha > 0),
            )
            self.assertTrue(np.any((flash_alpha > 0) & (flash_alpha < 120)))
            self.assertGreater(int(flash_alpha.max()), 120)
            self.assertLess(int(flash_alpha.max()), 220)
            flash_rendered = np.asarray(
                Image.open(flash_destination).convert("RGBA")
            )
            self.assertTrue(np.all(flash_rendered[flash_alpha > 0, :3] >= 230))

            regional_destination = root / "trail-region-small.png"
            lightning_trail(
                [source, None, None],
                regional_destination,
                viewport=VIEWPORTS["small"],
                output_width=1920,
            )
            expected_height = round(
                1920
                * (image.height * VIEWPORTS["small"]["height"])
                / (image.width * VIEWPORTS["small"]["width"])
            )
            with Image.open(regional_destination) as regional_image:
                self.assertEqual(regional_image.size, (1920, expected_height))

            detailed_destination = root / "trail-region-detailed.png"
            lightning_trail(
                [source, None, None],
                detailed_destination,
                viewport=VIEWPORTS["small"],
                output_width=3840,
                symbol_reference_width=1440,
                blur_glow=False,
            )
            with Image.open(detailed_destination) as detailed_image:
                self.assertEqual(
                    detailed_image.size,
                    (
                        3840,
                        round(
                            3840
                            * (image.height * VIEWPORTS["small"]["height"])
                            / (image.width * VIEWPORTS["small"]["width"])
                        ),
                    ),
                )
                detailed_alpha = np.asarray(
                    detailed_image.convert("RGBA")
                )[:, :, 3]
            regional_alpha = np.asarray(
                Image.open(regional_destination).convert("RGBA")
            )[:, :, 3]
            regional_y, regional_x = np.where(regional_alpha > 20)
            detailed_y, detailed_x = np.where(detailed_alpha > 20)
            self.assertGreater(
                detailed_y.max() - detailed_y.min(),
                regional_y.max() - regional_y.min(),
            )
            self.assertLess(
                (detailed_y.max() - detailed_y.min()) / 3840,
                (regional_y.max() - regional_y.min()) / 1920,
            )

    def test_transmission_overlay_uses_haloed_geobc_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "transmission-lines.geojson"
            source.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-124.0, 49.0], [-121.0, 52.0]],
                    },
                }],
            }))
            transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
            xmin, ymin = transformer.transform(-125.0, 48.0)
            xmax, ymax = transformer.transform(-120.0, 53.0)
            domain = Domain(
                id="bc",
                title="BC",
                west=-125.0,
                south=48.0,
                east=-120.0,
                north=53.0,
                crs="EPSG:3857",
                width=120,
                height=100,
                tier="bc",
                projected_bounds=(xmin, ymin, xmax, ymax),
            )
            destination = root / "transmission-lines.png"

            render_transmission_overlay(domain, destination, source)

            rendered = np.asarray(Image.open(destination).convert("RGBA"))
            self.assertEqual(rendered.shape, (100, 120, 4))
            self.assertTrue(np.any(rendered[:, :, 3] > 0))
            self.assertTrue(np.any(
                (rendered[:, :, 0] < 130)
                & (rendered[:, :, 1] < 130)
                & (rendered[:, :, 2] < 130)
                & (rendered[:, :, 3] > 0)
            ))
            self.assertTrue(np.any(
                (rendered[:, :, 0] > 235)
                & (rendered[:, :, 1] > 235)
                & (rendered[:, :, 2] > 235)
                & (rendered[:, :, 3] > 0)
            ))

            high_resolution = root / "transmission-lines-2x.png"
            render_transmission_overlay(
                domain,
                high_resolution,
                source,
                output_width=240,
            )
            with Image.open(high_resolution) as high_resolution_image:
                self.assertEqual(high_resolution_image.size, (240, 200))

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

    def test_new_strike_halo_is_embedded_only_in_the_first_display_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            domain = replace(test_domain(), id="bc")
            first_time = VALID.replace(minute=10)
            source = frame_path(output, domain, LAYERS["lightning"], first_time)
            source.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
            image.putpixel((60, 45), (0, 45, 255, 255))
            image.save(source, "PNG")
            write_metadata(output, domain, LAYERS["lightning"], first_time, source)
            for radar_time in (first_time, first_time + dt.timedelta(minutes=10)):
                radar = frame_path(output, domain, LAYERS["radar-rain"], radar_time)
                radar.parent.mkdir(parents=True, exist_ok=True)
                Image.new(
                    "RGBA",
                    (domain.width, domain.height),
                    (0, 0, 0, 0),
                ).save(radar, "PNG")
                write_metadata(output, domain, LAYERS["radar-rain"], radar_time, radar)

            derive_lightning_trails(output, domain, {}, hours=1)

            first_meta = json.loads(metadata_path(
                output,
                domain,
                LAYERS["lightning-trail"],
                first_time,
            ).read_text())
            next_time = first_time + dt.timedelta(minutes=10)
            next_meta = json.loads(metadata_path(
                output,
                domain,
                LAYERS["lightning-trail"],
                next_time,
            ).read_text())
            self.assertTrue(first_meta["newStrikeHalo"])
            self.assertFalse(next_meta["newStrikeHalo"])
            with Image.open(frame_path(
                output,
                domain,
                LAYERS["lightning-trail"],
                first_time,
            )) as first_frame:
                self.assertEqual(first_frame.mode, "RGBA")
                self.assertEqual(first_frame.width, BC_LIGHTNING_WIDTH)
            with Image.open(frame_path(
                output,
                domain,
                LAYERS["lightning-trail"],
                next_time,
            )) as next_frame:
                self.assertEqual(next_frame.mode, "P")
            regional_layer = LAYERS[regional_layer_id("lightning-trail", "small")]
            with Image.open(frame_path(
                output,
                domain,
                regional_layer,
                first_time,
            )) as regional_frame:
                self.assertEqual(regional_frame.width, BC_LIGHTNING_WIDTH)

    def test_hourly_lightning_aggregate_uses_six_ten_minute_bins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            domain = test_domain()
            anchor = VALID.replace(minute=0)
            for index in range(6):
                valid = anchor - dt.timedelta(minutes=10 * index)
                source = frame_path(output, domain, LAYERS["lightning"], valid)
                source.parent.mkdir(parents=True, exist_ok=True)
                image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
                image.putpixel((15 + index * 12, 45), (0, 45, 255, 255))
                image.save(source, "PNG")
                write_metadata(output, domain, LAYERS["lightning"], valid, source)

            derive_lightning_trails(output, domain, {}, hours=2)

            layer = LAYERS["lightning-hour"]
            self.assertTrue(frame_path(output, domain, layer, anchor).is_file())
            payload = json.loads(metadata_path(output, domain, layer, anchor).read_text())
            self.assertEqual(
                set(payload["sourceTimes"]),
                {"age0", "age10", "age20", "age30", "age40", "age50"},
            )

    def test_hourly_lightning_history_extends_beyond_live_trails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            domain = test_domain()
            newest = VALID.replace(minute=0)
            old_hour = newest - dt.timedelta(hours=30)
            for anchor in (old_hour, newest):
                for index in range(6):
                    valid = anchor - dt.timedelta(minutes=10 * index)
                    source = frame_path(output, domain, LAYERS["lightning"], valid)
                    source.parent.mkdir(parents=True, exist_ok=True)
                    image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
                    image.putpixel((15 + index * 12, 45), (0, 45, 255, 255))
                    image.save(source, "PNG")
                    write_metadata(output, domain, LAYERS["lightning"], valid, source)

            derive_lightning_trails(output, domain, {}, hours=48)

            self.assertTrue(
                frame_path(output, domain, LAYERS["lightning-hour"], old_hour).is_file()
            )
            self.assertFalse(
                frame_path(output, domain, LAYERS["lightning-trail"], old_hour).exists()
            )

    def test_recovered_lightning_gap_gets_a_derived_trail_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            domain = test_domain()
            newest = VALID.replace(minute=10)
            old = newest - dt.timedelta(hours=8)
            for valid in (old, newest):
                source = frame_path(output, domain, LAYERS["lightning"], valid)
                source.parent.mkdir(parents=True, exist_ok=True)
                image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
                image.putpixel((60, 45), (0, 45, 255, 255))
                image.save(source, "PNG")
                write_metadata(output, domain, LAYERS["lightning"], valid, source)
            radar = frame_path(output, domain, LAYERS["radar-rain"], newest)
            radar.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0)).save(radar, "PNG")
            write_metadata(output, domain, LAYERS["radar-rain"], newest, radar)

            derive_lightning_trails(output, domain, {}, hours=12)

            self.assertTrue(
                frame_path(output, domain, LAYERS["lightning-trail"], old).exists()
            )
            self.assertFalse(
                frame_path(output, domain, LAYERS["lightning-flash"], old).exists()
            )
            regional_layer = LAYERS[regional_layer_id("lightning-trail", "small")]
            self.assertFalse(frame_path(output, domain, regional_layer, old).exists())

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
    def test_fire_refresh_precedes_unrelated_geomet_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with (
                mock.patch("radarsat.pipeline.GeoMetClient") as client_class,
                mock.patch("radarsat.pipeline.ensure_static_assets"),
                mock.patch(
                    "radarsat.pipeline.ingest_geomet",
                    side_effect=RuntimeError("radar unavailable"),
                ),
                mock.patch("radarsat.pipeline.ingest_hotspot_snapshot") as hotspots,
                mock.patch("radarsat.pipeline.ingest_active_fire_snapshot") as active,
                mock.patch("radarsat.pipeline.derive_fire_overlays") as derive,
            ):
                client_class.return_value.__enter__.return_value = object()
                with self.assertRaisesRegex(RuntimeError, "radar unavailable"):
                    run(output, ["bc"], 3, False, spool_mode="off")

            hotspots.assert_called_once()
            active.assert_called_once()
            derive.assert_called_once()

    def test_broad_native_lightning_renders_live_window_not_entire_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            catalog = output / "catalog.json"
            native_result = SpoolIngestResult(timelines={"lightning": []})
            with (
                mock.patch("radarsat.pipeline.GeoMetClient") as client_class,
                mock.patch("radarsat.pipeline.ensure_static_assets"),
                mock.patch("radarsat.pipeline.ingest_geomet", return_value={}),
                mock.patch("radarsat.pipeline.derive_lightning_trails"),
                mock.patch("radarsat.pipeline.ingest_hotspot_snapshot", return_value={}),
                mock.patch("radarsat.pipeline.ingest_active_fire_snapshot", return_value={}),
                mock.patch("radarsat.pipeline.derive_fire_overlays", return_value={}),
                mock.patch("radarsat.pipeline.ingest_raw_satellite", return_value={"status": "unchanged"}),
                mock.patch("radarsat.pipeline.ingest_goes_hazards", return_value={"status": "unchanged"}),
                mock.patch("radarsat.pipeline.prune"),
                mock.patch("radarsat.pipeline.write_catalog", return_value=catalog),
                mock.patch("radarsat.spool.ingest_spool", return_value=native_result) as native_ingest,
            ):
                client_class.return_value.__enter__.return_value = object()
                run(
                    output,
                    ["north-america"],
                    3,
                    False,
                    Path(temporary) / "spool",
                    "auto",
                )

            self.assertEqual(native_ingest.call_args.args[3], 24.0)
            self.assertEqual(native_ingest.call_args.kwargs["include_layers"], ("lightning",))

    def test_broad_domains_ingest_and_derive_canadian_lightning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            catalog = output / "catalog.json"
            with (
                mock.patch("radarsat.pipeline.GeoMetClient") as client_class,
                mock.patch("radarsat.pipeline.ensure_static_assets"),
                mock.patch("radarsat.pipeline.ingest_geomet", return_value={}) as geomet,
                mock.patch("radarsat.pipeline.derive_lightning_trails") as derive_lightning,
                mock.patch("radarsat.pipeline.ingest_hotspot_snapshot", return_value={}),
                mock.patch("radarsat.pipeline.ingest_active_fire_snapshot", return_value={}),
                mock.patch("radarsat.pipeline.derive_fire_overlays", return_value={}),
                mock.patch("radarsat.pipeline.ingest_raw_satellite", return_value={"status": "unchanged"}),
                mock.patch("radarsat.pipeline.ingest_goes_hazards", return_value={"status": "unchanged"}),
                mock.patch("radarsat.pipeline.prune"),
                mock.patch("radarsat.pipeline.write_catalog", return_value=catalog),
            ):
                client_class.return_value.__enter__.return_value = object()
                run(output, ["north-america"], 3, False, spool_mode="off")

            self.assertEqual(geomet.call_count, 2)
            self.assertNotIn(
                "lightning",
                geomet.call_args_list[0].kwargs["include_layers"],
            )
            self.assertEqual(
                geomet.call_args_list[1].kwargs["include_layers"],
                ("lightning",),
            )
            derive_lightning.assert_called_once()
            self.assertEqual(derive_lightning.call_args.args[1].id, "north-america")

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
