import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from radarsat.composite_video import prune_composite_sidecar_manifests
from radarsat.r2 import PublicationSafetyError, _discover_objects_stable


class PlaybackReliabilityTests(unittest.TestCase):
    def test_pruning_keeps_catalog_generation_until_catalog_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = 'bc-large-overlay/eccc-geocolor/live/operational-default-v1'
            folder = root / 'composite-manifests' / group / '3'
            folder.mkdir(parents=True)
            old, new = folder / 'old.json', folder / 'new.json'
            for path in [old, new]:
                path.write_text('{}')
                os.utime(path, (0, 0))
            pointer = {'manifestPath': new.relative_to(root).as_posix()}
            index = root / 'composite-index' / group / '3.json'
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps(pointer))
            catalog = {'compositeProfiles': {'bc-large-overlay': {'eccc-geocolor': {'live': [
                {'manifestPath': old.relative_to(root).as_posix()}
            ]}}}}
            (root / 'catalog.json').write_text(json.dumps(catalog))
            now = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
            self.assertEqual(prune_composite_sidecar_manifests(root, now=now), 0)
            self.assertTrue(old.exists())
            catalog['compositeProfiles']['bc-large-overlay']['eccc-geocolor']['live'] = [pointer]
            (root / 'catalog.json').write_text(json.dumps(catalog))
            self.assertEqual(prune_composite_sidecar_manifests(root, now=now), 1)
            self.assertTrue(new.exists())
            self.assertFalse(old.exists())

    def test_publication_retries_optional_rotation_but_not_static_corruption(self):
        for relative in ['composite-manifests/a.json', 'videos/a.mp4', 'frames/a.png']:
            for failure in ['missing or unreadable', 'missing or empty']:
                with self.subTest(relative=relative, failure=failure), mock.patch(
                    'radarsat.r2.discover_objects', side_effect=[
                        PublicationSafetyError(f'Catalog asset is {failure}: {relative}'),
                        ([], b'{}'),
                    ]
                ) as discover, mock.patch('radarsat.r2.time.sleep'):
                    self.assertEqual(_discover_objects_stable(Path('/tmp')), ([], b'{}'))
                    self.assertEqual(discover.call_count, 2)
        with mock.patch('radarsat.r2.discover_objects', side_effect=PublicationSafetyError(
            'Catalog asset is missing or empty: static/base.png'
        )) as discover:
            with self.assertRaises(PublicationSafetyError):
                _discover_objects_stable(Path('/tmp'))
            self.assertEqual(discover.call_count, 1)
