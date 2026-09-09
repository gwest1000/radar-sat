import datetime as dt
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from PIL import Image
from radarsat import cloud_style, cloud_policy
from radarsat.composite_video import _atomic_png, _RenderContext, _composite_video_crf
from radarsat.video import VIDEO_PROFILES, SelectedFrame


class CloudStyleTests(unittest.TestCase):
    def setUp(self):
        self.spec = next(s for s in VIDEO_PROFILES if s.product_id == 'bc-large-overlay'
                         and s.layer_id == 'eccc-geocolor' and s.track == 'live')
        self.time = dt.datetime(2026, 9, 9, 22, tzinfo=dt.timezone.utc)

    def test_operational_scope(self):
        for track in ('live', 'day', 'archive'):
            spec = replace(self.spec, track_name=track)
            for hours in (3, 6, 12, 24, 168):
                self.assertTrue(cloud_policy.enabled(spec, hours))
                self.assertEqual(_composite_video_crf(spec, hours), 22)
        for spec in (replace(self.spec, layer_id='raw-visir'),
                     replace(self.spec, product_id='bc-northeast-overlay')):
            self.assertFalse(cloud_policy.enabled(spec))
            self.assertEqual(_composite_video_crf(spec, 6), 20)

    def test_cache_reuses_and_repairs_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ab-test.png'
            source = Image.new('RGB', (16, 12), (170, 180, 190))
            with mock.patch.object(cloud_style, 'render', return_value=source.copy()) as render:
                cloud_style.render_cached(source, self.time, path, _atomic_png).close()
                cloud_style.render_cached(source, self.time, path, _atomic_png).close()
                self.assertEqual(render.call_count, 1)
            path.write_bytes(b'corrupt')
            with mock.patch.object(cloud_style, 'render', side_effect=RuntimeError('failed')):
                with self.assertRaisesRegex(RuntimeError, 'failed'):
                    cloud_style.render_cached(source, self.time, path, _atomic_png)
            self.assertEqual(path.read_bytes(), b'corrupt')
            with mock.patch.object(cloud_style, 'render', return_value=source.copy()):
                cloud_style.render_cached(source, self.time, path, _atomic_png).close()
            self.assertIsNotNone(cloud_style.read_cache(path, source.size))

    def test_cache_identity_includes_corrections_style_geometry_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'static/bc').mkdir(parents=True)
            (root / 'static/bc/base-dark.png').write_bytes(b'base')
            (root / 'source.webp').write_bytes(b'source')
            frame = SelectedFrame(self.time, self.time, {}, 'eccc-geocolor', 'source.webp', 'fetch1')
            def key(spec=self.spec, f=frame):
                return cloud_style.cache_path(root, root, spec, f)
            original = key()
            for changed in [replace(frame, source_fetched_at='fetch2'),
                            replace(frame, source_valid_time=self.time+dt.timedelta(seconds=20))]:
                self.assertNotEqual(original, key(f=changed))
            self.assertNotEqual(original, key(spec=replace(self.spec, width=960)))
            with mock.patch.object(cloud_style, 'STYLE_VERSION', 'next'):
                self.assertNotEqual(original, key())
            (root / 'source.webp').write_bytes(b'corrected source')
            self.assertNotEqual(original, key())

    def test_shared_grade_cache_across_tracks_and_renditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'static/bc').mkdir(parents=True)
            Image.new('RGBA', (64, 48), (30, 40, 50, 255)).save(root / 'static/bc/base-dark.png')
            Image.new('RGBA', (64, 48), (160, 170, 180, 255)).save(root / 'source.png')
            frame = SelectedFrame(self.time, self.time, {}, 'eccc-geocolor', 'source.png', 'fetch1')
            with mock.patch.object(cloud_style, 'render', side_effect=lambda image, time: image.copy()) as renderer:
                for track in ('live', 'day', 'archive'):
                    for width, height in ((1920, self.spec.height), (640, 442)):
                        spec = replace(self.spec, track_name=track, width=width, height=height)
                        image = cloud_policy.frame_image(root, root, spec, frame)
                        self.assertEqual(image.size, (width, height))
                        image.close()
                self.assertEqual(renderer.call_count, 1)
            self.assertEqual(len(list((root / 'composite-frame-cache/cloud-light').glob('*.png'))), 1)

    def test_base_and_archive_manifest_use_grade_and_crf22(self):
        import json
        import shutil
        from radarsat.video import build_profile
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'source', root / 'output'
            (source / 'static/bc').mkdir(parents=True)
            Image.new('RGBA', (64, 48), (30, 40, 50, 255)).save(source / 'static/bc/base-dark.png')
            frames = []
            for index in range(3):
                name = f'source{index}.png'
                Image.new('RGBA', (64, 48), (160, 170, 180, 255)).save(source / name)
                timestamp = (self.time + dt.timedelta(hours=index)).isoformat()
                frames.append({'validTime': timestamp, 'path': name, 'fetchedAt': timestamp})
            catalog = {'domains': {'bc': {'staticLayers': {}, 'layers': {
                'eccc-geocolor': {'frames': frames, 'maxAgeMinutes': 90}}}}}
            for track in ('live', 'day', 'archive'):
                spec = replace(self.spec, track_name=track, width=64, height=48, cadence_minutes=60)
                with mock.patch.object(cloud_policy, 'frame_image', side_effect=lambda sr, out, sp, f: Image.new('RGB', (sp.width, sp.height), (180, 190, 200))) as render:
                    result = build_profile(source, output, catalog, spec, ffmpeg=shutil.which('ffmpeg'), hours=168,
                                           now=self.time + dt.timedelta(hours=2))
                    self.assertGreater(render.call_count, 0)
                manifest = json.loads((output / result['manifestPath']).read_text())
                self.assertEqual(manifest['satelliteStyle'], cloud_style.STYLE_VERSION)
                self.assertEqual(manifest['videoEncoding']['crf'], 22)
                self.assertEqual(manifest['mediaViewport'], dict(spec.viewport))
                if track == 'archive':
                    self.assertTrue(manifest['composites'])
                    self.assertTrue(all(c['ranges'][0]['hours'] == 168 for c in manifest['composites']))
                else:
                    self.assertNotIn('composites', manifest)  # exact sidecars own these ranges

    def test_night_preserves_supplied_pixels(self):
        # Full grid is night at this time; no daytime grade leaks into IR.
        image = Image.effect_noise((64, 48), 45).convert('RGB')
        output = cloud_style.render(image, dt.datetime(2026, 9, 9, 9, 40, tzinfo=dt.timezone.utc))
        self.assertEqual(output.tobytes(), image.tobytes())

if __name__ == '__main__':
    unittest.main()
