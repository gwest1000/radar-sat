from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h5netcdf
import numpy as np
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.transform import from_bounds

from radarsat.catalog import build_catalog
from radarsat.config import LAYERS, Domain
from radarsat.goes_hazards import (
    ADP_PRODUCT,
    GLM_PRODUCT,
    GLMFlashes,
    GLMWindow,
    GoesHazardClient,
    SmokeProduct,
    classify_smoke,
    decode_smoke_product,
    read_glm_flashes,
    render_glm_bins,
    render_smoke_overlay,
)
from radarsat.pipeline import (
    derive_glm_lightning_trails,
    frame_path,
    ingest_goes_hazards,
    metadata_path,
    write_metadata,
)
from radarsat.raw_satellite import PublicObject


UTC = dt.timezone.utc
VALID = dt.datetime(2026, 7, 21, 6, 40, tzinfo=UTC)


class DiscoveryClient(GoesHazardClient):
    def __init__(self, objects: dict[str, list[tuple[str, int]]]) -> None:
        self.objects = objects

    def list_prefix(self, _bucket: str, prefix: str) -> list[tuple[str, int]]:
        return self.objects.get(prefix, [])

    def close(self) -> None:
        return None


def projected_domain(domain_id: str = "hazard-test", width: int = 200, height: int = 120) -> Domain:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xmin, ymin = transformer.transform(-130.0, 45.0)
    xmax, ymax = transformer.transform(-110.0, 55.0)
    return Domain(
        id=domain_id,
        title="Hazard test",
        west=-130.0,
        south=45.0,
        east=-110.0,
        north=55.0,
        crs="EPSG:3857",
        width=width,
        height=height,
        tier="broad",
        projected_bounds=(xmin, ymin, xmax, ymax),
    )


class HazardDiscoveryTests(unittest.TestCase):
    def test_latest_adp_uses_the_latest_completed_full_disk_object(self) -> None:
        prefix = f"{ADP_PRODUCT}/2026/202/06/"
        client = DiscoveryClient(
            {
                prefix: [
                    (prefix + "OR_ABI-L2-ADPF-M6_G18_s20262020630000_e.nc", 10),
                    (prefix + "OR_ABI-L2-ADPF-M6_G18_s20262020640000_e.nc", 20),
                ]
            }
        )

        selected = client.latest_adp(dt.datetime(2026, 7, 21, 6, 55, tzinfo=UTC))

        self.assertEqual(selected.valid_time, VALID)
        self.assertEqual(selected.size, 20)

    def test_glm_discovery_skips_a_newer_partial_window(self) -> None:
        prefix = f"{GLM_PRODUCT}/2026/202/06/"
        objects: list[tuple[str, int]] = []
        for start, count in ((VALID, 30), (VALID + dt.timedelta(minutes=10), 5)):
            for index in range(count):
                source_time = start + dt.timedelta(seconds=20 * index)
                stamp = source_time.strftime("%Y%j%H%M%S") + "0"
                objects.append((prefix + f"OR_GLM-L2-LCFA_G18_s{stamp}_e.nc", 100))
        client = DiscoveryClient({prefix: objects})

        window = client.latest_complete_glm_window(
            dt.datetime(2026, 7, 21, 6, 59, tzinfo=UTC)
        )

        self.assertEqual(window.start_time, VALID)
        self.assertEqual(len(window.objects), 30)


class HazardDecodeTests(unittest.TestCase):
    def test_smoke_classification_keeps_low_medium_and_high_daylight_clear_pixels(self) -> None:
        smoke = np.ones((1, 6), dtype=np.int8)
        dqf = np.asarray([[0x00, 0x04, 0x08, 0x0C, 0x00, 0x00]], dtype=np.uint16)
        cloud = np.asarray([[0, 0, 0, 0, 1, 0]], dtype=np.int8)
        pqi2 = np.asarray([[0, 0, 0, 0, 0, 0x08]], dtype=np.uint16)

        classes = classify_smoke(smoke, dqf, cloud, pqi2)

        self.assertTrue(np.array_equal(classes, [[2, 1, 3, 255, 255, 255]]))

    def test_adpf_decoder_builds_the_native_geostationary_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / (
                "OR_ABI-L2-ADPF-M6_G18_s20262020640228_e20262020649536_c20262020654088.nc"
            )
            with h5netcdf.File(path, "w") as dataset:
                dataset.dimensions = {"y": 2, "x": 3}
                for name, dtype, values in (
                    ("Smoke", "i1", [[1, 1, 0], [0, 0, 0]]),
                    ("Cloud", "i1", [[0, 0, 0], [0, 0, 0]]),
                    ("DQF", "u2", [[0, 4, 0], [0, 0, 0]]),
                    ("PQI2", "u2", [[0, 0, 0], [0, 0, 0]]),
                ):
                    variable = dataset.create_variable(name, ("y", "x"), dtype=dtype)
                    variable[:] = np.asarray(values, dtype=dtype)
                x = dataset.create_variable("x", ("x",), dtype="f4")
                y = dataset.create_variable("y", ("y",), dtype="f4")
                x[:] = np.asarray((-0.001, 0.0, 0.001), dtype=np.float32)
                y[:] = np.asarray((0.001, -0.001), dtype=np.float32)
                projection = dataset.create_variable("goes_imager_projection", (), dtype="i4")
                projection[...] = 0
                projection.attrs.update(
                    {
                        "perspective_point_height": 35_786_023.0,
                        "longitude_of_projection_origin": -137.0,
                        "semi_major_axis": 6_378_137.0,
                        "semi_minor_axis": 6_356_752.31414,
                        "sweep_angle_axis": np.bytes_("x"),
                    }
                )
                dataset.attrs["time_coverage_start"] = "2026-07-21T06:40:22.8Z"
                dataset.attrs["time_coverage_end"] = "2026-07-21T06:49:53.6Z"

            product = decode_smoke_product(path)

            self.assertEqual(product.classes.shape, (2, 3))
            self.assertEqual(int(product.classes[0, 0]), 2)
            self.assertEqual(int(product.classes[0, 1]), 1)
            self.assertEqual(product.start_time, dt.datetime(2026, 7, 21, 6, 40, 22, 800000, tzinfo=UTC))
            self.assertIn("Geostationary Satellite", product.crs.to_wkt())

    def test_glm_reader_drops_degraded_and_nonfinite_flashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "glm.nc"
            with h5netcdf.File(path, "w") as dataset:
                dataset.dimensions = {"number_of_flashes": 4}
                for name, dtype, values in (
                    ("flash_lat", "f4", [49.0, 50.0, np.nan, 60.0]),
                    ("flash_lon", "f4", [-123.0, -122.0, -121.0, -120.0]),
                    ("flash_quality_flag", "i2", [0, 1, 0, 0]),
                ):
                    variable = dataset.create_variable(name, ("number_of_flashes",), dtype=dtype)
                    variable[:] = np.asarray(values, dtype=dtype)

            flashes = read_glm_flashes(path)

            self.assertEqual(flashes.observed_count, 4)
            self.assertEqual(flashes.good_count, 2)
            self.assertTrue(np.array_equal(flashes.latitudes, [49.0, 60.0]))


class HazardRenderTests(unittest.TestCase):
    def test_smoke_overlay_preserves_confidence_as_opacity_not_intensity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "smoke.png"
            crs = CRS.from_epsg(3857)
            product = SmokeProduct(
                np.asarray([[2, 1], [3, 255]], dtype=np.uint8),
                from_bounds(0, 0, 20_000, 20_000, 2, 2),
                crs,
                VALID,
                VALID + dt.timedelta(minutes=10),
            )
            domain = Domain(
                id="same-grid",
                title="same grid",
                west=0,
                south=0,
                east=1,
                north=1,
                crs="EPSG:3857",
                width=2,
                height=2,
                tier="broad",
                projected_bounds=(0, 0, 20_000, 20_000),
            )

            summary = render_smoke_overlay(product, domain, destination)

            rgba = np.asarray(Image.open(destination).convert("RGBA"))
            self.assertGreater(int(rgba[0, 0, 3]), int(rgba[0, 1, 3]))
            self.assertEqual(int(rgba[1, 0, 3]), int(rgba[0, 1, 3]))
            self.assertEqual(summary["highConfidencePixels"], 1)
            self.assertEqual(summary["mediumConfidencePixels"], 1)
            self.assertEqual(summary["lowConfidencePixels"], 1)

    def test_glm_bins_respect_the_documented_52_degree_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "glm.png"
            flashes = GLMFlashes(
                np.asarray((49.0, 53.0, 60.0)),
                np.asarray((-123.0, -122.0, -120.0)),
                observed_count=3,
                good_count=3,
            )

            summary = render_glm_bins(flashes, projected_domain(), destination)

            rgba = np.asarray(Image.open(destination).convert("RGBA"))
            self.assertEqual(summary["mappedFlashCount"], 1)
            self.assertEqual(summary["markerCount"], 1)
            self.assertEqual(int(np.count_nonzero(rgba[:, :, 3])), 1)

    def test_age_trail_uses_current_and_two_prior_exact_bins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = projected_domain(width=100, height=60)
            for index in range(3):
                valid = VALID - dt.timedelta(minutes=10 * index)
                source = frame_path(root, domain, LAYERS["glm-lightning"], valid)
                source.parent.mkdir(parents=True, exist_ok=True)
                image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
                image.putpixel((20 + index * 20, 30), (255, 255, 255, 255))
                image.save(source, "PNG")
                write_metadata(root, domain, LAYERS["glm-lightning"], valid, source)

            derive_glm_lightning_trails(root, domain)

            trail = frame_path(root, domain, LAYERS["glm-lightning-trail"], VALID)
            metadata = json.loads(
                metadata_path(root, domain, LAYERS["glm-lightning-trail"], VALID).read_text()
            )
            self.assertTrue(trail.is_file())
            self.assertEqual(set(metadata["sourceTimes"]), {"age0", "age10", "age20"})
            rgba = np.asarray(Image.open(trail).convert("RGBA"))
            self.assertGreater(int(np.count_nonzero(rgba[:, :, 3])), 100)


class FakeHazardClient:
    def __init__(self) -> None:
        adp_key = (
            f"{ADP_PRODUCT}/2026/202/06/"
            "OR_ABI-L2-ADPF-M6_G18_s20262020640228_e20262020649536_c20262020654088.nc"
        )
        glm_key = (
            f"{GLM_PRODUCT}/2026/202/06/"
            "OR_GLM-L2-LCFA_G18_s20262020640000_e20262020640200_c20262020640219.nc"
        )
        self.adp = PublicObject("noaa-goes18", adp_key, 10, VALID + dt.timedelta(seconds=22))
        self.window = GLMWindow(VALID, (PublicObject("noaa-goes18", glm_key, 10, VALID),))
        self.downloaded_paths: list[Path] = []

    def latest_adp(self, _now: dt.datetime) -> PublicObject:
        return self.adp

    def latest_complete_glm_window(self, _now: dt.datetime) -> GLMWindow:
        return self.window

    def download(self, item: PublicObject, cache_root: Path, _max_bytes: int) -> Path:
        destination = cache_root / "downloads" / Path(item.key).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"temporary NOAA source")
        self.downloaded_paths.append(destination)
        return destination


class HazardPipelineTests(unittest.TestCase):
    def test_ingest_archives_only_processed_pngs_and_deletes_raw_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = projected_domain("test-domain", width=100, height=60)
            client = FakeHazardClient()
            smoke_product = SmokeProduct(
                np.zeros((2, 2), dtype=np.uint8),
                from_bounds(0, 0, 1, 1, 2, 2),
                CRS.from_epsg(3857),
                VALID,
                VALID + dt.timedelta(minutes=10),
            )

            def smoke_render(_product: SmokeProduct, target: Domain, destination: Path) -> dict[str, object]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (target.width, target.height), (0, 0, 0, 0)).save(destination, "PNG")
                return {"availability": "daylight", "validPixelCount": 1}

            def glm_render(_flashes: GLMFlashes, target: Domain, destination: Path) -> dict[str, object]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                image = Image.new("RGBA", (target.width, target.height), (0, 0, 0, 0))
                image.putpixel((50, 30), (255, 255, 255, 255))
                image.save(destination, "PNG")
                return {"mappedFlashCount": 1, "markerCount": 1}

            with (
                mock.patch.dict("radarsat.pipeline.DOMAINS", {domain.id: domain}),
                mock.patch("radarsat.goes_hazards.decode_smoke_product", return_value=smoke_product),
                mock.patch(
                    "radarsat.goes_hazards.read_glm_flashes",
                    return_value=GLMFlashes(np.asarray([49.0]), np.asarray([-123.0]), 1, 1),
                ),
                mock.patch("radarsat.goes_hazards.render_smoke_overlay", side_effect=smoke_render),
                mock.patch("radarsat.goes_hazards.render_glm_bins", side_effect=glm_render),
            ):
                result = ingest_goes_hazards(root, [domain.id], VALID + dt.timedelta(minutes=20), client=client)

            self.assertEqual(result["status"], "rendered")
            self.assertTrue(frame_path(root, domain, LAYERS["smoke"], VALID).is_file())
            self.assertTrue(frame_path(root, domain, LAYERS["glm-lightning"], VALID).is_file())
            self.assertTrue(
                frame_path(
                    root,
                    domain,
                    LAYERS["glm-lightning-points"],
                    VALID + dt.timedelta(minutes=10),
                ).is_file()
            )
            self.assertFalse(
                frame_path(root, domain, LAYERS["glm-lightning-trail"], VALID).exists()
            )
            self.assertEqual(result["legacyTrailDomains"], [])
            self.assertTrue(all(not path.exists() for path in client.downloaded_paths))
            smoke_metadata = json.loads(metadata_path(root, domain, LAYERS["smoke"], VALID).read_text())
            glm_metadata = json.loads(
                metadata_path(root, domain, LAYERS["glm-lightning"], VALID).read_text()
            )
            self.assertEqual(smoke_metadata["sourceLayer"], "ABI-L2-ADPF")
            self.assertEqual(glm_metadata["sourceLayer"], "GLM-L2-LCFA")
            point_metadata = json.loads(
                metadata_path(
                    root,
                    domain,
                    LAYERS["glm-lightning-points"],
                    VALID + dt.timedelta(minutes=10),
                ).read_text()
            )
            self.assertEqual(point_metadata["pointFrameSchemaVersion"], 1)
            self.assertEqual(point_metadata["pointSchema"], ["x", "y", "ageMinutes", "count"])

    def test_catalog_exposes_processed_hazard_frames_and_product_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = projected_domain("north-america", width=100, height=60)
            for layer_id in ("smoke", "glm-lightning", "glm-lightning-trail"):
                layer = LAYERS[layer_id]
                frame = frame_path(root, domain, layer, VALID)
                frame.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0)).save(
                    frame,
                    "PNG",
                )
                write_metadata(root, domain, layer, VALID, frame)

            with mock.patch.dict("radarsat.catalog.DOMAINS", {domain.id: domain}, clear=True):
                catalog = build_catalog(root)

            catalog_layers = catalog["domains"][domain.id]["layers"]
            self.assertEqual(
                set(catalog_layers),
                {"smoke", "glm-lightning", "glm-lightning-trail"},
            )
            broad = next(
                product
                for product in catalog["products"]
                if product["id"] == "north-america-overlay"
            )
            control_ids = {layer["id"] for layer in broad["layers"]}
            self.assertIn("smoke", control_ids)
            self.assertIn("glm-lightning-trail", control_ids)
            self.assertIn("NOAA GOES-18", catalog["sources"])


if __name__ == "__main__":
    unittest.main()
