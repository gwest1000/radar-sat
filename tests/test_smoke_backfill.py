from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.transform import from_bounds

from radarsat.config import LAYERS, Domain
from radarsat.goes_hazards import (
    ADP_ARCHIVE_HOURS,
    ADP_MAX_DISCOVERY_BYTES,
    ADP_MAX_SCANS,
    ADP_PRODUCT,
    GoesHazardClient,
    SmokeProduct,
)
from radarsat.pipeline import (
    SMOKE_RENDER_VERSION,
    frame_path,
    ingest_goes_smoke_archive,
    metadata_path,
    write_metadata,
)
from radarsat.raw_satellite import PublicObject


UTC = dt.timezone.utc


def adp_key(value: dt.datetime, *, revision: str = "0") -> str:
    prefix = f"{ADP_PRODUCT}/{value:%Y}/{value:%j}/{value:%H}/"
    stamp = value.strftime("%Y%j%H%M%S") + revision
    return prefix + f"OR_ABI-L2-ADPF-M6_G18_s{stamp}_e{stamp}_c{stamp}.nc"


class DiscoveryClient(GoesHazardClient):
    def __init__(self, objects: dict[str, list[tuple[str, int]]]) -> None:
        self.objects = objects
        self.prefixes: list[str] = []

    def list_prefix(self, _bucket: str, prefix: str) -> list[tuple[str, int]]:
        self.prefixes.append(prefix)
        return self.objects.get(prefix, [])

    def close(self) -> None:
        return None


def test_domain(domain_id: str = "north-america") -> Domain:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xmin, ymin = transformer.transform(-130.0, 45.0)
    xmax, ymax = transformer.transform(-110.0, 55.0)
    return Domain(
        id=domain_id,
        title="Smoke test",
        west=-130.0,
        south=45.0,
        east=-110.0,
        north=55.0,
        crs="EPSG:3857",
        width=80,
        height=50,
        tier="broad",
        projected_bounds=(xmin, ymin, xmax, ymax),
    )


class SmokeDiscoveryTests(unittest.TestCase):
    def test_history_crosses_utc_day_boundary_and_returns_oldest_first(self) -> None:
        now = dt.datetime(2026, 7, 22, 0, 5, tzinfo=UTC)
        previous = dt.datetime(2026, 7, 21, 23, 10, tzinfo=UTC)
        latest = dt.datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
        objects: dict[str, list[tuple[str, int]]] = {}
        for value in (previous, latest):
            prefix = f"{ADP_PRODUCT}/{value:%Y}/{value:%j}/{value:%H}/"
            objects.setdefault(prefix, []).append((adp_key(value), 100))
        client = DiscoveryClient(objects)

        scans = client.adp_scans(
            now,
            lookback_hours=24,
            max_scans=10,
            max_total_bytes=10_000,
        )

        self.assertEqual([item.valid_time for item in scans], [previous, latest])
        self.assertEqual(len(client.prefixes), 25)
        self.assertIn(f"{ADP_PRODUCT}/2026/202/00/", client.prefixes)
        self.assertIn(f"{ADP_PRODUCT}/2026/202/23/", client.prefixes)
        self.assertIn(f"{ADP_PRODUCT}/2026/203/00/", client.prefixes)

    def test_history_deduplicates_ten_minute_buckets_and_applies_both_caps(self) -> None:
        now = dt.datetime(2026, 7, 22, 1, 0, tzinfo=UTC)
        values = [now - dt.timedelta(minutes=10 * index) for index in range(6)]
        objects: dict[str, list[tuple[str, int]]] = {}
        for value in values:
            prefix = f"{ADP_PRODUCT}/{value:%Y}/{value:%j}/{value:%H}/"
            objects.setdefault(prefix, []).append((adp_key(value), 100))
        # The later scan-start in 00:50's bucket must win before bounds apply.
        duplicate = now - dt.timedelta(minutes=1)
        prefix = f"{ADP_PRODUCT}/{duplicate:%Y}/{duplicate:%j}/{duplicate:%H}/"
        objects.setdefault(prefix, []).append((adp_key(duplicate, revision="1"), 100))
        client = DiscoveryClient(objects)

        byte_limited = client.adp_scans(
            now,
            lookback_hours=2,
            max_scans=3,
            max_total_bytes=250,
        )
        scan_limited = client.adp_scans(
            now,
            lookback_hours=2,
            max_scans=3,
            max_total_bytes=10_000,
        )

        self.assertEqual(len(byte_limited), 2)
        self.assertEqual([item.valid_time for item in byte_limited], [duplicate, now])
        self.assertEqual(len(scan_limited), 3)
        self.assertEqual(scan_limited[-1].valid_time, now)
        self.assertLessEqual(sum(item.size for item in byte_limited), 250)

    def test_history_rejects_configuration_above_audited_hard_bounds(self) -> None:
        client = DiscoveryClient({})
        with self.assertRaises(ValueError):
            client.adp_scans(lookback_hours=ADP_ARCHIVE_HOURS + 0.1)
        with self.assertRaises(ValueError):
            client.adp_scans(max_scans=ADP_MAX_SCANS + 1)
        with self.assertRaises(ValueError):
            client.adp_scans(max_total_bytes=ADP_MAX_DISCOVERY_BYTES + 1)


class ArchiveClient:
    def __init__(self, scans: list[PublicObject]) -> None:
        self.scans = scans
        self.download_order: list[dt.datetime] = []
        self.downloaded_paths: list[Path] = []
        self.discovery_args: dict[str, object] = {}

    def adp_scans(self, _now: dt.datetime, **kwargs: object) -> list[PublicObject]:
        self.discovery_args = kwargs
        return self.scans

    def download(self, item: PublicObject, cache_root: Path, _max_bytes: int) -> Path:
        destination = cache_root / "downloads" / Path(item.key).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"temporary ADPF")
        self.download_order.append(item.valid_time)
        self.downloaded_paths.append(destination)
        return destination


def smoke_product(value: dt.datetime) -> SmokeProduct:
    return SmokeProduct(
        np.full((2, 2), 255, dtype=np.uint8),
        from_bounds(0, 0, 1, 1, 2, 2),
        CRS.from_epsg(3857),
        value,
        value + dt.timedelta(minutes=10),
    )


class SmokeArchivePipelineTests(unittest.TestCase):
    def make_objects(self, values: list[dt.datetime], size: int = 100) -> list[PublicObject]:
        return [PublicObject("noaa-goes18", adp_key(value), size, value) for value in values]

    def test_archive_rejects_a_nonpositive_per_object_cap(self) -> None:
        domain = test_domain()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict("radarsat.pipeline.DOMAINS", {domain.id: domain}),
            self.assertRaises(ValueError),
        ):
            ingest_goes_smoke_archive(
                Path(temporary),
                [domain.id],
                client=ArchiveClient([]),
                max_object_bytes=0,
            )

    def test_catchup_skips_ready_frame_renders_oldest_first_and_deletes_raw_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = test_domain()
            values = [dt.datetime(2026, 7, 21, 6, minute, tzinfo=UTC) for minute in (20, 30, 40)]
            client = ArchiveClient(self.make_objects(list(reversed(values))))
            ready = frame_path(root, domain, LAYERS["smoke"], values[0])
            ready.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0)).save(ready, "PNG")
            write_metadata(
                root,
                domain,
                LAYERS["smoke"],
                values[0],
                ready,
                extra={"renderVersion": SMOKE_RENDER_VERSION, "availability": "unavailable"},
            )
            decoded: dict[str, dt.datetime] = {
                Path(item.key).name: item.valid_time for item in client.scans
            }
            render_order: list[dt.datetime] = []

            def decode(path: Path) -> SmokeProduct:
                return smoke_product(decoded[path.name])

            def render(product: SmokeProduct, target: Domain, destination: Path) -> dict[str, object]:
                self.assertTrue(all(not path.exists() for path in client.downloaded_paths))
                render_order.append(product.start_time)
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (target.width, target.height), (0, 0, 0, 0)).save(
                    destination,
                    "PNG",
                )
                return {"availability": "unavailable", "validPixelCount": 0}

            with (
                mock.patch.dict("radarsat.pipeline.DOMAINS", {domain.id: domain}),
                mock.patch("radarsat.goes_hazards.decode_smoke_product", side_effect=decode),
                mock.patch("radarsat.goes_hazards.render_smoke_overlay", side_effect=render),
            ):
                result = ingest_goes_smoke_archive(
                    root,
                    [domain.id],
                    values[-1] + dt.timedelta(minutes=10),
                    client=client,
                )

            self.assertEqual(client.download_order, values[1:])
            self.assertEqual(render_order, values[1:])
            self.assertEqual(result["framesSkipped"], 1)
            self.assertEqual(result["framesRendered"], 2)
            self.assertEqual(result["status"], "rendered")
            self.assertEqual(result["lookbackHours"], 24.0)
            self.assertEqual(client.discovery_args["max_scans"], 150)
            self.assertEqual(client.discovery_args["max_total_bytes"], 600_000_000)
            for value in values:
                payload = json.loads(
                    metadata_path(root, domain, LAYERS["smoke"], value).read_text()
                )
                self.assertEqual(payload["availability"], "unavailable")

    def test_failed_middle_scan_keeps_partial_success_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = test_domain()
            values = [dt.datetime(2026, 7, 21, 7, minute, tzinfo=UTC) for minute in (0, 10, 20)]
            client = ArchiveClient(self.make_objects(values))
            decoded = {Path(item.key).name: item.valid_time for item in client.scans}

            def decode(path: Path) -> SmokeProduct:
                value = decoded[path.name]
                if value == values[1]:
                    raise ValueError("broken middle scan")
                return smoke_product(value)

            def render(_product: SmokeProduct, target: Domain, destination: Path) -> dict[str, object]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (target.width, target.height), (0, 0, 0, 0)).save(
                    destination,
                    "PNG",
                )
                return {"availability": "unavailable", "validPixelCount": 0}

            with (
                mock.patch.dict("radarsat.pipeline.DOMAINS", {domain.id: domain}),
                mock.patch("radarsat.goes_hazards.decode_smoke_product", side_effect=decode),
                mock.patch("radarsat.goes_hazards.render_smoke_overlay", side_effect=render),
            ):
                result = ingest_goes_smoke_archive(
                    root,
                    [domain.id],
                    values[-1] + dt.timedelta(minutes=10),
                    client=client,
                )

            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["framesRendered"], 2)
            self.assertTrue(result["warnings"])
            self.assertTrue(metadata_path(root, domain, LAYERS["smoke"], values[0]).is_file())
            self.assertFalse(metadata_path(root, domain, LAYERS["smoke"], values[1]).exists())
            self.assertTrue(metadata_path(root, domain, LAYERS["smoke"], values[2]).is_file())
            self.assertTrue(all(not path.exists() for path in client.downloaded_paths))

    def test_runtime_aggregate_cap_preserves_completed_older_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = test_domain()
            values = [dt.datetime(2026, 7, 21, 8, minute, tzinfo=UTC) for minute in (0, 10)]
            client = ArchiveClient(self.make_objects(values, size=70))
            decoded = {Path(item.key).name: item.valid_time for item in client.scans}

            def render(_product: SmokeProduct, target: Domain, destination: Path) -> dict[str, object]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (target.width, target.height), (0, 0, 0, 0)).save(
                    destination,
                    "PNG",
                )
                return {"availability": "daylight", "validPixelCount": 1}

            with (
                mock.patch.dict("radarsat.pipeline.DOMAINS", {domain.id: domain}),
                mock.patch(
                    "radarsat.goes_hazards.decode_smoke_product",
                    side_effect=lambda path: smoke_product(decoded[path.name]),
                ),
                mock.patch("radarsat.goes_hazards.render_smoke_overlay", side_effect=render),
            ):
                result = ingest_goes_smoke_archive(
                    root,
                    [domain.id],
                    values[-1] + dt.timedelta(minutes=10),
                    client=client,
                    max_download_bytes=100,
                )

            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["framesRendered"], 1)
            self.assertEqual(result["downloadBytes"], 70)
            self.assertTrue(metadata_path(root, domain, LAYERS["smoke"], values[0]).is_file())
            self.assertFalse(metadata_path(root, domain, LAYERS["smoke"], values[1]).exists())


if __name__ == "__main__":
    unittest.main()
