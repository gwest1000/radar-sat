import datetime as dt
import unittest
from radarsat.config import PRODUCTS, video_composite_layer_ids
from radarsat.retention import keep_layer_frame
from radarsat.r2 import expired_remote_keys
from radarsat.video import VIDEO_PROFILES, _selected_satellite_frames, _proxy_selections

UTC = dt.timezone.utc

class EasternPacificNorthAmericaTests(unittest.TestCase):
    def test_sparse_archive_uses_real_synchronized_slots(self):
        base = dt.datetime(2026, 9, 10, tzinfo=UTC)
        def frames(layer, seconds=0):
            return [dict(validTime=(base+dt.timedelta(hours=h,seconds=seconds)).isoformat(),
                         path=f'frames/north-america/{layer}/{h}.png') for h in (0,3,6)]
        satellite=frames('raw-visir',21)
        lightning=frames('glm-lightning-hour',21)
        catalog={'domains':{'north-america':{'layers':{
            'raw-visir':{'frames':satellite,'maxAgeMinutes':90},
            'radar-rain':{'frames':frames('radar-rain'),'maxAgeMinutes':20},
            'glm-lightning-hour':{'frames':lightning,'maxAgeMinutes':80},
        }}}}
        spec=next(s for s in VIDEO_PROFILES if s.product_id=='north-america-overlay' and s.layer_id=='raw-visir' and s.track=='archive')
        selected=_selected_satellite_frames(catalog,spec,6)
        self.assertEqual([f.valid_time for f in selected],[base+dt.timedelta(hours=h) for h in (0,3,6)])
        self.assertEqual(len({f.source_path for f in selected}),3)
        for frame, layers in zip(selected,_proxy_selections(catalog,spec,selected)):
            by_id={x.recipe_id:x for x in layers}
            for layer in ('radar-rain','glm-lightning-trail'):
                self.assertLessEqual(abs((by_id[layer].source_valid_time-frame.valid_time).total_seconds()),21)

    def test_hourly_retention_matches_local_and_remote_for_north_america(self):
        now=dt.datetime(2026,9,16,12,tzinfo=UTC)
        valid=dt.datetime(2026,9,14,10,tzinfo=UTC)
        for layer in ('raw-visir','radar-rain','radar-coverage','glm-lightning-hour','smoke','hotspots'):
            self.assertTrue(keep_layer_frame(valid,now,'broad',layer,'north-america'))
            self.assertFalse(keep_layer_frame(valid,now,'broad',layer,'north-pacific'))
            key=f'frames/north-america/{layer}/2026/09/14/20260914T1000Z.png'
            self.assertEqual(expired_remote_keys({key:100},now),[])
        self.assertFalse(keep_layer_frame(valid+dt.timedelta(minutes=10),now,'broad','raw-visir','north-america'))
        self.assertFalse(keep_layer_frame(now-dt.timedelta(days=8),now,'broad','raw-visir','north-america'))
        self.assertFalse(keep_layer_frame(valid,now,'broad','ecmwf-mslp','north-america'))

    def test_full_recipes_and_domain_limits(self):
        products={p['id']:p for p in PRODUCTS}
        self.assertNotIn('pacific-wna-overlay',products)
        self.assertEqual(products['north-america-overlay']['shortTitle'],'E Pac/NA')
        for suffix in ('southwest','southeast','northeast','south-coast'):
            self.assertEqual(products[f'bc-{suffix}-overlay']['maxHours'],24)
        for product, satellite in [('bc-small-overlay','eccc-geocolor'),('bc-large-overlay','eccc-geocolor'),('north-america-overlay','raw-visir')]:
            full=set(video_composite_layer_ids(product,satellite,'weather-full-v1'))
            fires=set(video_composite_layer_ids(product,satellite,'operational-default-v1'))
            self.assertEqual(fires-full,{'smoke','hotspots'})
            self.assertTrue({satellite,'radar-rain','model-mslp','model-hgt500'} <= full)
            self.assertFalse(full-fires)
