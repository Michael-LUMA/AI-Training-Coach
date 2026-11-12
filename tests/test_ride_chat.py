"""Offline Chinese chat-brain tests (synthetic data, no network)."""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server
import ride_chat
import ride_brain as brain
from action_cards import ActionError


def memory(identifier='current', started='2026-11-07T08:00:00Z'):
    return {'schema_version':'luma-ride-memory/1','id':identifier,'started_at':started,
            'ended_at':started.replace('08:00:00','08:01:00'),'samples_hz':1,
            'series':[{'t':0,'hr':120},{'t':60,'hr':120}],
            'summary':{'duration_s':60,'avg_hr':120,'max_hr':120,'time_in_zone_s':None,
                       'zone_target':None,'gaps':0,'alerts':0},
            'coach_notes':['private note not for cloud'],'persona_version':'ride-persona/4',
            'source':'ble_gatt_0x180d'}


def transport(speech='这次平均心率120 bpm，记录了1分钟。', *, status=200, draft=None, finish='stop'):
    envelope={'choices':[{'finish_reason':finish,'message':{'content':json.dumps({'speech':speech} if draft is None else draft,ensure_ascii=False)}}],
              'usage':{'prompt_tokens':11,'completion_tokens':12}}
    return Mock(return_value=(status, {}, json.dumps(envelope,ensure_ascii=False).encode('utf-8')))


def facts_of(post):
    return json.loads(json.loads(post.call_args.args[0].data)['messages'][-1]['content'])


class ChatQuestionValidationTests(unittest.TestCase):
    def test_invalid_question_rejected_without_upstream(self):
        post=Mock(side_effect=AssertionError('no network'))
        for question in ('','   ','问'*201,'心率多少？\x00','心率多少？\n','心率多少？\x7f',None,123,['心率']):
            with self.subTest(question=repr(question)):
                result=ride_chat.build_ride_chat(question,session=memory(),
                                                 api_key='synthetic-key',http_post=post)
                self.assertEqual(result['llm_status'],'rejected_bad_schema')
                self.assertIsNone(result['draft']);self.assertEqual(result['fallback'],'template')
        post.assert_not_called()

    def test_boundary_question_lengths_accepted(self):
        for question in ('？','问'*200):
            with self.subTest(length=len(question)):
                result=ride_chat.build_ride_chat(question,session=memory(),
                                                 api_key='synthetic-key',http_post=transport())
                self.assertEqual(result['llm_status'],'ok')

    def test_not_configured_never_calls_transport_or_reads_env(self):
        post=Mock(side_effect=AssertionError('no network'))
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY':'must-not-read'}):
            for key in (None,'','   '):
                result=ride_chat.build_ride_chat('心率多少？',session=memory(),api_key=key,http_post=post)
                self.assertEqual(result,{'llm_status':'not_configured','draft':None,'usage':None,'fallback':'template'})
        post.assert_not_called()


class ChatFactsTests(unittest.TestCase):
    def chat(self,question='这次心率多少？',speech='这次平均心率120 bpm。',**kwargs):
        kwargs.setdefault('api_key','synthetic-key')
        return ride_chat.build_ride_chat(question,**kwargs)

    def test_session_facts_projected_question_included_no_key_in_body(self):
        post=transport()
        result=self.chat(session=memory(),http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        post.assert_called_once()
        request=post.call_args.args[0]
        self.assertEqual(request.full_url,brain.ENDPOINT)
        self.assertEqual(request.get_header('Authorization'),'Bearer synthetic-key')
        self.assertNotIn(b'synthetic-key',request.data)
        facts=facts_of(post)
        self.assertEqual(facts['question'],'这次心率多少？')
        self.assertEqual(facts['summary']['avg_hr'],120)
        self.assertEqual(facts['activity'],'ride')
        self.assertEqual(facts['display']['duration_min'],1)
        for forbidden in ('private note','series','started_at','persona_version','current','chat'):
            self.assertNotIn(forbidden,json.dumps(facts,ensure_ascii=False))
        system=json.loads(request.data)['messages'][0]['content']
        self.assertEqual(system,ride_chat.SYSTEM_PROMPT)
        self.assertNotEqual(system,brain.SYSTEM_PROMPT)

    def test_no_session_gives_history_facts_only(self):
        recent=[{'duration_s':120,'avg_hr':130,'max_hr':140,'time_in_zone_s':None,
                 'zone_target':None,'gaps':0,'alerts':0}]
        post=transport('近一次平均心率130，时长2分钟，这是你第3次骑行。')
        result=self.chat(question='我最近骑得怎么样？',session=None,
                         recent_summaries=recent,activity_ordinal=3,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        facts=facts_of(post)
        self.assertNotIn('summary',facts)
        self.assertNotIn('source',facts)
        self.assertEqual(facts['recent_trend']['avg_hr_list'],[130])
        self.assertEqual(facts['recent_trend']['duration_min_list'],[2])
        self.assertEqual(facts['session_ordinal'],3)

    def test_empty_history_facts_are_reachable_i_dont_know(self):
        post=transport('这个我没记到。')
        result=self.chat(question='我上周骑了多久？',session=None,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        self.assertEqual(facts_of(post),{'question':'我上周骑了多久？'})

    def test_missing_sensor_metric_short_circuits_without_llm(self):
        post=transport('平均踏频92转。')
        result=self.chat(question='我这次的平均踏频是多少？',session=None,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        self.assertEqual(result['draft'],{'speech':'这个我没记到。'})
        post.assert_not_called()

    def test_missing_metric_gate_unit_levels(self):
        self.assertTrue(ride_chat._missing_metric_answer('我这次踏频多少？',{'summary':{'duration_s':60}}))
        self.assertFalse(ride_chat._missing_metric_answer('我这次踏频多少？',{'summary':{'avg_cadence':88}}))
        self.assertFalse(ride_chat._missing_metric_answer('我该怎么练踏频？',{'summary':{}}))
        self.assertFalse(ride_chat._missing_metric_answer('我心率多少？',{'summary':{}}))

    def test_no_chat_history_in_payload(self):
        post=transport()
        first=self.chat(question='第一个问题',session=memory(),http_post=post)
        self.assertEqual(first['llm_status'],'ok')
        second_post=transport()
        second=self.chat(question='第二个问题',session=memory(),http_post=second_post)
        self.assertEqual(second['llm_status'],'ok')
        body=json.loads(second_post.call_args.args[0].data)
        self.assertEqual(len(body['messages']),2)
        self.assertNotIn('第一个问题',json.dumps(body,ensure_ascii=False))


class ChatVerificationTests(unittest.TestCase):
    def answer(self,speech,**kwargs):
        kwargs.setdefault('api_key','synthetic-key')
        kwargs.setdefault('session',memory())
        return ride_chat.build_ride_chat('这次心率多少？',http_post=transport(speech),**kwargs)

    def test_invented_number_rejects_entire_draft(self):
        for speech in ('这次平均心率999。','这次骑了500公里。'):
            with self.subTest(speech=speech):
                result=self.answer(speech)
                self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
                self.assertIsNone(result['draft']);self.assertEqual(result['fallback'],'template')

    def test_verified_numbers_pass(self):
        for speech in ('这次平均心率120 bpm。','这次记录了1分钟。','平均心率一百二十，时长六十秒。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.answer(speech)['llm_status'],'ok')

    def test_forbidden_prescription_and_medical_rejected(self):
        for speech in ('你变强了，建议加大强度。','这个训练效果不错。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.answer(speech)['llm_status'],'rejected_unsafe_speech')

    def test_i_dont_know_answer_passes(self):
        result=self.answer('这个我没记到。')
        self.assertEqual(result['llm_status'],'ok')
        self.assertEqual(result['draft'],{'speech':'这个我没记到。'})

    def test_simulated_source_must_be_disclosed(self):
        item=memory();item['source']='simulated'
        hidden=self.answer('这次平均心率120。',session=item)
        self.assertEqual(hidden['llm_status'],'rejected_unsafe_speech')
        shown=self.answer('模拟记录：这次平均心率120。',session=item)
        self.assertEqual(shown['llm_status'],'ok')
        post=transport('模拟记录：这次平均心率120。')
        ride_chat.build_ride_chat('心率？',session=memory(identifier='x')|{'source':'simulated'},
                                  api_key='synthetic-key',http_post=post)
        self.assertIn('speech_rule',facts_of(post))

    def test_malformed_drafts_rejected(self):
        for draft in ({},{'speech':''},{'speech':'   '},{'speech':'好'*301},{'speech':'行','extra':1}):
            with self.subTest(draft=draft):
                result=ride_chat.build_ride_chat('心率？',session=memory(),api_key='synthetic-key',
                                                 http_post=transport(draft=draft))
                self.assertEqual(result['llm_status'],'rejected_bad_schema')

    def test_upstream_failures_map_to_fallback(self):
        for post in (Mock(return_value=(500,{},b'{}')),
                     Mock(return_value=(200,{},b'not json')),
                     Mock(return_value=(200,{},json.dumps({'error':{'message':'x'}}).encode())),
                     Mock(side_effect=TimeoutError())):
            with self.subTest(post=post):
                result=ride_chat.build_ride_chat('心率？',session=memory(),
                                                 api_key='synthetic-key',http_post=post)
                self.assertEqual(result['llm_status'],'upstream_error')
                self.assertEqual(result['fallback'],'template')


class ChatTemplateTests(unittest.TestCase):
    def test_template_snapshot(self):
        self.assertEqual(ride_chat.chat_template(),'这个我现在答不上来，等我记到更多数据再聊。')
        self.assertEqual(ride_chat.TEMPLATE,ride_chat.chat_template())

    def test_session_not_mutated(self):
        item=memory();before=copy.deepcopy(item)
        ride_chat.build_ride_chat('心率？',session=item,api_key='synthetic-key',http_post=transport())
        self.assertEqual(item,before)


class ChatEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.post=transport();self.app=coach_server.App(store=SimpleNamespace(path=Path(self.temp.name)/'coach.sqlite3'),llm_post=self.post)
        self.env=patch.dict(os.environ,{'DASHSCOPE_API_KEY':''});self.env.start();self.addCleanup(self.env.stop)

    def request(self,path,payload,headers=None):
        body=json.dumps(payload,ensure_ascii=False).encode()
        default={'Host':'127.0.0.1:8794','Origin':'http://127.0.0.1:8794','Content-Type':'application/json',
                 'Content-Length':str(len(body)),'X-Coach-Token':self.app.token}
        default.update(headers or {})
        return self.app.dispatch('POST',path,default,body)

    def save(self,item=None):
        return self.request('/api/coach/ride/session',memory() if item is None else item)

    def v3(self,identifier,started,avg_hr,source='ble_gatt_0x180d'):
        return {'schema_version':'luma-ride-memory/3','id':identifier,'started_at':started,
                'ended_at':started.replace('00:00Z','01:00Z'),'samples_hz':1,
                'series':[{'t':0,'hr':avg_hr},{'t':60,'hr':avg_hr}],
                'summary':{'duration_s':60,'avg_hr':avg_hr,'max_hr':avg_hr,'time_in_zone_s':None,
                           'zone_target':None,'gaps':0,'alerts':0,'avg_cadence':None,'avg_power':None,
                           'max_power':None,'distance_m':None,'avg_speed_mps':None,'max_speed_mps':None},
                'coach_notes':[],'persona_version':'ride-persona/14','source':source}

    def test_whitelist_and_security_headers(self):
        with self.assertRaises(ActionError) as error:
            self.request('/api/coach/ride/chats',{'question':'心率？'})
        self.assertEqual(error.exception.status,404)
        for headers in ({'X-Coach-Token':'bad'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            with self.subTest(headers=headers):
                with self.assertRaises(ActionError) as error:
                    self.request('/api/coach/ride/chat',{'question':'心率？'},headers)
                self.assertEqual(error.exception.status,403)
        self.post.assert_not_called()

    def test_field_validation_and_invalid_question_shape(self):
        for payload in ({},{'session_id':'current'},{'question':'心率？','extra':1},
                        {'question':'心率？','session_id':'../secret'},{'question':'心率？','session_id':[]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ActionError) as error:
                    self.request('/api/coach/ride/chat',payload)
                self.assertEqual(error.exception.status,400)
        self.post.assert_not_called()
        self.save()
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'问'*201,'session_id':'current'})
        self.assertEqual(result['speech_status'],'rejected_bad_schema')
        self.assertEqual(result['fallback'],'template')
        self.post.assert_not_called()

    def test_missing_session_404(self):
        with self.assertRaises(ActionError) as error:
            self.request('/api/coach/ride/chat',{'question':'心率？','session_id':'missing'})
        self.assertEqual((error.exception.status,error.exception.code),(404,'not_found'))
        self.post.assert_not_called()

    def test_tz_offset_reaches_general_lane_now_bundle(self):
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'聊聊古埃及金字塔','tz_offset_min':480})
        self.assertEqual((result['speech_status'],result['lane']),('ok','general'))
        facts=json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])
        self.assertRegex(facts['now']['local_time'],r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
        self.assertIn(facts['now']['weekday'],[f'星期{d}' for d in '一二三四五六日'])
        from datetime import datetime as dt, timedelta as td, timezone as tz
        expected=dt.now(tz(td(minutes=480))).strftime('%Y-%m-%d %H:%M')
        self.assertEqual(facts['now']['local_time'],expected)

    def test_invalid_tz_offset_rejected(self):
        for bad in ('480',480.5,True,-721,841,None):
            if bad is None:
                continue
            with self.subTest(tz_offset_min=repr(bad)):
                with self.assertRaises(ActionError) as error:
                    self.request('/api/coach/ride/chat',{'question':'心率？','tz_offset_min':bad})
                self.assertEqual(error.exception.status,400)
        self.post.assert_not_called()

    def test_weather_question_without_bundle_is_deterministic_honesty(self):
        result=self.request('/api/coach/ride/chat',{'question':'今天天气怎么样？'})
        self.assertEqual((result['speech_status'],result['lane']),('ok','personal'))
        self.assertIn('定位',result['speech'])
        self.post.assert_not_called()

    def test_conversational_self_never_routes_personal(self):
        self.post.return_value=transport('金字塔是古埃及法老的陵墓群。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'给我讲个有意思的运动冷知识'})
        self.assertIn(result['lane'],('knowledge','general'))
        self.assertNotEqual(result['lane'],'personal')

    def test_unconfigured_returns_quiet_template_shape(self):
        self.save()
        result=self.request('/api/coach/ride/chat',{'question':'心率？','session_id':'current'})
        self.assertEqual(result,{'speech_status':'not_configured','speech':None,'fallback':'template','usage':None,'lane':'knowledge'})
        self.post.assert_not_called()

    def test_chat_with_session_and_history_aggregation(self):
        self.save(self.v3('older','2026-11-05T08:00:00Z',138))
        self.save(self.v3('sim','2026-11-06T08:00:00Z',140,source='simulated'))
        self.save()
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'这次心率多少？','session_id':'current'})
        self.assertEqual(result['speech_status'],'ok')
        self.assertEqual(result['speech'],'这次平均心率120 bpm，记录了1分钟。')
        facts=json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])
        self.assertEqual(facts['question'],'这次心率多少？')
        self.assertEqual(facts['summary']['avg_hr'],120)
        self.assertEqual(facts['recent_trend']['avg_hr_list'],[138])
        self.assertEqual(facts['session_ordinal'],2)
        self.assertEqual(facts['excluded_simulated'],1)

    def test_chat_without_session_uses_history_only(self):
        self.save(self.v3('one','2026-11-05T08:00:00Z',138))
        self.save(self.v3('two','2026-11-06T08:00:00Z',135))
        self.post.return_value=transport('近两次平均心率138和135 bpm。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'我最近几次心率多少？'})
        self.assertEqual(result['speech_status'],'ok')
        facts=json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])
        self.assertNotIn('summary',facts)
        self.assertEqual(facts['recent_trend']['avg_hr_list'],[138,135])
        self.assertEqual(facts['session_ordinal'],2)

    def test_chat_counts_toward_shared_post_bucket(self):
        self.save()
        for _ in range(59):
            self.request('/api/coach/ride/chat',{'question':'心率？','session_id':'current'})
        with self.assertRaises(ActionError) as error:
            self.request('/api/coach/ride/chat',{'question':'心率？','session_id':'current'})
        self.assertEqual((error.exception.status,error.exception.code),(429,'rate_limited'))

    def test_session_save_keeps_tight_disk_bucket(self):
        for _ in range(10):
            self.save()
        with self.assertRaises(ActionError) as error:
            self.save()
        self.assertEqual((error.exception.status,error.exception.code),(429,'rate_limited'))

    def test_chat_totals_aggregated_under_lock(self):
        day=lambda n: date.today().replace(day=n).isoformat()+'T08:00:00Z'
        first=self.v3('one',day(1),138)
        first['summary'].update(distance_m=1200,avg_speed_mps=20.0,max_speed_mps=25.0)
        second=self.v3('two',day(2),135)
        second['summary'].update(distance_m=1200,avg_speed_mps=20.0,max_speed_mps=25.0)
        self.save(first);self.save(second)
        self.post.return_value=transport('你累计骑了2次，总共2分钟，累计2.4公里。本月2次。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'我累计骑了多少？'})
        self.assertEqual(result['speech_status'],'ok')
        facts=json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])
        self.assertEqual(facts['totals'],{'sessions_count':2,'duration_min':2,'distance_km':2.4,
                                          'month':{'sessions':2,'duration_min':2,'distance_km':2.4}})

    def test_chat_totals_distance_hidden_when_any_session_lacks_it(self):
        day=lambda n: date.today().replace(day=n).isoformat()+'T08:00:00Z'
        first=self.v3('one',day(1),138)
        first['summary'].update(distance_m=1200,avg_speed_mps=20.0,max_speed_mps=25.0)
        self.save(first);self.save(self.v3('two',day(2),135))
        self.post.return_value=transport('你累计骑了2次，总共2分钟。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'我累计骑了多少？'})
        self.assertEqual(result['speech_status'],'ok')
        facts=json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])
        self.assertIsNone(facts['totals']['distance_km'])
        self.assertIsNone(facts['totals']['month']['distance_km'])

    def test_personal_marker_routes_personal_lane(self):
        self.save()
        self.post.return_value=transport('这次平均心率120 bpm。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'这次心率多少？','session_id':'current'})
        self.assertEqual((result['speech_status'],result['lane']),('ok','personal'))
        self.assertEqual(self.post.call_count,1)

    def test_knowledge_question_routes_knowledge_lane(self):
        cards=self.app.knowledge_cards
        body=next(card['body'] for card in cards if card['id']=='cadence-basics')
        self.assertNotRegex(body,r'\d')
        speech='踏频是曲柄每分钟转动的圈数，和车速不同。（来源：通用运动常识）'
        self.post.return_value=transport(speech).return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'踏频是什么？'})
        self.assertEqual((result['speech_status'],result['lane']),('ok','knowledge'))
        self.assertEqual(self.post.call_count,1)

    def test_preferred_lane_failure_falls_through_once(self):
        # A question with no matching card goes to the general lane, which
        # chats freely: no prefix required, common-knowledge numbers allowed.
        speech='金字塔是古埃及法老的陵墓群，多在尼罗河西岸。'
        self.post.return_value=transport(speech).return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'聊聊古埃及金字塔'})
        self.assertEqual((result['speech_status'],result['lane']),('ok','general'))
        # General answers may carry common-knowledge numbers.
        self.post.return_value=transport('胡夫金字塔高约146米。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'聊聊古埃及金字塔'})
        self.assertEqual((result['speech_status'],result['lane']),('ok','general'))
        # The forbidden-word gate still applies to the general lane.
        self.post.return_value=transport('坚持训练效果明显，你会变强。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'聊聊怎么练'})
        self.assertEqual((result['speech_status'],result['lane']),('rejected_unsafe_speech','general'))
        # 个人道的核验拒稿绝不跨道：用户数据诚实是底线，别的道只会编造。
        self.post.return_value=transport('心率999。').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'我这次心率多少？'})
        self.assertEqual((result['speech_status'],result['lane']),('rejected_unverifiable_number','personal'))
        # 知识卡的核验拒稿回退通用道：通用道答问题本身（自带安全门），不碰坏卡。
        self.post.return_value=transport('踏频一般是90转。（来源：通用运动常识）').return_value
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'踏频是什么？'})
        self.assertEqual((result['speech_status'],result['lane']),('ok','general'))
        # Transport failure still consults the other lane exactly once.
        self.post.return_value=(500,{},b'{}')
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/chat',{'question':'我这次心率多少？'})
        self.assertEqual((result['speech_status'],result['lane']),('upstream_error','knowledge'))

    def test_chat_writes_no_history_to_disk(self):
        self.save()
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            self.request('/api/coach/ride/chat',{'question':'心率？','session_id':'current'})
        stored=[path.name for path in self.app.ride_dir.glob('*.json')]
        self.assertEqual(stored,['current.json'])


class ChatTotalsTests(unittest.TestCase):
    def totals(self):
        return {'sessions_count':12,'duration_min':600,'distance_km':201.5,
                'month_sessions':3,'month_duration_min':150,'month_distance_km':60.2}

    def chat(self,totals,**kwargs):
        kwargs.setdefault('api_key','synthetic-key')
        return ride_chat.build_ride_chat('我累计和本月骑了多少？',totals=totals,**kwargs)

    def test_invalid_totals_rejected_without_upstream(self):
        invalid=[False,12,1.5,'totals',[],(),set()]
        for key in self.totals():
            missing=self.totals();del missing[key]
            invalid.append(missing)
        invalid.append(self.totals()|{'extra':1})
        limits={'sessions_count':100000,'duration_min':600000,'distance_km':200000,
                'month_sessions':100000,'month_duration_min':100000,'month_distance_km':100000}
        for key,maximum in limits.items():
            values=[-1,maximum+1,'1',True,False,float('nan'),float('inf'),
                    float('-inf'),ride_chat.Decimal('1'),[],{}]
            if key == 'sessions_count':
                values.extend([0,1.0,1.5,None])
            elif key == 'month_sessions':
                values.extend([1.0,1.5,None])
            elif key not in ('distance_km','month_distance_km'):
                values.append(None)
            for value in values:
                invalid.append(self.totals()|{key:value})
        for totals in invalid:
            for session in (None,memory()):
                with self.subTest(totals=totals,session=session is not None):
                    post=Mock(side_effect=AssertionError('no network'))
                    result=self.chat(totals,session=session,http_post=post)
                    self.assertEqual(result['llm_status'],'rejected_bad_schema')
                    self.assertIsNone(result['draft'])
                    self.assertEqual(result['fallback'],'template')
                    post.assert_not_called()

    def test_none_totals_remains_optional(self):
        self.assertEqual(ride_chat._totals_facts(None),({},[]))
        post=transport('这个我没记到。')
        result=self.chat(None,session=None,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        self.assertEqual(facts_of(post),{'question':'我累计和本月骑了多少？'})

    def test_totals_only_projection_and_verified_speech(self):
        totals=self.totals();before=copy.deepcopy(totals)
        speech='你累计骑了12次，一共600分钟，累计201.5公里。本月3次。'
        post=transport(speech)
        result=self.chat(totals,session=None,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        self.assertEqual(result['draft'],{'speech':speech})
        facts=facts_of(post)
        self.assertEqual(facts,{'totals':{'sessions_count':12,'duration_min':600,
                                        'distance_km':201.5,
                                        'month':{'sessions':3,'duration_min':150,
                                                 'distance_km':60.2}},
                               'question':'我累计和本月骑了多少？'})
        self.assertLess(list(facts).index('totals'),list(facts).index('question'))
        self.assertEqual(totals,before)

    def test_zero_month_sessions_is_honest_and_accepted(self):
        totals=self.totals()|{'month_sessions':0,'month_duration_min':0,'month_distance_km':None}
        post=transport('本月还没有记录。')
        result=self.chat(totals,session=None,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        self.assertEqual(facts_of(post)['totals']['month'],{'sessions':0,'duration_min':0,'distance_km':None})

    def test_totals_numbers_registered_with_decimal_and_units(self):
        _,numbers=ride_chat._totals_facts(self.totals())
        expected=[('12',None),('600','min'),('201.5','km'),
                  ('3',None),('150','min'),('60.2','km')]
        self.assertEqual(numbers,[(ride_chat.Decimal(value),unit) for value,unit in expected])
        for value,_ in numbers:
            self.assertIs(type(value),ride_chat.Decimal)

    def test_all_totals_numbers_are_available_to_verification(self):
        for speech in ('累计12次。','累计600分钟。','累计201.5公里。',
                       '本月3次。','本月150分钟。','本月60.2公里。'):
            with self.subTest(speech=speech):
                result=self.chat(self.totals(),http_post=transport(speech))
                self.assertEqual(result['llm_status'],'ok')

    def test_invented_totals_count_rejected(self):
        result=self.chat(self.totals(),session=None,http_post=transport('你累计骑了13次'))
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
        self.assertIsNone(result['draft'])
        self.assertEqual(result['fallback'],'template')

    def test_null_distances_preserved_and_not_registered(self):
        for key in ('distance_km','month_distance_km'):
            with self.subTest(key=key):
                totals=self.totals()|{key:None}
                facts,numbers=ride_chat._totals_facts(totals)
                if key=='distance_km':
                    self.assertIsNone(facts['distance_km'])
                else:
                    self.assertIsNone(facts['month']['distance_km'])
                self.assertEqual(len(numbers),5)
                self.assertNotIn((ride_chat.Decimal(str(self.totals()[key])),'km'),numbers)
        totals=self.totals()|{'distance_km':None,'month_distance_km':None}
        post=transport('累计12次，共600分钟。本月3次，共150分钟。')
        result=self.chat(totals,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        self.assertIsNone(facts_of(post)['totals']['distance_km'])
        self.assertIsNone(facts_of(post)['totals']['month']['distance_km'])
        _,numbers=ride_chat._totals_facts(totals)
        self.assertFalse(any(unit=='km' for _,unit in numbers))

    def test_null_distances_reject_any_claimed_kilometers(self):
        totals=self.totals()|{'distance_km':None,'month_distance_km':None}
        for number in (0,1,3,12,150,600,201.5,60.2):
            with self.subTest(number=number):
                result=self.chat(totals,http_post=transport(f'累计{number}公里。'))
                self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
                self.assertIsNone(result['draft'])

    def test_totals_merge_with_session_and_history(self):
        post=transport('这次平均心率120 bpm，累计12次，本月150分钟。')
        result=self.chat(self.totals(),session=memory(),recent_summaries=[memory()['summary']],
                         activity_ordinal=12,excluded_simulated=2,http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        facts=facts_of(post)
        self.assertEqual(facts['summary']['avg_hr'],120)
        self.assertEqual(facts['totals']['sessions_count'],12)
        self.assertEqual(facts['totals']['month']['duration_min'],150)
        self.assertEqual(facts['recent_trend']['count'],1)
        self.assertEqual(facts['session_ordinal'],12)
        self.assertEqual(facts['excluded_simulated'],2)
        self.assertEqual(facts['question'],'我累计和本月骑了多少？')

    def test_totals_numeric_boundaries_and_floats_accepted(self):
        for totals in ({'sessions_count':1,'duration_min':0,'distance_km':0,
                        'month_sessions':1,'month_duration_min':0,'month_distance_km':0},
                       {'sessions_count':100000,'duration_min':600000,'distance_km':200000,
                        'month_sessions':100000,'month_duration_min':100000,'month_distance_km':100000},
                       self.totals()|{'duration_min':600.5,'month_duration_min':150.5}):
            with self.subTest(totals=totals):
                result=self.chat(totals,http_post=transport('这个我没记到。'))
                self.assertEqual(result['llm_status'],'ok')


if __name__=='__main__':
    unittest.main()


class ChatDialogHistoryTests(unittest.TestCase):
    """Optional bounded dialog context: projected, validated, never registering numbers."""

    def chat(self, question='这次心率多少？', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        kwargs.setdefault('session', memory())
        return ride_chat.build_ride_chat(question, **kwargs)

    def test_history_projects_into_facts(self):
        post = transport()
        history = [{'q': '我心率多少？', 'a': '平均心率120 bpm。'},
                   {'q': '为什么？', 'a': '只记录了1分钟。'}]
        result = self.chat(history=history, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(facts_of(post)['recent_dialog'], history)

    def test_no_history_omits_recent_dialog(self):
        post = transport()
        self.assertEqual(self.chat(http_post=post)['llm_status'], 'ok')
        self.assertNotIn('recent_dialog', facts_of(post))

    def test_empty_history_projects_as_empty_dialog(self):
        post = transport()
        self.assertEqual(self.chat(history=[], http_post=post)['llm_status'], 'ok')
        self.assertEqual(facts_of(post)['recent_dialog'], [])

    def test_invalid_history_rejected_without_upstream(self):
        post = Mock(side_effect=AssertionError('no network'))
        cases = ([{'q': '问' * 201, 'a': '答'}], [{'q': '问', 'a': '答' * 481}],
                 [{'q': '问'}], [{'q': '问', 'a': '答', 'x': 1}],
                 [{'q': '', 'a': '答'}], [{'q': '问', 'a': ''}],
                 [{'q': '问\n', 'a': '答'}], [{'q': '问', 'a': '答\x7f'}],
                 [{'q': '问', 'a': '答'}] * 6, '问', 42, [{'q': 1, 'a': '答'}],
                 [['问', '答']])
        for history in cases:
            with self.subTest(history=repr(history)[:40]):
                result = self.chat(history=history, http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_history_numbers_never_register(self):
        # 999 exists only inside the dialog context; quoting it must fail verification.
        post = transport('这次平均心率999 bpm。')
        history = [{'q': '我心率多少？', 'a': '刚才是999 bpm。'}]
        result = self.chat(history=history, http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_history_not_mutated_and_boundaries_accepted(self):
        history = [{'q': '问' * 200, 'a': '答' * 300}] * 3
        snapshot = copy.deepcopy(history)
        post = transport()
        result = self.chat(history=history, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(history, snapshot)
        self.assertEqual(facts_of(post)['recent_dialog'], snapshot)


class ChatConversationTests(unittest.TestCase):
    """Server-side short memory: per conv_id, RAM-only, TTL-bounded, ok-turns only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.post = transport()
        self.app = coach_server.App(store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
                                    llm_post=self.post)
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''})
        self.env.start(); self.addCleanup(self.env.stop)

    def request(self, path, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        default = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                   'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                   'X-Coach-Token': self.app.token}
        default.update(headers or {})
        return self.app.dispatch('POST', path, default, body)

    def save(self):
        return self.request('/api/coach/ride/session', memory())

    def ask(self, question, conv_id='conv-1'):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            return self.request('/api/coach/ride/chat',
                                {'question': question, 'session_id': 'current', 'conv_id': conv_id})

    def last_facts(self):
        return json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])

    def test_bad_conv_id_rejected_before_any_work(self):
        for bad in ('conv id', '中文', '-x', 'x' * 65, '', 123, ['conv-1'], None):
            if bad is None:
                continue  # absence is fine; None as a value is a schema error below
            with self.subTest(conv_id=repr(bad)):
                with self.assertRaises(ActionError) as error:
                    self.request('/api/coach/ride/chat', {'question': '心率？', 'conv_id': bad})
                self.assertEqual(error.exception.status, 400)
        self.post.assert_not_called()

    def test_followup_carries_recent_dialog_and_isolates_conversations(self):
        self.save()
        first = self.ask('这次心率多少？')
        self.assertEqual((first['speech_status'], first['lane']), ('ok', 'personal'))
        second = self.ask('这次呢？')
        self.assertEqual(second['speech_status'], 'ok')
        self.assertEqual(self.last_facts()['recent_dialog'],
                         [{'q': '这次心率多少？', 'a': first['speech']}])
        # A different conversation sees nothing.
        third = self.ask('这次呢？', conv_id='conv-2')
        self.assertEqual(third['speech_status'], 'ok')
        self.assertNotIn('recent_dialog', self.last_facts())

    def test_only_successful_answers_are_remembered(self):
        self.save()
        self.post.return_value = transport('心率999。').return_value
        result = self.ask('这次心率多少？')
        self.assertNotEqual(result['speech_status'], 'ok')
        self.assertNotIn('conv-1', self.app._convs)

    def test_dialog_truncates_to_three_turns(self):
        self.save()
        for index in range(5):
            self.assertEqual(self.ask(f'这次心率多少？第{index}问')['speech_status'], 'ok')
        turns = self.app._convs['conv-1'][0]
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0]['q'], '这次心率多少？第2问')

    def test_idle_dialog_self_destructs(self):
        self.save()
        self.assertEqual(self.ask('这次心率多少？')['speech_status'], 'ok')
        self.assertIn('conv-1', self.app._convs)
        # Age the entry past the TTL; the next read sweeps it before use.
        self.app._convs['conv-1'][1] -= coach_server._CONV_TTL_S + 1
        self.assertIsNone(self.app._dialog_read('conv-1'))
        self.assertNotIn('conv-1', self.app._convs)
        # The follow-up question then carries no dialog context.
        self.assertEqual(self.ask('这次呢？')['speech_status'], 'ok')
        self.assertNotIn('recent_dialog', self.last_facts())

    def test_memory_only_never_touches_disk(self):
        self.save()
        self.assertEqual(self.ask('这次心率多少？')['speech_status'], 'ok')
        self.assertIn('conv-1', self.app._convs)
        stored = [path.name for path in self.app.ride_dir.glob('*')]
        self.assertEqual(stored, ['current.json'])


class ChatProfileTests(unittest.TestCase):
    """Usage-profile facts: validated bundle, registered numbers, honest scope."""

    def profile(self):
        return {'sessions': 5, 'usual_start_hour': 20, 'usual_duration_min': 45}

    def chat(self, profile, **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_chat.build_ride_chat('我一般什么时候出门？', session=None, profile=profile, **kwargs)

    def test_profile_projection_and_verified_speech(self):
        profile = self.profile(); before = copy.deepcopy(profile)
        post = transport('你通常20点出门，通常骑45分钟。')
        result = self.chat(profile, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(profile, before)
        facts = facts_of(post)
        self.assertEqual(facts['profile'], {'sessions': 5, 'usual_start_hour': 20,
                                            'usual_duration_min': 45})
        self.assertIn('profile', ride_chat.SYSTEM_PROMPT)

    def test_unregistered_hour_rejected(self):
        post = transport('你通常21点出门。')
        result = self.chat(self.profile(), http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')
        self.assertIsNone(result['draft'])

    def test_null_fields_stay_unmentioned(self):
        post = transport('你已经记录了5次。')
        result = self.chat({'sessions': 5, 'usual_start_hour': None, 'usual_duration_min': None},
                           http_post=post)
        self.assertEqual(result['llm_status'], 'ok')

    def test_invalid_profiles_rejected_without_upstream(self):
        invalid = [None, False, 5, 'profile', [], {'sessions': 5},
                   self.profile() | {'extra': 1},
                   self.profile() | {'sessions': 2},
                   self.profile() | {'sessions': '5'},
                   self.profile() | {'sessions': 5.0},
                   self.profile() | {'usual_start_hour': 24},
                   self.profile() | {'usual_start_hour': -1},
                   self.profile() | {'usual_start_hour': 20.5},
                   self.profile() | {'usual_start_hour': '20'},
                   self.profile() | {'usual_duration_min': 0},
                   self.profile() | {'usual_duration_min': -5},
                   self.profile() | {'usual_duration_min': '45'},
                   self.profile() | {'usual_duration_min': 600001},
                   self.profile() | {'usual_duration_min': float('nan')}]
        for profile in invalid:
            if profile is None:
                continue  # None means "not provided", handled separately
            with self.subTest(profile=repr(profile)):
                post = Mock(side_effect=AssertionError('no network'))
                result = self.chat(profile, http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
                post.assert_not_called()

    def test_none_profile_leaves_no_trace(self):
        post = transport('这个我没记到。')
        result = self.chat(None, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertNotIn('profile', facts_of(post))


class ChatProfileServerTests(unittest.TestCase):
    """Server computes the profile from stored real sessions only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.post = transport('这个我没记到。')
        self.app = coach_server.App(store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
                                    llm_post=self.post)

    def request(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                   'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                   'X-Coach-Token': self.app.token}
        return self.app.dispatch('POST', path, headers, body)

    def save(self, identifier, started, source='ble_gatt_0x180d'):
        from datetime import datetime, timedelta, timezone
        begin = datetime.fromisoformat(started.replace('Z', '+00:00'))
        end = (begin + timedelta(seconds=60)).isoformat().replace('+00:00', 'Z')
        item = memory(identifier, started)
        item['ended_at'] = end
        item['source'] = source
        return self.request('/api/coach/ride/session', item)

    def ask(self, question='我一般什么时候出门？'):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            return self.request('/api/coach/ride/chat', {'question': question})

    def last_facts(self):
        return json.loads(json.loads(self.post.call_args.args[0].data)['messages'][-1]['content'])

    def test_profile_from_three_stored_sessions(self):
        self.save('a', '2026-11-05T08:00:00Z')
        self.save('b', '2026-11-06T09:00:00Z')
        self.save('c', '2026-11-07T20:00:00Z')
        self.assertEqual(self.ask()['speech_status'], 'ok')
        profile = self.last_facts()['profile']
        self.assertEqual(profile['sessions'], 3)
        expected_hour = sorted(__import__('datetime').datetime.fromisoformat(t).astimezone().hour
                               for t in ('2026-11-05T08:00:00+00:00', '2026-11-06T09:00:00+00:00',
                                         '2026-11-07T20:00:00+00:00'))[1]
        self.assertEqual(profile['usual_start_hour'], expected_hour)
        self.assertEqual(profile['usual_duration_min'], 1)

    def test_simulated_sessions_never_shape_profile(self):
        self.save('a', '2026-11-05T08:00:00Z', source='simulated')
        self.save('b', '2026-11-06T09:00:00Z', source='simulated')
        self.save('c', '2026-11-07T20:00:00Z', source='simulated')
        self.assertEqual(self.ask()['speech_status'], 'ok')
        self.assertNotIn('profile', self.last_facts())

    def test_two_real_sessions_stay_silent(self):
        self.save('a', '2026-11-05T08:00:00Z')
        self.save('b', '2026-11-06T09:00:00Z')
        self.save('c', '2026-11-07T20:00:00Z', source='simulated')
        self.assertEqual(self.ask()['speech_status'], 'ok')
        self.assertNotIn('profile', self.last_facts())

    def test_week_counts_only_current_week(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        recent = now.isoformat().replace('+00:00', 'Z')
        old = (now - timedelta(days=14)).isoformat().replace('+00:00', 'Z')
        self.save('this-week', recent)
        self.save('two-weeks-ago', old)
        self.assertEqual(self.ask()['speech_status'], 'ok')
        facts = self.last_facts()
        self.assertEqual(facts['week']['sessions'], 1)
        self.assertEqual(facts['week']['duration_min'], 1)
        self.assertIsNone(facts['week']['distance_km'])

    def test_week_present_from_first_session(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        self.save('only', now)
        self.assertEqual(self.ask()['speech_status'], 'ok')
        self.assertEqual(self.last_facts()['week']['sessions'], 1)


class ChatWeekTests(unittest.TestCase):
    """Current-week aggregate facts: validated bundle, registered numbers."""

    def week(self):
        return {'sessions': 3, 'duration_min': 90, 'distance_km': 30.5}

    def chat(self, week, **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_chat.build_ride_chat('我这周骑了多少？', session=None, week=week, **kwargs)

    def test_week_projection_and_verified_speech(self):
        week = self.week(); before = copy.deepcopy(week)
        post = transport('你本周记录了3次，一共90分钟、30.5公里。')
        result = self.chat(week, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(week, before)
        self.assertEqual(facts_of(post)['week'], self.week())
        self.assertIn('week', ride_chat.SYSTEM_PROMPT)

    def test_unregistered_week_number_rejected(self):
        post = transport('你本周骑了4次。')
        result = self.chat(self.week(), http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_null_distance_stays_unmentioned(self):
        post = transport('你本周记录了3次，一共90分钟。')
        result = self.chat({'sessions': 3, 'duration_min': 90, 'distance_km': None},
                           http_post=post)
        self.assertEqual(result['llm_status'], 'ok')

    def test_invalid_week_rejected_without_upstream(self):
        invalid = [False, 3, 'week', [], {'sessions': 3},
                   self.week() | {'extra': 1},
                   self.week() | {'sessions': -1},
                   self.week() | {'sessions': 3.0},
                   self.week() | {'sessions': '3'},
                   self.week() | {'duration_min': -1},
                   self.week() | {'duration_min': '90'},
                   self.week() | {'duration_min': 600001},
                   self.week() | {'distance_km': '30'},
                   self.week() | {'distance_km': -1},
                   self.week() | {'distance_km': 200001}]
        for week in invalid:
            with self.subTest(week=repr(week)):
                post = Mock(side_effect=AssertionError('no network'))
                result = self.chat(week, http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
                post.assert_not_called()

    def test_zero_session_week_is_valid(self):
        post = transport('你本周还没有记录。')
        result = self.chat({'sessions': 0, 'duration_min': 0, 'distance_km': None},
                           http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(facts_of(post)['week']['sessions'], 0)

    def test_none_week_leaves_no_trace(self):
        post = transport('这个我没记到。')
        result = self.chat(None, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertNotIn('week', facts_of(post))


class ChatWeatherTests(unittest.TestCase):
    """Client-relayed AMap weather: validated bundle, unitless temperatures."""

    def weather(self):
        return {'city': '上海市', 'day_weather': '多云', 'day_temp': 15,
                'night_weather': '晴', 'night_temp': 9}

    def chat(self, weather, question='今天天气怎么样？', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_chat.build_ride_chat(question, session=None, weather=weather, **kwargs)

    def test_weather_projection_and_verified_speech(self):
        weather = self.weather(); before = copy.deepcopy(weather)
        post = transport('高德天气：上海市今天白天多云，15度；夜间晴，9度。')
        result = self.chat(weather, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(weather, before)
        facts = facts_of(post)
        self.assertEqual(facts['weather'], {'source': 'amap', **self.weather()})
        self.assertIn('weather', ride_chat.SYSTEM_PROMPT)

    def test_string_temperatures_normalize_to_int(self):
        post = transport('高德天气：上海市今天白天多云，15度；夜间晴，9度。')
        bundle = self.weather() | {'day_temp': '15', 'night_temp': '9'}
        result = self.chat(bundle, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(facts_of(post)['weather']['day_temp'], 15)

    def test_unregistered_temperature_rejected(self):
        post = transport('今天白天16度。')
        result = self.chat(self.weather(), http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_missing_field_drops_bundle(self):
        bundle = {key: value for key, value in self.weather().items() if key != 'city'}
        post = transport('这个我没记到。')
        result = self.chat(bundle, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertNotIn('weather', facts_of(post))

    def test_malformed_weather_drops_out_whole(self):
        invalid = ['weather', 3, [],
                   self.weather() | {'day_temp': 71},
                   self.weather() | {'day_temp': 'hot'},
                   self.weather() | {'night_temp': None},
                   self.weather() | {'day_temp': True},
                   self.weather() | {'city': ''},
                   self.weather() | {'day_weather': '多云' * 40},
                   self.weather() | {'night_weather': '晴\n'}]
        for bundle in invalid:
            with self.subTest(weather=repr(bundle)[:60]):
                post = transport('这个我没记到。')
                result = self.chat(bundle, http_post=post)
                self.assertEqual(result['llm_status'], 'ok')
                self.assertNotIn('weather', facts_of(post))

    def test_none_weather_leaves_no_trace(self):
        post = transport('这个我没记到。')
        result = self.chat(None, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertNotIn('weather', facts_of(post))


class ChatNavTests(unittest.TestCase):
    """Client-relayed AMap route state: validated bundle, deterministic honesty."""

    def nav(self):
        return {'destination': '奥林匹克森林公园南园', 'remaining_km': 12.3,
                'step_index': 2, 'step_count': 9, 'off_route': False,
                'next_instruction': '左转进入林萃路', 'next_distance_m': 350}

    def chat(self, nav, question='还有多远？', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_chat.build_ride_chat(question, session=None, nav=nav, **kwargs)

    def test_nav_projection_and_verified_speech(self):
        nav = self.nav(); before = copy.deepcopy(nav)
        post = transport('沿规划路线去奥林匹克森林公园南园，还剩12.3公里；前方350米左转进入林萃路。')
        result = self.chat(nav, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(nav, before)
        facts = facts_of(post)
        self.assertEqual(facts['nav'], {'source': 'amap', **self.nav()})
        self.assertIn('nav', ride_chat.SYSTEM_PROMPT)

    def test_unregistered_distance_rejected(self):
        post = transport('还剩13公里。')
        result = self.chat(self.nav(), http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_turn_trap_phrase_rejected_not_shipped(self):
        # 「下一转向」归一化后会把「一转」读成 1 rpm —— 这种措辞必须被核验链拦下。
        post = transport('还剩12.3公里；下一转向：左转进入林萃路。')
        result = self.chat(self.nav(), http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_nav_question_without_route_is_deterministic_honesty(self):
        calls = []
        def counting_post(request, timeout=20):
            calls.append(request)
            return transport('这个我没记到。')(request, timeout=timeout)
        result = self.chat(None, http_post=counting_post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['draft'], {'speech': '现在没有进行中的导航路线。'})
        self.assertEqual(calls, [])

    def test_non_nav_question_without_route_still_asks(self):
        post = transport('这个我没记到。')
        result = self.chat(None, question='我累计骑了多久？', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertNotIn('nav', facts_of(post))

    def test_malformed_nav_drops_out_whole(self):
        invalid = ['nav', 3, [],
                   self.nav() | {'destination': ''},
                   self.nav() | {'destination': '奥体' * 60},
                   self.nav() | {'destination': '奥体\n'},
                   self.nav() | {'remaining_km': -1},
                   self.nav() | {'remaining_km': 2001},
                   self.nav() | {'remaining_km': True},
                   self.nav() | {'remaining_km': '12.3'},
                   self.nav() | {'step_index': 0},
                   self.nav() | {'step_index': 10},
                   self.nav() | {'step_count': '9'},
                   self.nav() | {'off_route': 'yes'},
                   self.nav() | {'next_instruction': ''},
                   self.nav() | {'next_instruction': '左转\n'},
                   self.nav() | {'next_distance_m': -5},
                   self.nav() | {'next_distance_m': None}]
        for bundle in invalid:
            with self.subTest(nav=repr(bundle)[:60]):
                post = transport('这个我没记到。')
                result = self.chat(bundle, question='我累计骑了多久？', http_post=post)
                self.assertEqual(result['llm_status'], 'ok')
                self.assertNotIn('nav', facts_of(post))

    def test_arrived_route_omits_turn_fields(self):
        bundle = {'destination': '奥体', 'remaining_km': 0,
                  'step_index': 9, 'step_count': 9, 'off_route': False}
        post = transport('沿规划路线去奥体，还剩0公里。')
        result = self.chat(bundle, http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        facts = facts_of(post)['nav']
        self.assertNotIn('next_instruction', facts)
        self.assertNotIn('next_distance_m', facts)
