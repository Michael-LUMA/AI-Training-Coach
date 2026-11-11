"""Synthetic GPS-only end-to-end records, isolated disk, no external services."""
import copy,json,os,tempfile,unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from coach_server import App,CoachStore
from ride_memory import validate_session
from ride_weekly import aggregate_ride_weeks
from tests.test_ride_chat import transport,facts_of


def gps_record(identifier='mark-test', measured=True):
    return validate_session({'schema_version':'luma-ride-memory/3','id':identifier,
        'started_at':'2026-11-10T00:00:00Z','ended_at':'2026-11-10T00:02:00Z',
        'samples_hz':1,'series':[],
        'summary':{'duration_s':120,'avg_hr':None,'max_hr':None,'time_in_zone_s':None,
            'zone_target':None,'gaps':0,'alerts':0,'avg_cadence':None,'avg_power':None,
            'max_power':None,'distance_m':120 if measured else None,
            'avg_speed_mps':1 if measured else None,'max_speed_mps':2 if measured else None},
        'coach_notes':[],'persona_version':'mark-gps/1','source':'web_gps','activity':'ride'})


class MarkRecordClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.post=transport('最近一次记录了2分钟，没有心率数据。')
        self.app=App(store=CoachStore(Path(self.temp.name)/'test.sqlite3'),
                     today=lambda:date(2026,11,10),llm_post=self.post)
        env=patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key',
                                  'LLM_FAST_ORDER':'dashscope','LLM_SMART_ORDER':'dashscope'})
        env.start();self.addCleanup(env.stop)

    def request(self,path,body=None):
        raw=json.dumps(body).encode() if body is not None else b''
        headers={'Host':'127.0.0.1:8794','Origin':'http://127.0.0.1:8794',
                 'X-Coach-Token':self.app.token,'Content-Type':'application/json',
                 'Content-Length':str(len(raw))}
        return self.app.dispatch('POST' if body is not None else 'GET',path,headers,raw)

    def test_save_list_chat_weekly_share_the_same_real_source_record(self):
        record=gps_record()
        for _ in range(2):
            self.assertEqual(self.request('/api/coach/ride/session',record)['status'],'saved')
        listed=self.request('/api/coach/ride/sessions')['sessions']
        self.assertEqual(len(listed),1)
        self.assertEqual(listed[0]['summary']['avg_hr'],None)
        result=self.request('/api/coach/ride/chat',{'question':'最近一次骑行怎么样？','session_id':record['id'],'tz_offset_min':480})
        self.assertEqual(result['lane'],'personal')
        facts=json.dumps(facts_of(self.post),ensure_ascii=False)
        self.assertIn('web_gps',facts)
        self.assertNotIn('ble_gatt_0x180d',facts)
        weekly=self.request('/api/coach/ride/weekly',{'week_start':'2026-11-09'})
        self.assertEqual(weekly['week'],{'count':1,'total_duration_s':120})

    def test_missing_gps_and_hr_survive_persistence_without_zero_fill(self):
        self.request('/api/coach/ride/session',gps_record(measured=False))
        summary=self.request('/api/coach/ride/sessions')['sessions'][0]['summary']
        self.assertIsNone(summary['distance_m']);self.assertIsNone(summary['avg_hr'])

    def test_weekly_source_is_not_relabeled_as_ble(self):
        gps=gps_record()
        self.assertEqual(aggregate_ride_weeks([gps],[])['source'],'web_gps')
        imported=copy.deepcopy(gps);imported.update(id='import-test',source='imported_gpx')
        self.assertEqual(aggregate_ride_weeks([gps,imported],[])['source'],'mixed')
        self.assertIsNone(aggregate_ride_weeks([],[])['source'])

if __name__=='__main__':unittest.main()
