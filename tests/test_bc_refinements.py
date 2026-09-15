import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from PIL import Image
from radarsat.video_clock import append_clock_strip, GUARD_HEIGHT
from radarsat.cloud_raster import ensure_enhanced_msc, expose_enhanced_msc, ENHANCED_LAYER
from radarsat import cloud_style
from radarsat.config import VIEWPORTS, VIDEO_COMPOSITE_PRESETS, video_composite_layer_ids
from radarsat.composite_video import _RenderContext, _render_high_frame
from radarsat.video import VIDEO_PROFILES, SelectedFrame, _composite_presets
from dataclasses import replace

class BCRefinementTests(unittest.TestCase):
    def test_clock_changes_do_not_reach_visible_picture_or_guard(self):
        image=Image.new('RGB',(32,24),(54,82,106))
        a,b=(append_clock_strip(image,phase) for phase in (0,1))
        self.assertEqual(a.crop((0,0,32,24+GUARD_HEIGHT)).tobytes(),b.crop((0,0,32,24+GUARD_HEIGHT)).tobytes())
        self.assertEqual(a.getpixel((10,24)),image.getpixel((10,23)))
        self.assertNotEqual(a.getpixel((10,39)),b.getpixel((10,39)))

    def test_regional_zoom_is_incremental_and_centered(self):
        for region,w,h in [('northeast',.502,.4422),('southeast',.405,.3473),('southwest',.4048,.3438)]:
            self.assertAlmostEqual(VIEWPORTS[region]['width'],w/1.15/1.10)
            self.assertAlmostEqual(VIEWPORTS[region]['height'],h/1.15/1.10)

    def test_south_coast_never_renders_satellite_into_prebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Path(tmp);(r/'static/bc').mkdir(parents=True)
            Image.new('RGB',(32,24),(17,28,39)).save(r/'static/bc/base-dark.png')
            spec=replace(next(s for s in VIDEO_PROFILES if s.product_id=='bc-south-coast-overlay'),width=32,height=24)
            t=dt.datetime(2026,9,15,20,tzinfo=dt.timezone.utc)
            frame=SelectedFrame(t,t,{},'eccc-geocolor','missing-source.png','fetched')
            self.assertEqual(len(_composite_presets(spec)), 2)
            for preset in VIDEO_COMPOSITE_PRESETS[spec.product_id]:
                ids=video_composite_layer_ids(spec.product_id,spec.layer_id,preset['id'])
                self.assertNotIn('eccc-geocolor',ids)
                self.assertIn('radar-rain',ids)
                with _RenderContext(r,spec) as context, mock.patch.object(_RenderContext,'satellite',side_effect=AssertionError('satellite must not render')):
                    _render_high_frame(context,spec,ids,[],frame,{},r/'frame.png')
                with Image.open(r/'frame.png') as result:self.assertEqual(result.getpixel((10,10)),(17,28,39))

    def test_custom_derivative_is_reused_and_publication_exposes_only_enhanced(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Path(tmp);src=r/'frames/bc/eccc-geocolor/20260915T2000Z.webp';src.parent.mkdir(parents=True)
            Image.new('RGB',(32,24),(170,180,190)).save(src)
            m={'path':str(src.relative_to(r)),'validTime':'2026-09-15T20:00:00Z','fetchedAt':'first'}
            with mock.patch.object(cloud_style,'render',side_effect=lambda image,time,**kw:image.convert('RGB')) as render:
                out=ensure_enhanced_msc(r,m);ensure_enhanced_msc(r,m);self.assertEqual(render.call_count,1)
                ensure_enhanced_msc(r,{**m,'fetchedAt':'corrected'});self.assertEqual(render.call_count,2)
            c={'domains':{'bc':{'layers':{'eccc-geocolor':{'frames':[m]},ENHANCED_LAYER:{'frames':[{'path':str(out.relative_to(r)), 'satelliteStyle':cloud_style.STYLE_VERSION}]}}}}}
            expose_enhanced_msc(c);expose_enhanced_msc(c);layers=c['domains']['bc']['layers']
            self.assertNotIn(ENHANCED_LAYER,layers)
            self.assertIn('/eccc-geocolor-enhanced/',layers['eccc-geocolor']['frames'][0]['path'])
            empty={'domains':{'bc':{'layers':{'eccc-geocolor':{'frames':[m]}}}}}
            expose_enhanced_msc(empty);self.assertEqual(empty['domains']['bc']['layers']['eccc-geocolor']['frames'],[])
