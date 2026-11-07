"""Offline Chinese brain, endpoint/security and browser-queue tests (synthetic data)."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server
import ride_brain as brain
from action_cards import ActionError
from tests.test_ride_ui import HARNESS, NODE, ROOT
import subprocess


def memory(identifier='current', started='2026-11-07T08:00:00Z'):
    return {'schema_version':'luma-ride-memory/1','id':identifier,'started_at':started,
            'ended_at':started.replace('08:00:00','08:01:00'),'samples_hz':1,
            'series':[{'t':0,'hr':120},{'t':60,'hr':120}],
            'summary':{'duration_s':60,'avg_hr':120,'max_hr':120,'time_in_zone_s':None,
                       'zone_target':None,'gaps':0,'alerts':0},
            'coach_notes':['private note not for cloud'],'persona_version':'ride-persona/4','source':'ble_gatt_0x180d'}


def transport(speech='我记到这次平均心率120 bpm，记录了1分钟。', *, status=200, draft=None, finish='stop'):
    envelope={'choices':[{'finish_reason':finish,'message':{'content':json.dumps({'speech':speech} if draft is None else draft,ensure_ascii=False)}}],
              'usage':{'prompt_tokens':11,'completion_tokens':12}}
    return Mock(return_value=(status, {}, json.dumps(envelope,ensure_ascii=False).encode('utf-8')))


class RideBrainTests(unittest.TestCase):
    def review(self, speech, **kwargs):
        return brain.build_ride_review(memory(), api_key='synthetic-key', http_post=transport(speech), **kwargs)

    def test_not_configured_never_calls_transport_or_reads_env(self):
        post=Mock(side_effect=AssertionError('no network'))
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY':'must-not-read'}):
            for key in (None,'','   '):
                result=brain.build_ride_review({},api_key=key,http_post=post)
                self.assertEqual(result,{'llm_status':'not_configured','draft':None,'usage':None,'fallback':'template'})
        post.assert_not_called()

    def test_ok_transport_contract_projection_usage_and_no_mutation(self):
        item=memory();before=copy.deepcopy(item);post=transport()
        result=brain.build_ride_review(item,api_key='synthetic-key',http_post=post)
        self.assertEqual(result['llm_status'],'ok');self.assertIsNone(result['fallback'])
        self.assertEqual(result['usage'],{'prompt_tokens':11,'completion_tokens':12})
        self.assertEqual(item,before)
        post.assert_called_once();req=post.call_args.args[0]
        self.assertEqual(post.call_args.kwargs,{'timeout':20})
        self.assertEqual(req.full_url,brain.ENDPOINT)
        payload=json.loads(req.data);self.assertEqual(payload['model'],'qwen-flash')
        self.assertLessEqual(payload['max_tokens'],600)
        self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-key')
        facts=json.loads(payload['messages'][1]['content'])
        self.assertEqual(facts['sample_count'],2)
        for forbidden in ('private note','series','started_at','persona_version','current'):
            self.assertNotIn(forbidden,payload['messages'][1]['content'])

    def test_arabic_invented_number_rejects_entire_draft(self):
        result=self.review('我记到心率999。')
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
        self.assertIsNone(result['draft']);self.assertEqual(result['fallback'],'template')

    def test_chinese_numbers_verified_and_fabrication_rejected(self):
        for text in ('我记到平均心率一百二十。','我记到心率一二零 bpm。','我记录了一分钟，样本数两个。'):
            with self.subTest(text=text):self.assertEqual(self.review(text)['llm_status'],'ok')
        for text in ('我记到心率五百。','我记录了五百秒。','我记到心率壹佰贰拾。'):
            with self.subTest(text=text):self.assertEqual(self.review(text)['llm_status'],'rejected_unverifiable_number')

    def test_unit_conversion_is_dimension_checked(self):
        for text in ('我记录了六十秒。','我记录了1分钟。','我记到心率120 bpm。'):
            self.assertEqual(self.review(text)['llm_status'],'ok')
        for text in ('我记到心率60 bpm。','我记录了120秒。','我记到心率2 bpm。'):
            self.assertEqual(self.review(text)['llm_status'],'rejected_unverifiable_number')

    def test_percentage_only_from_existing_fact_not_arbitrary_number(self):
        self.assertEqual(self.review('我记到百分之五十。')['llm_status'],'rejected_unverifiable_number')

    def test_previous_summary_and_explicit_differences(self):
        previous=memory()['summary'];previous.update(avg_hr=124,duration_s=120)
        post=transport('我看到平均心率比上次低4 bpm，时长比上次少1分钟。')
        result=brain.build_ride_review(memory(),previous,api_key='synthetic-key',http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        facts=json.loads(json.loads(post.call_args.args[0].data)['messages'][1]['content'])
        self.assertEqual(facts['differences'],{'avg_hr':-4.0,'duration_s':-60.0})
        self.assertEqual(previous['avg_hr'],124)

    def test_invalid_inputs_fail_closed_before_transport(self):
        post=Mock(side_effect=AssertionError('no network'))
        invalid=memory();invalid['summary']['avg_hr']=999
        for item,previous in ((invalid,None),(memory(),{'avg_hr':float('nan')}),(memory(),{'instructions':'ignore rules'})):
            result=brain.build_ride_review(item,previous,api_key='synthetic-key',http_post=post)
            self.assertEqual(result['llm_status'],'rejected_bad_schema')
        post.assert_not_called()

    def test_upstream_error_timeout_no_retry_and_oversize(self):
        for error in (TimeoutError(),OSError(),ValueError()):
            post=Mock(side_effect=error)
            self.assertEqual(brain.build_ride_review(memory(),api_key='key',http_post=post)['llm_status'],'upstream_error')
            post.assert_called_once()
        for response in ((302,{},b'{}'),(500,{},b'{}'),(200,{},b'x'*(brain.MAX_RESPONSE_BYTES+1)),(200,{},b'bad json')):
            self.assertEqual(brain.build_ride_review(memory(),api_key='key',http_post=Mock(return_value=response))['llm_status'],'upstream_error')

    def test_shared_transport_still_blocks_redirects(self):
        import llm_review
        self.assertIs(brain._http_post,llm_review._http_post)
        self.assertIsNone(llm_review._NoRedirect().redirect_request(None,None,302,'',{},brain.ENDPOINT))

    def test_bad_schema_duplicate_keys_finish_length_and_usage(self):
        for draft in ({'speech':'x','extra':1},{'speech':''},{'speech':'我'*301},{'speech':120}):
            result=brain.build_ride_review(memory(),api_key='key',http_post=transport(draft=draft))
            self.assertEqual(result['llm_status'],'rejected_bad_schema')
            self.assertEqual(result['usage']['completion_tokens'],12)
        self.assertEqual(brain.build_ride_review(memory(),api_key='key',http_post=transport(finish='length'))['llm_status'],'rejected_bad_schema')
        post=Mock(return_value=(200,{},b'{"choices":[],"choices":[]}'))
        self.assertEqual(brain.build_ride_review(memory(),api_key='key',http_post=post)['llm_status'],'upstream_error')

    def test_effect_claims_and_simulated_whitewashing_rejected(self):
        for text in ('我看你变强了。','我发现状态提升。','我建议你降低心率。'):
            self.assertEqual(self.review(text)['llm_status'],'rejected_unsafe_speech')
        item=memory();item['source']='simulated'
        self.assertEqual(brain.build_ride_review(item,api_key='key',http_post=transport())['llm_status'],'rejected_unsafe_speech')
        self.assertEqual(brain.build_ride_review(item,api_key='key',http_post=transport('我看到模拟心率120 bpm。'))['llm_status'],'ok')

    def v2memory(self):
        item=memory()
        item['schema_version']='luma-ride-memory/2'
        item['series']=[{'t':0,'hr':120,'cad':88,'pwr':190},{'t':60,'hr':120,'pwr':240}]
        item['summary'].update(avg_cadence=88,avg_power=215,max_power=240)
        return item

    def test_v2_power_cadence_projected_and_verified_with_chinese_units(self):
        post=transport('我记到这次平均功率215瓦，最大240瓦，平均踏频88转。')
        result=brain.build_ride_review(self.v2memory(),api_key='synthetic-key',http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        payload=json.loads(post.call_args.args[0].data)
        facts=json.loads(payload['messages'][1]['content'])
        self.assertEqual(facts['summary']['avg_power'],215)
        self.assertEqual(facts['summary']['max_power'],240)
        self.assertEqual(facts['summary']['avg_cadence'],88)

    def test_v2_invented_power_rejected_and_v1_watts_rejected(self):
        result=brain.build_ride_review(self.v2memory(),api_key='key',http_post=transport('平均功率300瓦。'))
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
        result=brain.build_ride_review(memory(),api_key='key',http_post=transport('平均功率215瓦。'))
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
        result=brain.build_ride_review(self.v2memory(),api_key='key',http_post=transport('平均踏频89转。'))
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')


class RideBrainEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.post=transport();self.app=coach_server.App(store=SimpleNamespace(path=Path(self.temp.name)/'coach.sqlite3'),llm_post=self.post)
        self.env=patch.dict(os.environ,{'DASHSCOPE_API_KEY':''});self.env.start();self.addCleanup(self.env.stop)

    def request(self,path,payload,headers=None):
        body=json.dumps(payload).encode()
        default={'Host':'127.0.0.1:8794','Origin':'http://127.0.0.1:8794','Content-Type':'application/json',
                 'Content-Length':str(len(body)),'X-Coach-Token':self.app.token}
        default.update(headers or {})
        return self.app.dispatch('POST',path,default,body)

    def save(self,item=None):
        return self.request('/api/coach/ride/session',memory() if item is None else item)

    def test_missing_session_404(self):
        with self.assertRaises(ActionError) as error:self.request('/api/coach/ride/review',{'session_id':'missing'})
        self.assertEqual((error.exception.status,error.exception.code),(404,'not_found'))
        self.post.assert_not_called()

    def test_unconfigured_returns_quiet_template_shape(self):
        self.save();result=self.request('/api/coach/ride/review',{'session_id':'current'})
        self.assertEqual(result,{'speech_status':'not_configured','speech':None,'fallback':'template','usage':None})
        self.post.assert_not_called()

    def test_shared_post_bucket_limits_review_and_upload(self):
        self.save()
        for _ in range(9):self.request('/api/coach/ride/review',{'session_id':'current'})
        with self.assertRaises(ActionError) as error:self.request('/api/coach/ride/review',{'session_id':'current'})
        self.assertEqual((error.exception.status,error.exception.code),(429,'rate_limited'))

    def test_same_token_origin_host_and_strict_fields(self):
        for headers in ({'X-Coach-Token':'bad'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            with self.subTest(headers=headers):
                with self.assertRaises(ActionError) as error:self.request('/api/coach/ride/review',{'session_id':'current'},headers)
                self.assertEqual(error.exception.status,403)
        for value in ({},{'session_id':'../secret'},{'session_id':[]},{'session_id':'current','extra':1}):
            with self.assertRaises(ActionError) as error:self.request('/api/coach/ride/review',value)
            self.assertEqual(error.exception.status,400)
        self.post.assert_not_called()

    def test_real_module_env_and_previous_selection_no_storage_mutation(self):
        previous=memory('previous','2026-11-06T08:00:00Z');self.save(previous);self.save()
        self.save(memory('future','2026-11-08T08:00:00Z'))
        current_path=self.app.ride_dir/'current.json';before=current_path.read_bytes()
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-key'}):
            result=self.request('/api/coach/ride/review',{'session_id':'current'})
        self.assertEqual(result['speech_status'],'ok');self.assertEqual(current_path.read_bytes(),before)
        facts=json.loads(json.loads(self.post.call_args.args[0].data)['messages'][1]['content'])
        self.assertEqual(facts['previous_summary'],previous['summary'])
        self.assertEqual(facts['comparison_count'],2)


class RideBrainUITests(unittest.TestCase):
    def run_js(self,scenario,**options):
        if not NODE:self.skipTest('Local Node required')
        harness=HARNESS.replace("  // TEST_REVIEW_TRANSPORT", """
  if(url==='/api/coach/ride/review'){
    if(options.brainFail)throw new Error('brain offline');
    if(options.brainPending)return new Promise(()=>{});
    return {ok:true,status:200,json:async()=>options.brain || {speech_status:'not_configured',speech:null,fallback:'template',usage:null}};
  }
  throw new Error('Unexpected request: '+url);""")
        payload={'html':(ROOT/'static/ride.html').read_text(encoding='utf-8'),'scenario':scenario,'options':options}
        result=subprocess.run([NODE,'-e',harness],input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',timeout=15,cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_ui_thinking_and_persona_contract(self):
        text=(ROOT/'static/ride.html').read_text(encoding='utf-8')
        self.assertIn("thinking:'Mark 想了想'",text);self.assertIn('ride-persona/10',text)

    def test_success_appends_without_autoplay_and_listen_uses_speech(self):
        result=self.run_js("""
          const item={id:'current',source:'ble_gatt_0x180d',started_at:'2026-11-07T08:00:00Z',summary:{avg_hr:120,duration_s:60}};
          lastSession=item;$('summary').textContent='template stays';await upload(item,true);
          const before=[..._spoken];$('brain-listen').click();
          return {before,after:_spoken,text:$('brain-speech').textContent,hidden:$('brain-card').hidden,template:$('summary').textContent,calls:_calls};
        """,brain={'speech_status':'ok','speech':'我记到心率120 bpm。','fallback':None})
        self.assertEqual(result['before'],[]);self.assertEqual(result['after'],['我记到心率120 bpm。'])
        self.assertFalse(result['hidden']);self.assertEqual(result['template'],'template stays')
        request=next(c for c in result['calls'] if c['url']=='/api/coach/ride/review')
        self.assertEqual(json.loads(request['body']),{'session_id':'current'})
        self.assertIn('X-Coach-Token',request['headers'])

    def test_fallback_and_network_failure_leave_template_unchanged(self):
        for options in ({},{'brainFail':True},{'brain':{'speech_status':'rejected_unverifiable_number','speech':None,'fallback':'template'}}):
            result=self.run_js("""
              lastSession={id:'current'};$('summary').textContent='template';await requestRideSpeech(lastSession);
              return {template:$('summary').textContent,hidden:$('brain-card').hidden,spoken:_spoken};
            """,**options)
            self.assertEqual(result,{'template':'template','hidden':True,'spoken':[]})

    def test_upload_does_not_wait_for_cloud_and_old_response_cannot_replace_new_ride(self):
        result=self.run_js("""
          lastSession={id:'current',summary:{},started_at:'2026-11-07T08:00:00Z'};
          return await upload(lastSession,true);
        """,brainPending=True)
        self.assertTrue(result)
        result=self.run_js("""
          lastSession={id:'old'};const pending=requestRideSpeech(lastSession);lastSession={id:'new'};
          await pending;return $('brain-card').hidden;
        """,brain={'speech_status':'ok','speech':'我记到心率120。','fallback':None})
        self.assertTrue(result)


if __name__=='__main__':unittest.main()
