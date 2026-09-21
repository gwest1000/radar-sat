import copy
import datetime as dt
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from radarsat.publication_content import project_frames, expired_compact_keys
from radarsat.publication_ledger import put_if_changed, put_pointer
from radarsat.r2 import LocalObject, PublishState, R2Config
import test_publisher_reconciliation as fixtures
MemoryR2 = fixtures.MemoryR2
from radarsat.live_edge import publish_live_edge

class ContentTests(unittest.TestCase):
    def test_alias_equal_pixels_preserve_observation_clocks_and_recover_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); frames=[]
            for minute in ('00','06'):
                key=f'frames/bc/radar-rain/2026/09/09/20260909T16{minute}Z.png'
                (root/key).parent.mkdir(parents=True,exist_ok=True); (root/key).write_bytes(b'pixels')
                record={'path':key,'validTime':f'2026-09-09T16:{minute}:00Z','extraRecoveryField':42}
                meta=root/Path('metadata',*Path(key).parts[1:]).with_suffix('.json')
                meta.parent.mkdir(parents=True,exist_ok=True);meta.write_text(json.dumps(record));frames.append(record)
            catalog={'domains':{'bc':{'layers':{'radar-rain':{'frames':copy.deepcopy(frames)}}}}}
            keys=project_frames(root,catalog)
            public=catalog['domains']['bc']['layers']['radar-rain']['frames']
            self.assertEqual(public[0]['path'], public[1]['path'])
            self.assertNotEqual(public[0]['validTime'],public[1]['validTime'])
            self.assertEqual(len(keys),2)
            bundle=next(k for k in keys if k.endswith('.gz'))
            recovered=json.loads(gzip.decompress((root/bundle).read_bytes()))['records']
            self.assertEqual([v['metadata'] for v in recovered.values()], frames)
            (root/frames[0]['path']).write_bytes(b'corrected pixels')
            project_frames(root,catalog)
            self.assertNotEqual(public[0]['path'],public[1]['path'])
            self.assertEqual((root/public[1]['path']).read_bytes(),b'pixels')

    def test_shared_upload_race_rewrites_failures_and_missing_object_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'state.sqlite3';config=R2Config('account','a','s');client=MemoryR2()
            ledger=PublishState(state,'account/radar-sat');ledger.close()
            path=root/'image.png';path.write_bytes(b'pixels')
            def item():return LocalObject('frames/image.png',path,path.stat().st_size,path.stat().st_mtime_ns)
            with ThreadPoolExecutor(4) as pool:
                results=list(pool.map(lambda _:put_if_changed(client,config,item(),state),range(4)))
            self.assertEqual(sum(r[1] for r in results),1)
            os.utime(path,None)
            self.assertFalse(put_if_changed(client,config,item(),state)[1])
            path.write_bytes(b'correction');self.assertTrue(put_if_changed(client,config,item(),state)[1])
            with patch.object(client,'put_object',side_effect=RuntimeError('failed')), patch('radarsat.r2.retry',side_effect=lambda f,*a:f()):
                path.write_bytes(b'next correction')
                with self.assertRaises(RuntimeError):put_if_changed(client,config,item(),state)
            self.assertTrue(put_if_changed(client,config,item(),state)[1])
            def pointer(time,value):return put_pointer(client,config,json.dumps({'generatedAt':time,'value':value}).encode(),'catalog.json',state)
            self.assertTrue(pointer('2026-09-09T16:00:00Z',1))
            self.assertFalse(pointer('2026-09-09T16:10:00Z',1))
            self.assertFalse(pointer('2026-09-09T16:05:00Z',2))
            self.assertTrue(pointer('2026-09-09T16:11:00Z',2))
            ledger=PublishState(state,'account/radar-sat');ledger.forget(['catalog.json']);ledger.close()
            self.assertTrue(pointer('2026-09-09T16:11:00Z',2))

    def test_rapid_reintroduces_old_blob_during_full_catalog_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            state=PublishState(Path(directory)/'state.sqlite3','account/radar-sat')
            now=dt.datetime.now(dt.timezone.utc)
            key='frame-blobs/bc/previously-unused.png'
            state.protect_catalog(set(), committed=True, now=now)
            self.assertEqual(state.deletable_keys([key],now),[key])
            state.protect_live_edge([key],now)
            self.assertEqual(state.deletable_keys([key],now),[])
            self.assertEqual(state.deletable_keys([key],now+dt.timedelta(hours=3)),[key])
            state.close()

    def test_compact_cleanup_protects_references_and_recent_rapid_uploads(self):
        now=dt.datetime.now(dt.timezone.utc);old=now-dt.timedelta(hours=30)
        remote={'frame-blobs/bc/old.png':1,'frame-blobs/bc/referenced.png':1,'frame-blobs/bc/new.png':1}
        modified={k:old for k in remote};modified['frame-blobs/bc/new.png']=now
        self.assertEqual(expired_compact_keys(remote,modified,{'frame-blobs/bc/referenced.png'},now),['frame-blobs/bc/old.png'])

class CompactIntegrationTests(unittest.TestCase):
    run_publish = fixtures.PublisherReconciliationTests.run_publish
    # Run the purpose-built migration test only, not inherited legacy-path assertions.
    def setUp(self):
        fixtures.PublisherReconciliationTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'RADARSAT_R2_COMPACT_PUBLICATION':'1'}).start()
    def test_full_and_rapid_share_blob_and_noop_catalogs(self):
        self.state=self.state.parent/'r2-publish.sqlite3'
        result=self.run_publish()
        published=json.loads(self.client.objects['catalog.json'])
        frame=published['domains']['bc']['layers']['radar-rain']['frames'][0]
        self.assertTrue(frame['path'].startswith('frame-blobs/'))
        self.assertEqual(self.client.objects[frame['path']],self.frame.read_bytes())
        self.client.events.clear()
        first=publish_live_edge(self.root,self.config,client=self.client,now=self.now,state_path=self.state.parent/'edge.json')
        self.assertEqual(first['uploadedObjects'],0)
        self.catalog['generatedAt']='2026-09-09T16:10:00Z'
        (self.root/'catalog.json').write_text(json.dumps(self.catalog))
        self.client.events.clear()
        result=self.run_publish()
        self.assertEqual(result['uploaded'],0)
        self.assertEqual(result['catalogUploads'],0)
        self.assertFalse(any(e[0]=='put' for e in self.client.events))
