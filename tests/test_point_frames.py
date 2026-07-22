from __future__ import annotations

import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer

from radarsat.catalog import build_catalog
from radarsat.config import LAYERS, Domain
from radarsat.pipeline import frame_path, metadata_path, write_metadata
from radarsat.point_frames import (
    glm_point_rows,
    points_from_lightning_density_png,
    write_point_frame,
)
from radarsat.point_migration import derive_hazard_point_archive


UTC = dt.timezone.utc
VALID = dt.datetime(2026, 7, 22, 5, 20, tzinfo=UTC)


def test_domain(domain_id: str = "north-america") -> Domain:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xmin, ymin = transformer.transform(-130.0, 45.0)
    xmax, ymax = transformer.transform(-110.0, 55.0)
    return Domain(
        id=domain_id,
        title="Point-frame test",
        west=-130.0,
        south=45.0,
        east=-110.0,
        north=55.0,
        crs="EPSG:3857",
        width=200,
        height=120,
        tier="broad" if domain_id != "bc" else "bc",
        projected_bounds=(xmin, ymin, xmax, ymax),
    )


class PointFrameTests(unittest.TestCase):
    def test_glm_rows_are_normalized_counted_and_aged_at_window_end(self) -> None:
        domain = test_domain()
        reference = VALID + dt.timedelta(minutes=10)
        epochs = np.asarray(
            [
                (reference - dt.timedelta(minutes=2)).timestamp(),
                (reference - dt.timedelta(minutes=1)).timestamp(),
                (reference - dt.timedelta(minutes=3)).timestamp(),
            ]
        )

        points, summary = glm_point_rows(
            np.asarray([49.0, 49.0, 53.0]),
            np.asarray([-123.0, -123.0, -122.0]),
            epochs,
            domain,
            reference,
        )

        self.assertEqual(len(points), 1)
        x, y, age, count = points[0]
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x, 1)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y, 1)
        self.assertEqual(age, 1.0)
        self.assertEqual(count, 2)
        self.assertEqual(summary["agePrecisionSeconds"], 20)
        self.assertEqual(summary["mappedFlashCount"], 2)

    def test_point_frame_is_compact_and_self_describing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "frame.json"
            domain = test_domain()
            write_point_frame(
                destination,
                layer="glm-lightning-points",
                domain=domain,
                valid_time=VALID,
                window_start=VALID,
                window_end=VALID + dt.timedelta(minutes=10),
                age_reference_time=VALID + dt.timedelta(minutes=10),
                point_schema=("x", "y", "ageMinutes", "count"),
                points=[[0.25, 0.75, 1.5, 2]],
                age_mode="source-file-midpoint",
                age_precision_seconds=20,
            )

            raw = destination.read_text()
            payload = json.loads(raw)
            self.assertNotIn("\n  ", raw)
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["coordinateSpace"]["origin"], "top-left")
            self.assertEqual(payload["points"], [[0.25, 0.75, 1.5, 2]])

    def test_eccc_density_cells_become_normalized_storm_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            domain = test_domain("bc")
            source = Path(temporary) / "lightning.png"
            image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
            image.putpixel((20, 30), (0, 54, 253, 255))
            image.putpixel((21, 31), (3, 179, 250, 255))
            image.putpixel((150, 80), (253, 51, 0, 255))
            image.save(source, "PNG")

            points = points_from_lightning_density_png(source, domain)

            self.assertEqual(len(points), 2)
            self.assertEqual(sorted(point[3] for point in points), [1, 2])
            self.assertTrue(all(point[2] == 5.0 for point in points))
            self.assertTrue(all(0 <= point[0] <= 1 and 0 <= point[1] <= 1 for point in points))


class PointMigrationTests(unittest.TestCase):
    def test_migration_preserves_pngs_and_labels_estimated_ages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = test_domain("bc")
            glm_layer = LAYERS["glm-lightning"]
            glm_path = frame_path(root, domain, glm_layer, VALID)
            glm_path.parent.mkdir(parents=True, exist_ok=True)
            glm = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
            glm.putpixel((50, 30), (255, 255, 255, 255))
            glm.save(glm_path, "PNG")
            write_metadata(
                root,
                domain,
                glm_layer,
                VALID,
                glm_path,
                extra={"windowEnd": "2026-07-22T05:30:00Z", "renderVersion": 1},
            )

            hotspot_layer = LAYERS["hotspots"]
            hotspot_path = frame_path(root, domain, hotspot_layer, VALID)
            hotspot_path.parent.mkdir(parents=True, exist_ok=True)
            hotspot = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(hotspot)
            draw.rectangle((90, 60, 94, 64), fill=(255, 148, 31, 230))
            hotspot.save(hotspot_path, "PNG")
            write_metadata(
                root,
                domain,
                hotspot_layer,
                VALID,
                hotspot_path,
                extra={"renderVersion": 3},
            )
            lightning_layer = LAYERS["lightning"]
            lightning_path = frame_path(root, domain, lightning_layer, VALID)
            lightning_path.parent.mkdir(parents=True, exist_ok=True)
            lightning = Image.new(
                "RGBA",
                (domain.width, domain.height),
                (0, 0, 0, 0),
            )
            lightning.putpixel((30, 40), (0, 54, 253, 255))
            lightning.save(lightning_path, "PNG")
            write_metadata(root, domain, lightning_layer, VALID, lightning_path)
            before = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (glm_path, hotspot_path, lightning_path)
            }

            with mock.patch.dict("radarsat.point_migration.DOMAINS", {domain.id: domain}, clear=True):
                result = derive_hazard_point_archive(root, [domain.id])

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                before,
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before},
            )
            glm_points_path = frame_path(
                root,
                domain,
                LAYERS["glm-lightning-points"],
                VALID + dt.timedelta(minutes=10),
            )
            hotspot_points_path = frame_path(root, domain, LAYERS["hotspot-points"], VALID)
            lightning_points_path = frame_path(
                root,
                domain,
                LAYERS["lightning-points"],
                VALID,
            )
            glm_payload = json.loads(glm_points_path.read_text())
            hotspot_payload = json.loads(hotspot_points_path.read_text())
            lightning_payload = json.loads(lightning_points_path.read_text())
            self.assertEqual(glm_payload["ageMode"], "window-midpoint-estimate")
            self.assertEqual(glm_payload["points"][0][2], 5.0)
            self.assertEqual(
                hotspot_payload["ageMode"],
                "render-colour-bucket-midpoint-estimate",
            )
            self.assertEqual(hotspot_payload["points"][0][2], 540.0)
            self.assertIsNone(hotspot_payload["points"][0][3])
            self.assertEqual(lightning_payload["ageMode"], "window-midpoint-estimate")
            self.assertEqual(lightning_payload["points"], [[0.150754, 0.336134, 5.0, 1]])

    def test_catalog_describes_point_schema_format_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = test_domain()
            layer = LAYERS["glm-lightning-points"]
            frame = frame_path(root, domain, layer, VALID)
            write_point_frame(
                frame,
                layer=layer.id,
                domain=domain,
                valid_time=VALID,
                window_start=VALID,
                window_end=VALID + dt.timedelta(minutes=10),
                age_reference_time=VALID + dt.timedelta(minutes=10),
                point_schema=layer.point_schema,
                points=[],
                age_mode="source-file-midpoint",
                age_precision_seconds=20,
            )
            write_metadata(root, domain, layer, VALID, frame, extra={"renderVersion": 1})

            with mock.patch.dict("radarsat.catalog.DOMAINS", {domain.id: domain}, clear=True):
                catalog = build_catalog(root)

            entry = catalog["domains"][domain.id]["layers"][layer.id]
            self.assertEqual(entry["role"], "points")
            self.assertEqual(entry["format"], "application/json")
            self.assertEqual(
                entry["pointFrame"]["pointSchema"],
                ["x", "y", "ageMinutes", "count"],
            )
            self.assertEqual(entry["pointFrame"]["retention"]["allFramesHours"], 24)
            self.assertEqual(entry["pointFrame"]["retention"]["archiveCadenceMinutes"], 60)


if __name__ == "__main__":
    unittest.main()
