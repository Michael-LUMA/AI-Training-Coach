"""Explanation boundaries and v2 preview controls; no external calls."""
import copy,json,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
import ride_brain as brain
from tools import generate_l3_blind_samples as legacy
from tools import generate_l3_explanation_preview as preview
from tests.test_ride_brain import transport


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.case=legacy.load_cases(preview.CORPUS)[0]
        self.session=legacy.session_for(self.case)
        self.facts,self.numbers=brain._facts(self.session,self.case['previous_summary'])

    def test_new_trend_dimensions_and_units(self):
        recent=[{'avg_hr':120,'duration_s':1200,'avg_speed_mps':5,'ascent_m':90.5},
                {'avg_hr':None,'duration_s':None,'avg_speed_mps':None,'ascent_m':None}]
        f,n=brain._facts(self.session,None,recent_summaries=recent)
        self.assertEqual(f['recent_trend']['avg_speed_kmh_list'],[18,None])
        self.assertEqual(f['recent_trend']['ascent_m_list'],[91,None])
        self.assertIn((Decimal(18),'km/h'),n)
        self.assertIn((Decimal(91),'m'),n)
        self.assertNotIn((Decimal('90.5'),'bpm'),n)
        with self.assertRaises(ValueError):
            brain._facts(self.session,None,recent_summaries=[{'ascent_m':-1}])

    def test_cloud_retrieval_and_history_chat_preserve_new_dimensions(self):
        from unittest.mock import patch
        import memory_service as memory
        import ride_chat
        from tests.test_profile_service import record,NOW
        records=[]
        for day,speed,ascent in ((3,26,100),(2,24,200),(1,22,300)):
            item=record(day)
            item['summary'].update(avg_speed_mps=speed/3.6,max_speed_mps=30/3.6,ascent_m=ascent,
                                   distance_m=speed/3.6*item['summary']['duration_s'])
            records.append(item)
        with patch.object(memory,'_records',return_value=records), patch('dialog_store.get_recent',return_value=None):
            context=memory.get_context_for_chat('a','最近均速和爬升如何变化',now=NOW)
        recent=context['recent_summaries']
        self.assertTrue(recent)
        self.assertTrue(all('avg_speed_mps' in item and 'ascent_m' in item for item in recent))
        facts,numbers=ride_chat._history_facts(recent,None,None)
        self.assertEqual(facts['recent_trend']['avg_speed_kmh_list'],[round(item['avg_speed_mps']*3.6,1) for item in recent])
        self.assertEqual(facts['recent_trend']['ascent_m_list'],[item['ascent_m'] for item in recent])
        self.assertLessEqual(memory.memory_upper_bound(context,'chat'),800)
        # Missing current session must not turn history-only explanations off.
        speech='均速22公里/小时、爬升300米，可能与爬升增加有关。'
        self.assertTrue(brain._verified_text(brain._normalized_numbers(speech),numbers))
        self.assertTrue(brain._explanation_has_basis(speech,facts))
        facts,_=ride_chat._history_facts([{'avg_speed_mps':None,'ascent_m':None}],None,None)
        self.assertFalse(brain._explanation_has_basis(speech,facts))

    def test_previous_displays_differences_and_nulls(self):
        self.assertEqual(self.facts['display']['previous_avg_speed_kmh'],24)
        self.assertEqual(self.facts['display']['avg_speed_diff_kmh'],6)
        self.assertEqual(self.facts['display']['ascent_diff_m'],360)
        self.assertIn((Decimal(6),'km/h'),self.numbers)
        self.assertIn((Decimal(360),'m'),self.numbers)
        previous=copy.deepcopy(self.case['previous_summary']);previous['ascent_m']=None
        f,_=brain._facts(self.session,previous)
        self.assertNotIn('ascent_diff_m',f['display'])

    def test_grounded_explanation_passes_full_builder(self):
        text='模拟记录：爬升多360米、均速低6公里/小时，可能与爬升增加有关，不能只按均速评价表现。'
        result=brain.build_ride_review(self.session,self.case['previous_summary'],api_key='test-key',http_post=transport(text))
        self.assertEqual(result['llm_status'],'ok')

    def test_unsupported_reason_or_body_segment_claims_rejected(self):
        texts=[
            '模拟记录：均速18公里/小时，可能与逆风有关。',
            '模拟记录：均速低6公里/小时、爬升多360米，可能与速度变化有关。',
            '模拟记录：爬升多360米、均速低6公里/小时，可能与路况有关。',
            '模拟记录：爬升多360米、均速低6公里/小时，爬升影响了速度。',
            '模拟记录：均速18公里/小时，可能是爬坡导致。',
            '模拟记录：爬升多360米、均速低6公里/小时，因此爬升导致速度下降。',
            '模拟记录：心率144、均速18公里/小时，看起来你更轻松。',
            '模拟记录：时长54分钟、心率144，后半程应该是稳住了。',
            '模拟记录：心率144，心率正常。',
            '模拟记录：心率144，感觉有点累。',
            '模拟记录：心率144，你不累。',
            '模拟记录：时长54分钟、心率144，后半程更稳。',
        ]
        for text in texts:
            with self.subTest(text=text):
                result=brain.build_ride_review(self.session,self.case['previous_summary'],api_key='test-key',http_post=transport(text))
                self.assertNotEqual(result['llm_status'],'ok')

    def test_fabricated_number_keeps_precedence(self):
        result=brain.build_ride_review(self.session,self.case['previous_summary'],api_key='test-key',
            http_post=transport('模拟记录：爬升多999米、均速低6公里/小时，可能与爬升有关。'))
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')

    def test_new_cases_exclude_old_and_remain_synthetic(self):
        new=legacy.load_cases(preview.CORPUS);old=legacy.load_cases()
        self.assertEqual(len(new),10)
        self.assertFalse({c['case_id'] for c in new}&{c['case_id'] for c in old})
        self.assertTrue(all(c['summary']!=o['summary'] for c in new for o in old))
        self.assertTrue(all(legacy.session_for(c).record['source']=='simulated' for c in new))

    def test_presentation_only_strips_known_anchored_prefix(self):
        self.assertEqual(preview.presentation('模拟记录：均速18公里/小时。'),'均速18公里/小时。')
        self.assertEqual(preview.presentation('这是模拟记录：均速18公里/小时。'),'这是模拟记录：均速18公里/小时。')
        self.assertTrue(preview.length_matched('a'*50,'b'*40))
        self.assertTrue(preview.length_matched('a'*50,'b'*60))
        self.assertFalse(preview.length_matched('a'*50,'b'*61))
        self.assertFalse(preview.length_matched('a'*50,'b'*39))
        self.assertFalse(preview.explanation_style('心率下降，看起来在下降。'))
        self.assertFalse(preview.explanation_style('均速低4公里/小时，爬升160米，可能与速度变化有关。'))

    def test_bounded_attempts_preserved_and_never_release_even_when_mock_is_live(self):
        def post(request,*,timeout):
            raw={'choices':[{'message':{'content':json.dumps({'speech':'模拟记录：记录已完成。'})}}]}
            return 200,{},json.dumps(raw).encode()
        mock=Mock(side_effect=post)
        report=preview.generate(legacy.load_cases(preview.CORPUS),api_key='test-key',endpoint='https://invalid.test',post=mock,live=True)
        self.assertEqual(mock.call_count,40)
        self.assertEqual(report['real_model_calls'],40)
        for case in report['cases']:
            for variant in case['variants'].values():
                self.assertEqual(len(variant['attempts']),2)
                self.assertEqual(variant['selected_attempt'],2)
                self.assertTrue(variant['preview_failures'])
        self.assertFalse(report['participant_release_allowed'])
        self.assertFalse(report['blind_ready'])
        self.assertEqual(report['boss_approval'],'pending')
        self.assertFalse(report['mechanical_preview_checks_passed'])

    def test_prompt_removes_tautology_and_keeps_causal_limits(self):
        self.assertNotIn('看起来记录的心率在下降',brain.SYSTEM_PROMPT)
        self.assertNotIn('仍禁止推断趋势原因',brain.SYSTEM_PROMPT)
        self.assertIn('禁止无据推断原因',brain.SYSTEM_PROMPT)
        self.assertIn('解释型范例',brain.SYSTEM_PROMPT)
        self.assertIn('没有分段数据',brain.SYSTEM_PROMPT)

    def test_examples_obey_numeric_and_explanation_gates(self):
        for facts,speech in brain._REVIEW_EXAMPLES:
            numbers=[]
            for field in ('summary','previous_summary'):
                numbers.extend(brain._summary(facts[field])[1])
            for key,value in facts.get('differences',{}).items():
                unit={'duration_s':'s','avg_hr':'bpm','ascent_m':'m','avg_speed_mps':'m/s'}[key]
                numbers.append((abs(Decimal(str(value))),unit))
            # Production _facts() also authorizes its explicitly projected display values
            # (e.g. duration_min); mirror that contract rather than rejecting valid speech.
            display_units={
                'duration_min':'min','previous_duration_min':'min','duration_diff_min':'min',
                'avg_speed_kmh':'km/h','previous_avg_speed_kmh':'km/h','avg_speed_diff_kmh':'km/h',
                'ascent_m':'m','previous_ascent_m':'m','ascent_diff_m':'m',
            }
            for key,unit in display_units.items():
                if facts.get('display',{}).get(key) is not None:
                    numbers.append((Decimal(str(facts['display'][key])),unit))
            self.assertTrue(brain._verified_text(brain._normalized_numbers(speech),numbers),speech)
            self.assertTrue(brain._inference_has_basis(speech,numbers),speech)
            self.assertTrue(brain._explanation_has_basis(speech,facts),speech)

    def test_passing_preview_still_requires_boss_approval(self):
        from unittest.mock import patch
        def build(*args,**kwargs):
            explaining=brain.L3_INFERENCE_RULE in brain.SYSTEM_PROMPT
            text=('时长30分钟、心率130、均速20公里/小时、爬升100米，不能只按数字比较。' if explaining else
                  '记录时长30分钟、平均心率130、均速20公里/小时、累计爬升100米。')
            return {'llm_status':'ok','draft':{'speech':'模拟记录：'+text},'usage':{}}
        # Builder is mocked here: only test tooling gates, not a live success claim.
        with patch.object(brain,'build_ride_review',side_effect=build):
            r=preview.generate(legacy.load_cases(preview.CORPUS),api_key='test',endpoint='https://invalid.test',live=True)
        self.assertTrue(r['mechanical_preview_checks_passed'])
        self.assertEqual(r['real_model_calls'],20)
        self.assertFalse(r['participant_release_allowed'])
        self.assertFalse(r['blind_ready'])
        self.assertEqual(r['boss_approval'],'pending')
        self.assertTrue(all(len(v['attempts'])==1 for c in r['cases'] for v in c['variants'].values()))

    def test_real_role_examples_and_final_facts_separation(self):
        messages=brain._review_messages(self.facts)
        self.assertEqual(len(messages),22)  # system + ten user/assistant examples + final facts
        self.assertEqual(json.loads(messages[-1]['content']),self.facts)
        example_speeches=[]
        for i in range(2,21,2):
            payload=json.loads(messages[i]['content'])
            speech=payload['speech']
            example_speeches.append(speech)
            self.assertNotIn('模拟记录：',speech)
            self.assertTrue(preview.explanation_style(speech),speech)
        self.assertEqual(example_speeches[6], '均速低4公里/小时，爬升仍是160米，无法确认其他原因。')
        self.assertEqual(example_speeches[9], '这次比上次多爬180米、均速慢3公里/小时；坡多了，速度低些很正常。')
        self.assertLessEqual(sum('可能与' in text for text in example_speeches),4)
        # Every ordinary example lands in a permitted interpretation; #7 is the
        # explicitly approved honest negative control and deliberately stays unchanged.
        landing_markers=('很正常','不代表','不等于','不宜','不能','无法确认','先不猜','信号','下次')
        for index,speech in enumerate(example_speeches):
            if index != 6:
                self.assertTrue(any(marker in speech for marker in landing_markers),speech)
        from unittest.mock import patch
        with patch.object(brain,'SYSTEM_PROMPT',legacy.BASELINE.read_text(encoding='utf-8')):
            self.assertEqual(len(brain._review_messages(self.facts)),2)

if __name__=='__main__':unittest.main()
