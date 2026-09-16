import unittest
import numpy as np
from radarsat.config import PRODUCTS, VIDEO_COMPOSITE_PRESETS, VIDEO_EXACT_RANGES, VIDEO_ARCHIVE_PRODUCTS, VIEWPORTS
from radarsat.cloud_geo import geography
from radarsat.video import _display_size

class PrebuiltPolicyTests(unittest.TestCase):
    def test_requested_matrix_has_exactly_fifty_seven_entries(self):
        expected = {'bc-large-overlay': 10, 'bc-small-overlay': 8,
                    'bc-northeast-overlay': 8, 'bc-southeast-overlay': 8,
                    'bc-southwest-overlay': 8, 'bc-south-coast-overlay': 6,
                    'north-pacific-overlay': 3,
                    'north-america-overlay': 6}
        actual = {p: len(presets) * (len(VIDEO_EXACT_RANGES[p]) + (p in VIDEO_ARCHIVE_PRODUCTS))
                  for p, presets in VIDEO_COMPOSITE_PRESETS.items()}
        self.assertEqual(actual, expected)
        self.assertEqual(sum(actual.values()), 57)

    def test_availability_anchor_is_the_default_satellite(self):
        for product in PRODUCTS:
            if product['id'] == 'bc-south-coast-overlay':
                self.assertFalse(any(layer.get('defaultEnabled') for layer in product['layers'] if layer.get('choiceGroup') == 'satellite'))
                continue
            satellite=next(layer for layer in product['layers'] if layer.get('choiceGroup')=='satellite' and layer.get('defaultEnabled'))
            self.assertEqual(product['anchorLayer'],satellite['id'])

    def test_zoom_keeps_centers_aspect_and_display_size(self):
        old = {'southwest': (.3381,.5300,.4048,.3438),
               'southeast': (.5268,.4854,.4050,.3473),
               'northeast': (.3946,.1525,.5020,.4422)}
        for key, (x,y,w,h) in old.items():
            new = VIEWPORTS[key]
            self.assertAlmostEqual(new['left']+new['width']/2, x+w/2)
            self.assertAlmostEqual(new['top']+new['height']/2, y+h/2)
            self.assertAlmostEqual(w/new['width'], 1.15 * 1.10)
            self.assertAlmostEqual(h/new['height'], 1.15 * 1.10)
            self.assertEqual(_display_size('bc', new), _display_size('bc', dict(left=x,top=y,width=w,height=h)))

    def test_solar_grids_match_each_crop_and_cross_dateline_continuously(self):
        grids=[]
        for product in PRODUCTS:
            grid=geography(product['domain'], product['viewport'])
            points=np.array(grid['geo']).reshape(grid['geoHeight'],grid['geoWidth'],3)
            self.assertTrue(np.isfinite(points).all())
            self.assertLess(np.max(np.abs(np.diff(points[:,:,0],axis=1))), 10)
            grids.append(points[grid['geoHeight']//2,grid['geoWidth']//2,:2].tolist())
        self.assertEqual(len({tuple(x) for x in grids}),len(PRODUCTS))
