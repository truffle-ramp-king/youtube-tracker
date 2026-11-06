import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import youtube_tracker as t

CID = 'UC' + 'a' * 22
NOW = t.dt('2026-10-26T12:00:00Z')

def item(vid='abcdefghijk', views=700000, age=2, region=None):
    return {'id':vid, 'snippet':{'title':'Story', 'channelId':CID, 'channelTitle':'Stories', 'publishedAt':t.stamp(NOW-timedelta(days=age))}, 'contentDetails':{'duration':'PT20S', 'regionRestriction':region or {}}, 'statistics':{} if views is None else {'viewCount':str(views)}, 'status':{'privacyStatus':'public'}}

class FakeAPI:
    def __init__(self, responses): self.responses=iter(responses);self.calls=[]
    def get(self, method, **kwargs): self.calls.append((method,kwargs));return next(self.responses)

class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.db=t.connect(self.path/'test.sqlite3')
        self.watch={'channels':[{'id':CID,'enabled':True}]}
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def save(self, data):
        with patch.object(t,'utcnow',return_value=NOW): t.save_videos(self.db,FakeAPI([{'items':data}]),[i['id'] for i in data])
    def test_env_is_data_and_environment_takes_precedence(self):
        p=self.path/'.env';p.write_text('export YOUTUBE_DATA_API_KEY="literal$(never_execute)" # hi\n')
        with patch.dict(os.environ,{},clear=True):self.assertEqual(t.load_key(p),'literal$(never_execute)')
        with patch.dict(os.environ,{'YOUTUBE_DATA_API_KEY':'override'}):self.assertEqual(t.load_key(p),'override')
    def test_duration_and_deduplicated_batches(self):
        self.assertEqual(t.seconds('P1DT2H3M4.5S'),93784.5)
        self.assertEqual(t.seconds('bad'),0)
        self.assertEqual(list(t.batches(['a','a','b'],1)),[['a'],['b']])
    def test_views_missing_region_and_used_are_excluded(self):
        self.save([item(),item('bbbbbbbbbbb',views=None),item('ccccccccccc',region={'blocked':['US']}),item('ddddddddddd',age=100)])
        self.assertEqual([v['id'] for v in t.ranked(self.db,self.watch,now=NOW)],['abcdefghijk'])
        self.db.execute('INSERT INTO decisions VALUES (?,?,?,?)',('abcdefghijk','edited','',t.stamp(NOW)))
        self.assertEqual(t.ranked(self.db,self.watch,now=NOW),[])
        self.assertEqual(len(t.ranked(self.db,self.watch,include_used=True,now=NOW)),1)
    def test_snapshot_growth_requires_six_hours(self):
        self.save([item()])
        self.assertIsNone(t.ranked(self.db,self.watch,now=NOW)[0]['measured_views_per_day'])
        self.db.execute('INSERT INTO snapshots VALUES (?,?,?)',('abcdefghijk',t.stamp(NOW-timedelta(days=1)),600000))
        self.assertEqual(t.ranked(self.db,self.watch,now=NOW)[0]['measured_views_per_day'],100000)
    def test_excluded_channel_never_returns_in_broad_rankings(self):
        self.save([item()])
        self.watch['channels'][0]['excluded'] = True
        for broad in (False, True):
            self.assertEqual(t.ranked(self.db,self.watch,all_channels=broad,include_used=True,now=NOW),[])
        self.watch['channels'][0].update(excluded=False,enabled=False)
        self.assertEqual(len(t.ranked(self.db,self.watch,all_channels=True,now=NOW)),1)
    def test_excluded_channel_is_not_refreshed(self):
        self.watch['channels'][0]['excluded'] = True
        api=FakeAPI([])
        result=t.refresh(self.db,api,self.watch,60,3)
        self.assertEqual(result['videos_updated'],0)
        self.assertEqual(api.calls,[])
    def test_missing_video_marked_unavailable(self):
        self.save([item()]);t.save_videos(self.db,FakeAPI([{'items':[]}]),['abcdefghijk'])
        self.assertEqual(t.ranked(self.db,self.watch,now=NOW),[])
    def test_recent_views_and_diversity(self):
        self.assertGreater(t.score_video({'views':1000000},1),t.score_video({'views':1000000},80))
        self.assertGreater(t.score_video({'views':2000000},1),t.score_video({'views':1000000},1))
        rows=[{'id':1,'channel_id':'a'},{'id':2,'channel_id':'a'},{'id':3,'channel_id':'b'}]
        self.assertEqual([r['id'] for r in t.diversify(rows,2,1)],[1,3])
    def test_pagination_and_cutoff(self):
        c={'id':CID,'snippet':{'title':'Stories'},'contentDetails':{'relatedPlaylists':{'uploads':'UUtest'}}}
        api=FakeAPI([{'items':[c]}, {'items':[{'contentDetails':{'videoId':'abcdefghijk','videoPublishedAt':t.stamp(NOW)}}], 'nextPageToken':'next'}, {'items':[{'contentDetails':{'videoId':'oldoldoldol','videoPublishedAt':'2020-01-01T00:00:00Z'}}],'nextPageToken':'unused'}, {'items':[item()]}])
        with patch.object(t,'utcnow',return_value=NOW): result=t.refresh(self.db,api,self.watch,90,3)
        self.assertEqual(result['videos_updated'],1)
        self.assertFalse(result['coverage'][0]['page_cap_reached'])
        self.assertEqual(api.calls[2][1]['pageToken'],'next')
        self.assertEqual(api.calls[3][1]['id'],'abcdefghijk')
    def test_http_key_not_in_url_and_error_redaction(self):
        class Opener:
            def open(self,req,timeout):
                self.req=req
                raise HTTPError(req.full_url,403,'bad',{},io.BytesIO(json.dumps({'error':{'message':'secret-key','errors':[{'reason':'quotaExceeded'}]}}).encode()))
        o=Opener();api=t.YouTube('secret-key',opener=o)
        with self.assertRaises(t.APIError) as ctx: api.get('videos',id='abcdefghijk')
        self.assertNotIn('secret-key',str(ctx.exception));self.assertNotIn('secret-key',o.req.full_url)
        self.assertEqual(o.req.get_header('X-goog-api-key'),'secret-key')
        with self.assertRaises(t.APIError):t.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')
    def test_request_budget(self):
        with self.assertRaises(t.APIError):t.YouTube('unused',max_requests=0).get('videos',id='abcdefghijk')

if __name__=='__main__':unittest.main()
