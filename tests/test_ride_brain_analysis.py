"""Offline W3 first-segment derived facts and speech safety tests."""
import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import Mock

import ride_brain as brain


def memory(series=None):
    if series is None:
        series=[{'t':0,'hr':120},{'t':60,'hr':120}]
    hrs=[p['hr'] for p in series if 'hr' in p]
    powers=[p['pwr'] for p in series if 'pwr' in p]
    return {
        'schema_version':'luma-ride-memory/2','id':'analysis-current',
        'started_at':'2026-11-08T08:00:00Z','ended_at':'2026-11-08T08:01:00Z',
        'samples_hz':1,'series':series,
        'summary':{'duration_s':60,'avg_hr':sum(hrs)/len(hrs) if hrs else None,
                   'max_hr':max(hrs) if hrs else None,'time_in_zone_s':None,
                   'zone_target':None,'gaps':sum('gap' in p for p in series),
                   'alerts':0,'avg_cadence':None,
                   'avg_power':sum(powers)/len(powers) if powers else None,
                   'max_power':max(powers) if powers else None},
        'coach_notes':[],'persona_version':'ride-persona/14',
        'source':'ble_gatt_0x180d',
    }


def paired(front=(100,200), back=(120,180), count=20):
    return [{'t':i,'hr':(front if i<count//2 else back)[0],
             'pwr':(front if i<count//2 else back)[1]} for i in range(count)]


def transport(speech):
    body={'choices':[{'finish_reason':'stop','message':{
        'content':json.dumps({'speech':speech},ensure_ascii=False)}}]}
    return Mock(return_value=(200,{},json.dumps(body,ensure_ascii=False).encode('utf-8')))


class RideBrainAnalysisTests(unittest.TestCase):
    def zoned(self):
        session=memory()
        session['summary'].update(
            zone_target={'lower':110,'upper':130},
            time_in_zone_s={'below':9.9,'within':30.3,'above':19.8})
        return session

    def review(self, session, speech):
        return brain.build_ride_review(session,api_key='synthetic-key',
                                       http_post=transport(speech))

    def test_zone_percentages_half_up_and_raw_seconds_registered(self):
        session=self.zoned()
        before=copy.deepcopy(session)
        facts,numbers=brain._facts(session,None)
        self.assertEqual(facts['zone_share'],
                         {'below_pct':17,'within_pct':51,'above_pct':33})
        self.assertTrue(all(type(v) is int for v in facts['zone_share'].values()))
        for value in ('17','51','33'):
            self.assertIn((Decimal(value),'%'),numbers)
        for value in ('9.9','30.3','19.8'):
            self.assertIn((Decimal(value),'s'),numbers)
        self.assertEqual(session,before)

    def test_registered_percentages_pass_in_arabic_and_chinese(self):
        for speech in ('低于区间17%，区间内51%，高于区间33%。',
                       '区间内占百分之五十一。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.review(self.zoned(),speech)['llm_status'],'ok')

    def test_unregistered_percentage_rejects_even_if_value_is_duration(self):
        result=self.review(self.zoned(),'区间内占60%。')
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')
        self.assertIsNone(result['draft'])
        self.assertEqual(result['fallback'],'template')

    def test_zone_share_absent_without_target_or_times(self):
        for target in (None,{'lower':110,'upper':130}):
            session=memory()
            session['summary']['zone_target']=target
            facts,numbers=brain._facts(session,None)
            self.assertNotIn('zone_share',facts)
            self.assertFalse(any(unit=='%' for _,unit in numbers))

    def test_zone_denominator_is_duration_not_total_zone_time(self):
        session=self.zoned()
        session['summary']['time_in_zone_s']={'below':6,'within':12,'above':0}
        facts,_=brain._facts(session,None)
        self.assertEqual(facts['zone_share'],
                         {'below_pct':10,'within_pct':20,'above_pct':0})

    def test_zero_duration_omits_undefined_zone_share(self):
        session=memory([{'t':0,'hr':120}])
        session['ended_at']=session['started_at']
        session['summary'].update(duration_s=0,zone_target={'lower':110,'upper':130},
                                  time_in_zone_s={'below':0,'within':0,'above':0})
        facts,_=brain._facts(session,None)
        self.assertNotIn('zone_share',facts)

    def test_decoupling_matches_hand_calculation_and_is_pure(self):
        series=paired()
        before=copy.deepcopy(series)
        # Front: 2000/1000=2; back: 1800/1200=1.5; (2-1.5)/2*100=25.
        value=brain._decoupling_pct(series)
        self.assertIsInstance(value,Decimal)
        self.assertEqual(value,Decimal('25.0'))
        self.assertEqual(value.as_tuple().exponent,-1)
        self.assertEqual(series,before)
        facts,numbers=brain._facts(memory(series),None)
        self.assertIs(type(facts['pw_hr_decoupling_pct']),float)
        self.assertEqual(facts['pw_hr_decoupling_pct'],25.0)
        self.assertIn((Decimal('25.0'),'%'),numbers)
        self.assertEqual(self.review(memory(series),'功率心率比变化25%。')['llm_status'],'ok')

    def test_decoupling_uses_ratio_of_sums_not_mean_of_ratios(self):
        front=[{'t':i,'hr':100 if i%2==0 else 200,
                'pwr':100 if i%2==0 else 400} for i in range(10)]
        back=[{'t':i,'hr':100,'pwr':150} for i in range(10,20)]
        # (2500/1500 - 1500/1000)/(2500/1500)*100 = 10, not 0.
        self.assertEqual(brain._decoupling_pct(front+back),Decimal('10.0'))

    def test_decoupling_one_decimal_half_up_both_signs(self):
        for back,expected in ((391,'2.3'),(409,'-2.3')):
            with self.subTest(back=back):
                self.assertEqual(brain._decoupling_pct(paired((100,400),(100,back))),
                                 Decimal(expected))

    def test_negative_decoupling_and_absolute_value_registered(self):
        session=memory(paired((100,200),(100,250)))
        facts,numbers=brain._facts(session,None)
        self.assertEqual(facts['pw_hr_decoupling_pct'],-25.0)
        self.assertIn((Decimal('-25.0'),'%'),numbers)
        self.assertIn((Decimal('25.0'),'%'),numbers)
        for speech in ('功率心率比变化-25%。','功率心率比变化幅度25%。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.review(session,speech)['llm_status'],'ok')

    def test_zero_decoupling_is_not_omitted(self):
        facts,_=brain._facts(memory(paired((100,200),(100,200))),None)
        self.assertEqual(facts['pw_hr_decoupling_pct'],0.0)

    def test_insufficient_samples_omit_key(self):
        for count in (0,1,18,19):
            with self.subTest(count=count):
                series=paired(count=count)
                self.assertIsNone(brain._decoupling_pct(series))
                facts,numbers=brain._facts(memory(series),None)
                self.assertNotIn('pw_hr_decoupling_pct',facts)
                self.assertFalse(any(unit=='%' for _,unit in numbers))

    def test_only_paired_samples_count_and_gaps_are_ignored(self):
        series=paired(count=19)+[{'t':19,'hr':120},{'t':20,'gap':True}]
        self.assertIsNone(brain._decoupling_pct(series))
        facts,_=brain._facts(memory(series),None)
        self.assertNotIn('pw_hr_decoupling_pct',facts)
        complete=paired()+[{'t':20,'hr':120},{'t':21,'gap':True}]
        self.assertEqual(brain._decoupling_pct(complete),Decimal('25.0'))

    def test_odd_median_sample_belongs_to_back_half(self):
        series=paired(count=21)
        # Ten front and eleven back samples still have ratios 2 and 1.5.
        self.assertEqual(brain._decoupling_pct(series),Decimal('25.0'))

    def test_split_at_sample_time_median_not_elapsed_midpoint(self):
        series=paired()
        for i,point in enumerate(series):
            point['t']=i if i<10 else 50+(i-10)
        self.assertEqual(brain._decoupling_pct(series),Decimal('25.0'))

    def test_undefined_ratios_omit_key(self):
        for front,back in (((0,200),(120,180)),((100,200),(0,180)),
                           ((100,0),(120,180))):
            with self.subTest(front=front,back=back):
                series=paired(front,back)
                self.assertIsNone(brain._decoupling_pct(series))
                facts,_=brain._facts(memory(series),None)
                self.assertNotIn('pw_hr_decoupling_pct',facts)

    def test_no_power_no_decoupling_fact_or_user_payload_trace(self):
        session=memory([{'t':i,'hr':120} for i in range(20)])
        self.assertIsNone(brain._decoupling_pct(session['series']))
        facts,numbers=brain._facts(session,None)
        self.assertNotIn('pw_hr_decoupling_pct',facts)
        self.assertFalse(any(unit=='%' for _,unit in numbers))
        post=transport('这次平均心率120。')
        result=brain.build_ride_review(session,api_key='synthetic-key',http_post=post)
        self.assertEqual(result['llm_status'],'ok')
        payload=json.loads(post.call_args.args[0].data)
        self.assertNotIn('pw_hr_decoupling_pct',payload['messages'][1]['content'])

    def test_aerobic_evaluation_rejected_as_unsafe(self):
        result=self.review(memory(paired()),'有氧基础好。')
        self.assertEqual(result['llm_status'],'rejected_unsafe_speech')
        self.assertIsNone(result['draft'])
        self.assertEqual(result['fallback'],'template')

    def test_all_added_forbidden_phrases_rejected(self):
        for speech in ('有氧基础差','越来越轻松','越来越快','越来越强',
                       '进步明显','退步','疲劳积累','效率高','效率低','效率提升'):
            with self.subTest(speech=speech):
                self.assertEqual(self.review(memory(),speech)['llm_status'],
                                 'rejected_unsafe_speech')

    def test_prompt_declares_facts_not_training_evaluations(self):
        self.assertIn('zone_share 是心率区间时间占比（%）',brain.SYSTEM_PROMPT)
        self.assertIn('禁止解读为训练效果',brain.SYSTEM_PROMPT)
        self.assertIn('pw_hr_decoupling_pct 是前后半程功率心率比的变化百分比',
                      brain.SYSTEM_PROMPT)
        self.assertIn('禁止说『有氧基础好/差』『效率高/低』等任何评价',brain.SYSTEM_PROMPT)


if __name__=='__main__':
    unittest.main()

def summary(avg_hr=132, duration=2700):
    return {'duration_s':duration,'avg_hr':avg_hr,'max_hr':150,
            'time_in_zone_s':None,'zone_target':None,'gaps':0,'alerts':0,
            'avg_cadence':None,'avg_power':None,'max_power':None,
            'distance_m':None,'avg_speed_mps':None,'max_speed_mps':None}


class RideBrainHistoryFactsTests(unittest.TestCase):
    def review(self, session, speech, **kwargs):
        return brain.build_ride_review(session,api_key='synthetic-key',
                                       http_post=transport(speech),**kwargs)

    def test_trend_lists_values_in_order_and_registers_each(self):
        recents=[summary(avg_hr=138),summary(avg_hr=135),summary(avg_hr=132)]
        facts,numbers=brain._facts(memory(),None,recent_summaries=recents)
        trend=facts['recent_trend']
        self.assertEqual(trend,{'count':3,'avg_hr_list':[138,135,132],
                                'duration_min_list':[45,45,45]})
        for value in ('138','135','132'):
            self.assertIn((Decimal(value),'bpm'),numbers)
        self.assertIn((Decimal(3),None),numbers)
        result=self.review(memory(),'近三次平均心率138、135、132。',
                           recent_summaries=recents)
        self.assertEqual(result['llm_status'],'ok')

    def test_trend_null_avg_hr_is_preserved_not_filled(self):
        recents=[summary(avg_hr=138),summary(avg_hr=None)]
        facts,_=brain._facts(memory(),None,recent_summaries=recents)
        self.assertEqual(facts['recent_trend']['avg_hr_list'],[138,None])

    def test_trend_truncates_to_last_three(self):
        recents=[summary(avg_hr=v) for v in (150,145,140,135,130)]
        facts,numbers=brain._facts(memory(),None,recent_summaries=recents)
        self.assertEqual(facts['recent_trend']['avg_hr_list'],[140,135,130])
        self.assertEqual(facts['recent_trend']['count'],3)
        self.assertNotIn((Decimal(150),'bpm'),numbers)
        self.assertNotIn((Decimal(145),'bpm'),numbers)

    def test_trend_fabricated_value_rejected(self):
        recents=[summary(avg_hr=138),summary(avg_hr=135)]
        result=self.review(memory(),'上次平均心率129。',
                           recent_summaries=recents)
        self.assertEqual(result['llm_status'],'rejected_unverifiable_number')

    def test_ordinal_registered_and_speakable(self):
        facts,numbers=brain._facts(memory(),None,activity_ordinal=10)
        self.assertEqual(facts['session_ordinal'],10)
        self.assertIn((Decimal(10),None),numbers)
        result=self.review(memory(),'这是我陪你骑的第10次。',activity_ordinal=10)
        self.assertEqual(result['llm_status'],'ok')

    def test_ordinal_invalid_values_rejected(self):
        for bad in (0,-1,'x',10.5,100001):
            with self.subTest(bad=bad):
                result=self.review(memory(),'第1次。',activity_ordinal=bad)
                self.assertEqual(result['llm_status'],'rejected_bad_schema')

    def test_excluded_simulated_counted_only_when_positive(self):
        facts,numbers=brain._facts(memory(),None,excluded_simulated=2)
        self.assertEqual(facts['excluded_simulated'],2)
        self.assertIn((Decimal(2),None),numbers)
        for zero in (0,None):
            facts,_=brain._facts(memory(),None,excluded_simulated=zero)
            self.assertNotIn('excluded_simulated',facts)

    def test_defaults_keep_facts_byte_identical(self):
        session=self_zoned=memory()
        plain,_=brain._facts(copy.deepcopy(session),None)
        explicit,_=brain._facts(copy.deepcopy(session),None,recent_summaries=None,
                                activity_ordinal=None,excluded_simulated=None)
        self.assertEqual(json.dumps(plain,sort_keys=True,ensure_ascii=False),
                         json.dumps(explicit,sort_keys=True,ensure_ascii=False))
        for key in ('recent_trend','session_ordinal','excluded_simulated'):
            self.assertNotIn(key,plain)

    def test_prompt_declares_trend_ordinal_exclusion_rules(self):
        self.assertIn('recent_trend 是最近几次同场景活动的逐次数值',brain.SYSTEM_PROMPT)
        self.assertIn('禁止推断趋势原因',brain.SYSTEM_PROMPT)
        self.assertIn('禁止拔高成里程碑评价',brain.SYSTEM_PROMPT)
        self.assertIn('excluded_simulated 是被剔除的合成演示次数',brain.SYSTEM_PROMPT)


if __name__=='__main__':
    unittest.main()
