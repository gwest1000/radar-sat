from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from radarsat.config import LAYERS, Domain
from radarsat.pipeline import frame_path, metadata_path, write_metadata
from radarsat.raw_satellite import PublicObject, RenderedSatellite
from radarsat.westwx_satellite import (
    WESTWX_RENDER_VERSION,
    DiscoveryResult,
    PlannedBackfill,
    ScanResult,
    discover_goes18_scans,
    execute_backfill,
    plan_backfill,
    render_westwx_scan,
)


UTC = dt.timezone.utc


def goes_key(value: dt.datetime, revision: str = "0") -> str:
    prefix = f"ABI-L2-MCMIPF/{value:%Y}/{value:%j}/{value:%H}/"
    stamp = value.strftime("%Y%j%H%M%S")
    return (
        prefix
        + f"OR_ABI-L2-MCMIPF-M6_G18_s{stamp}0_e{stamp}0_c{stamp}{revision}.nc"
    )


def scan(value: dt.datetime, size: int = 100) -> PublicObject:
    return PublicObject("noaa-goes18", goes_key(value), size, value)


def tiny_domain() -> Domain:
    return Domain(
        id="north-america",
        title="Tiny WestWX test",
        west=-130,
        south=40,
        east=-110,
        north=60,
        crs="EPSG:4326",
        width=8,
        height=4,
        tier="broad",
    )


def tiny_bc_domain() -> Domain:
    return Domain(
        id="bc",
        title="Tiny BC rapid test",
        west=-130,
        south=48,
        east=-114,
        north=60,
        crs="EPSG:4326",
        width=6,
        height=5,
        tier="bc",
    )


class DiscoveryClient:
    def __init__(
        self,
        objects: dict[str, list[tuple[str, int]]],
        failing_prefixes: set[str] | None = None,
    ) -> None:
        self.objects = objects
        self.failing_prefixes = failing_prefixes or set()

    def list_prefix(self, _bucket: str, prefix: str) -> list[tuple[str, int]]:
        if prefix in self.failing_prefixes:
            raise RuntimeError("simulated listing failure")
        return self.objects.get(prefix, [])


class DownloadClient:
    def __init__(self) -> None:
        self.downloaded: list[Path] = []

    def download(self, item: PublicObject, cache_root: Path, _maximum: int) -> Path:
        destination = cache_root / "downloads" / Path(item.key).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * item.size)
        self.downloaded.append(destination)
        return destination


class WestWxDiscoveryTests(unittest.TestCase):
    def test_discovery_preserves_scan_seconds_and_isolates_an_hour_failure(self) -> None:
        start = dt.datetime(2026, 7, 21, 5, 50, tzinfo=UTC)
        end = dt.datetime(2026, 7, 21, 6, 30, tzinfo=UTC)
        failed = "ABI-L2-MCMIPF/2026/202/05/"
        prefix = "ABI-L2-MCMIPF/2026/202/06/"
        first = dt.datetime(2026, 7, 21, 6, 0, 21, tzinfo=UTC)
        second = dt.datetime(2026, 7, 21, 6, 10, 22, tzinfo=UTC)
        off_cadence = dt.datetime(2026, 7, 21, 6, 15, 22, tzinfo=UTC)
        client = DiscoveryClient(
            {
                prefix: [
                    (goes_key(first), 101),
                    (goes_key(second), 102),
                    (goes_key(off_cadence), 103),
                ]
            },
            {failed},
        )

        result = discover_goes18_scans(client, start, end)

        self.assertEqual(
            [item.valid_time for item in result.scans],
            [second, first],
        )
        self.assertEqual(result.scans[0].valid_time.second, 22)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn(failed, result.warnings[0])

    def test_plan_is_a_bounded_newest_first_prefix_and_skips_ready_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = tiny_domain()
            values = [
                dt.datetime(2026, 7, 21, 7, minute, 21, tzinfo=UTC)
                for minute in (20, 10, 0)
            ]
            scans = tuple(
                scan(value, size)
                for value, size in zip(values, (100, 150, 50), strict=True)
            )
            ready = scans[0]
            for layer_id in ("westwx-visible", "westwx-visir", "westwx-ir"):
                layer = LAYERS[layer_id]
                image = frame_path(root, domain, layer, ready.valid_time)
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b"ready")
                write_metadata(
                    root,
                    domain,
                    layer,
                    ready.valid_time,
                    image,
                    extra={
                        "renderVersion": WESTWX_RENDER_VERSION,
                        "sourceFile": Path(ready.key).name,
                    },
                )

            plan = plan_backfill(
                root,
                DiscoveryResult(scans),
                max_frames=2,
                max_download_bytes=175,
                domain=domain,
            )

            self.assertEqual(plan.skipped_ready, 1)
            self.assertEqual(plan.scans, (scans[1],))
            self.assertEqual(plan.estimated_bytes, 150)
            # The older 50-byte scan would fit, but is intentionally not used
            # after a newer scan reaches the contiguous byte boundary.
            self.assertEqual(plan.excluded_by_byte_limit, 1)


class WestWxRenderTests(unittest.TestCase):
    def test_render_writes_both_layers_with_actual_timestamp_and_cleans_work_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            cache = base / "cache"
            domain = tiny_domain()
            value = dt.datetime(2026, 7, 21, 20, 10, 21, tzinfo=UTC)
            source = scan(value, 32)
            client = DownloadClient()

            def fake_render(
                source_paths: object,
                reader: str,
                infrared_dataset: str,
                target: Domain,
                work_root: Path,
                stem: str,
            ) -> RenderedSatellite:
                paths = list(source_paths)  # type: ignore[arg-type]
                self.assertTrue(paths[0].is_file())
                self.assertEqual(reader, "abi_l2_nc")
                self.assertEqual(infrared_dataset, "C13")
                render_root = work_root / "renders"
                render_root.mkdir(parents=True, exist_ok=True)
                visible = render_root / f"{stem}-{target.id}-visible.webp"
                infrared = render_root / f"{stem}-{target.id}-ir.webp"
                infrared_gray = render_root / f"{stem}-{target.id}-ir-gray.webp"
                mask = render_root / f"{stem}-{target.id}-mask.png"
                Image.new("RGB", (target.width, target.height), (30, 150, 80)).save(
                    visible, "WEBP", lossless=True
                )
                Image.new("RGB", (target.width, target.height), (220, 30, 120)).save(
                    infrared, "WEBP", lossless=True
                )
                neutral = Image.new("RGBA", (target.width, target.height), (170, 170, 170, 255))
                neutral.save(infrared_gray, "WEBP", lossless=True, exact=True)
                Image.new("L", (target.width, target.height), 255).save(mask, "PNG")
                return RenderedSatellite(visible, infrared, infrared_gray, mask)

            result = render_westwx_scan(
                root,
                source,
                client,  # type: ignore[arg-type]
                cache,
                domain=domain,
                render_source=fake_render,
            )

            self.assertEqual(result.status, "rendered")
            for layer_id in ("westwx-visible", "westwx-visir", "westwx-ir"):
                layer = LAYERS[layer_id]
                image = frame_path(root, domain, layer, value)
                payload = json.loads(metadata_path(root, domain, layer, value).read_text())
                self.assertTrue(image.is_file())
                self.assertEqual(payload["validTime"], "2026-07-21T20:10:21Z")
                self.assertEqual(payload["scanStart"], "2026-07-21T20:10:21Z")
                self.assertEqual(payload["sourceFile"], Path(source.key).name)
                self.assertEqual(payload["nominalCadenceMinutes"], 10)
                self.assertTrue(payload["westwxOnly"])
            self.assertTrue(all(not path.exists() for path in client.downloaded))
            self.assertFalse((cache / "renders").exists())
            self.assertFalse((cache / "westwx-staging").exists())

    def test_production_render_reuses_one_download_for_north_america_and_bc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "output"
            cache = base / "cache"
            north_america = tiny_domain()
            bc = tiny_bc_domain()
            value = dt.datetime(2026, 7, 21, 20, 20, 21, tzinfo=UTC)
            source = scan(value, 32)
            client = DownloadClient()

            def fake_render(
                source_paths: object,
                _reader: str,
                _infrared_dataset: str,
                target: Domain,
                work_root: Path,
                stem: str,
            ) -> RenderedSatellite:
                self.assertTrue(list(source_paths)[0].is_file())  # type: ignore[arg-type]
                render_root = work_root / "renders"
                render_root.mkdir(parents=True, exist_ok=True)
                visible = render_root / f"{stem}-visible.webp"
                infrared = render_root / f"{stem}-ir.webp"
                infrared_gray = render_root / f"{stem}-ir-gray.webp"
                mask = render_root / f"{stem}-mask.png"
                Image.new("RGB", (target.width, target.height), (30, 150, 80)).save(
                    visible, "WEBP", lossless=True
                )
                Image.new("RGB", (target.width, target.height), (220, 30, 120)).save(
                    infrared, "WEBP", lossless=True
                )
                Image.new("RGBA", (target.width, target.height), (170, 170, 170, 255)).save(
                    infrared_gray, "WEBP", lossless=True, exact=True
                )
                Image.new("L", (target.width, target.height), 255).save(mask, "PNG")
                return RenderedSatellite(visible, infrared, infrared_gray, mask)

            with mock.patch.dict(
                "radarsat.westwx_satellite.DOMAINS",
                {"north-america": north_america, "bc": bc},
                clear=True,
            ):
                result = render_westwx_scan(
                    root,
                    source,
                    client,  # type: ignore[arg-type]
                    cache,
                    render_source=fake_render,
                )

            self.assertEqual(result.status, "rendered")
            self.assertEqual(len(client.downloaded), 1)
            for domain, layer_ids in (
                (north_america, ("westwx-visible", "westwx-visir", "westwx-ir")),
                (bc, ("raw-visible", "raw-visir", "raw-ir")),
            ):
                for layer_id in layer_ids:
                    layer = LAYERS[layer_id]
                    self.assertTrue(frame_path(root, domain, layer, value).is_file())
                    payload = json.loads(
                        metadata_path(root, domain, layer, value).read_text()
                    )
                    self.assertEqual(payload["nominalCadenceMinutes"], 10)
                    self.assertEqual(payload["rapidDomain"], domain.id)

    def test_failed_scan_is_isolated_and_later_scan_still_runs(self) -> None:
        values = [
            dt.datetime(2026, 7, 21, 8, minute, 21, tzinfo=UTC)
            for minute in (20, 10, 0)
        ]
        scans = tuple(scan(value) for value in values)
        plan = PlannedBackfill(scans, 300, 0, 0, 0)
        visited: list[dt.datetime] = []

        def processor(item: PublicObject) -> ScanResult:
            visited.append(item.valid_time)
            if item.valid_time == values[1]:
                raise ValueError("broken middle scan")
            return ScanResult(item.valid_time, "rendered", item.size)

        result = execute_backfill(Path("unused"), plan, processor, rebuild_catalog=False)

        self.assertEqual(visited, values)
        self.assertEqual(
            [item.status for item in result.scans],
            ["rendered", "failed", "rendered"],
        )
        self.assertEqual(result.status, "warning")
        self.assertIn("broken middle scan", result.scans[1].error or "")


if __name__ == "__main__":
    unittest.main()
