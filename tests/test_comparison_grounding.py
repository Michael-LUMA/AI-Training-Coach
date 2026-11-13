"""Bounded current/previous comparison claims; synthetic transports only."""
import copy
import unittest

import ride_brain as brain
import ride_live as live
from memory_service import SummarySession
from tests.test_profile_service import record
from tests.test_ride_brain import transport


class ComparisonGroundingTests(unittest.TestCase):
    def setUp(self):
        self.current = record(1, duration=3600, distance=18000, hr=142)
        self.current['summary']['ascent_m'] = 420
        self.previous = record(2, duration=1800, distance=12500, hr=134)
        self.previous['summary']['ascent_m'] = 140

    def review(self, text, previous=True):
        post = transport(text)
        result = brain.build_ride_review(SummarySession(self.current),
            self.previous['summary'] if previous else None,
            api_key='synthetic-key', http_post=post)
        self.assertEqual(post.call_count, 1)  # No quality retries.
        return result

    def rejected(self, text, previous=True):
        result = self.review(text, previous)
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference', text)
        self.assertIsNone(result['draft'])
        self.assertEqual(result['fallback'], 'template')

    def test_inverted_direction_is_rejected_even_without_causal_wording(self):
        for text in ('少骑30分钟。', '心率低8。', '均速快7公里/小时。', '少爬280米。'):
            with self.subTest(text=text):
                self.rejected(text)

    def test_measured_totals_cannot_be_reused_as_deltas(self):
        for text in ('多骑60分钟。', '心率高142。', '均速慢18公里/小时。', '多爬420米。'):
            with self.subTest(text=text):
                self.rejected(text)

    def test_causal_chain_with_reversed_facts_falls_back(self):
        self.rejected('少爬280米、均速快7公里/小时，可能与坡少了有关；不能只凭均速判断能力。')
        self.rejected('多爬420米、均速慢18公里/小时，可能与坡多了有关；不能只凭均速判断能力。')

    def test_correct_delta_and_explanation_remain_accepted(self):
        for text in ('多骑30分钟、心率高8，可能与骑得更久有关；不能单凭心率判断能力。',
                     '多爬280米、均速慢7公里/小时，可能与坡多了有关；不能只凭均速判断能力。',
                     '时长增加30分钟，心率升高8，均速降低7公里/小时，爬升增加280米。'):
            with self.subTest(text=text):
                self.assertEqual(self.review(text)['llm_status'], 'ok', text)

    def test_swapped_current_previous_totals_are_rejected(self):
        for text in ('这次爬升140米，上次爬升420米。',
                     '上次均速18公里/小时，这次均速25公里/小时。',
                     '这次心率134，上次心率142。',
                     '本次骑了30分钟，上次骑了60分钟。'):
            with self.subTest(text=text):
                self.rejected(text)

    def test_current_previous_subjects_and_reverse_comparison_are_supported(self):
        for text in ('这次爬升420米，上次爬升140米。',
                     '上次均速25公里/小时，这次均速18公里/小时。',
                     '这次心率142，上次心率134。',
                     '本次骑了60分钟，上次骑了30分钟。',
                     '上次均速快7公里/小时，这次多爬280米。'):
            with self.subTest(text=text):
                self.assertEqual(self.review(text)['llm_status'], 'ok', text)
        self.rejected('上次均速慢7公里/小时。')

    def test_explicit_comparator_is_not_misread_as_previous_subject(self):
        for text in ('这次比上次多爬280米，均速比上次慢7公里/小时。',
                     '均速相比上次慢7公里/小时。', '均速较上次慢7公里/小时。'):
            with self.subTest(text=text):
                self.assertEqual(self.review(text)['llm_status'], 'ok', text)
        self.rejected('均速比上次快7公里/小时。')

    def test_false_equality_is_rejected_for_either_known_total(self):
        for text in ('均速仍是25公里/小时。', '均速还是18公里/小时。',
                     '心率同是142。', '爬升仍是140米。'):
            with self.subTest(text=text):
                self.rejected(text)

    def test_equal_records_accept_equality_but_not_signed_change(self):
        self.previous = copy.deepcopy(self.current)
        self.assertEqual(self.review('心率同是142、均速仍是18公里/小时。')['llm_status'], 'ok')
        self.rejected('心率高142。')

    def test_missing_comparable_record_cannot_borrow_current_numbers(self):
        self.rejected('多爬420米。', previous=False)
        self.rejected('上次爬升420米。', previous=False)
        self.previous['summary']['ascent_m'] = None
        self.rejected('多爬420米。')
        self.assertEqual(self.review('这次爬升420米。', previous=False)['llm_status'], 'ok')

    def test_chinese_numerals_unit_conversion_and_rounded_speed(self):
        for text in ('多爬二百八十米，均速慢七公里/小时。',
                     '多骑0.5小时，心率高8。', '均速慢1.94米/秒。'):
            with self.subTest(text=text):
                self.assertEqual(self.review(text)['llm_status'], 'ok', text)
        self.rejected('多骑3600秒。')
        self.assertNotEqual(self.review('心率高-8。')['llm_status'], 'ok')

    def test_history_only_chat_uses_latest_two_records_not_older_numbers(self):
        facts = {'recent_trend': {
            'duration_min_list': [60, 30, 60], 'avg_hr_list': [142, 134, 142],
            'avg_speed_kmh_list': [18, 25, 18], 'ascent_m_list': [420, 140, 420]}}
        self.assertTrue(brain._explanation_has_basis('多爬280米、均速慢7公里/小时。', facts))
        self.assertFalse(brain._explanation_has_basis('少爬280米、均速快7公里/小时。', facts))
        self.assertFalse(brain._explanation_has_basis('这次爬升140米，上次爬升420米。', facts))
        facts['recent_trend']['ascent_m_list'][-2] = None
        self.assertFalse(brain._explanation_has_basis('多爬420米。', facts))

    def test_summary_with_history_uses_latest_prior_record(self):
        facts = {'summary': {'ascent_m': 420},
                 'recent_trend': {'ascent_m_list': [420, 140]}}
        self.assertTrue(brain._explanation_has_basis('多爬280米。', facts))
        self.assertFalse(brain._explanation_has_basis('少爬280米。', facts))

    def test_non_comparative_history_list_is_not_forced_into_current_role(self):
        facts = {'recent_trend': {'avg_hr_list': [134, 142]}}
        self.assertTrue(brain._explanation_has_basis('近2次平均心率134、142。', facts))

    def test_original_numeric_and_proximity_guards_still_reject(self):
        for text in ('多爬280分钟、均速慢7公里/小时，可能与坡多了有关。',
                     '多爬280米、均速慢7公里/小时；可能与坡多了有关。'):
            with self.subTest(text=text):
                self.assertNotEqual(self.review(text)['llm_status'], 'ok')


class LiveEventRoleGroundingTests(unittest.TestCase):
    def setUp(self):
        self.facts = {'distance_m': 5625, 'duration_s': 900, 'avg_speed_kmh': 22.5,
                      'paused': False, 'activity': 'ride'}
        self.drop = {'type': 'speed_drop', 'current_speed_mps': 2,
                     'avg_speed_mps': 6.25, 'ratio': .32}
        self.drift = {'type': 'hr_drift', 'current_hr': 148, 'expected_hr': 140,
                      'drift_bpm': 8, 'speed_mps': 6.25}
        self.high = {'type': 'hr_high', 'hr': 165, 'hr_upper': 155, 'duration_s': 900}
        self.low = {'type': 'hr_low', 'hr': 95, 'hr_lower': 105, 'duration_s': 900}

    def build(self, event, speech):
        post = transport(speech)
        result = live.build_live_speech([event], self.facts,
            api_key='synthetic-key', http_post=post)
        self.assertEqual(post.call_count, 1)
        return result

    def rejected(self, event, speech):
        result = self.build(event, speech)
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference', speech)
        self.assertEqual(result['fallback'], 'template')
        # The response must expose only the existing deterministic template.
        self.assertNotEqual(result.get('speech'), speech)

    def test_swapped_speed_roles_are_rejected(self):
        self.rejected(self.drop,
            '当前速度22.5公里/小时，比本次均速7.2公里/小时慢，原因还不清楚。')

    def test_correct_speed_roles_and_mps_units_are_accepted(self):
        for speech in ('当前速度7.2公里/小时，比本次均速22.5公里/小时慢，原因还不清楚。',
                       '当前速度2米/秒，本次均速6.25米/秒。',
                       '当前速度七点二公里/小时，均速二十二点五公里/小时。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.build(self.drop, speech)['llm_status'], 'ok')

    def test_correct_speed_values_cannot_claim_opposite_relation(self):
        self.rejected(self.drop, '当前速度7.2公里/小时，比本次均速22.5公里/小时快。')

    def test_swapped_drift_current_reference_are_rejected(self):
        self.rejected(self.drift, '心率140，比相近速度时参考的148高8；原因还不清楚。')
        self.rejected(self.drift, '当前心率140，参考心率148。')

    def test_correct_drift_reference_and_delta_remain_accepted(self):
        for speech in ('心率148，比相近速度时参考的140高8；原因还不清楚。',
                       '当前心率148，参考心率140。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.build(self.drift, speech)['llm_status'], 'ok')

    def test_drift_total_cannot_be_reused_as_delta_or_reversed(self):
        self.rejected(self.drift, '心率148，比相近速度时参考的140高148。')
        self.rejected(self.drift, '心率148，比相近速度时参考的140低8。')

    def test_swapped_high_heart_rate_and_limit_are_rejected(self):
        self.rejected(self.high, '现在心率155，高于你设的上限165。')
        self.rejected(self.high, '现在心率165，高于你设的上限165。')

    def test_swapped_low_heart_rate_and_limit_are_rejected(self):
        self.rejected(self.low, '现在心率105，低于你设的下限95。')

    def test_correct_safety_roles_keep_urgent_contract(self):
        for event, speech in ((self.high, '现在心率165，高于你设的上限155。'),
                              (self.low, '现在心率95，低于你设的下限105。')):
            with self.subTest(event=event['type']):
                result = self.build(event, speech)
                self.assertEqual(result['llm_status'], 'ok')
                self.assertTrue(result['urgent'])

    def test_safety_direction_must_match_measured_limit(self):
        self.rejected(self.high, '现在心率165，低于你设的上限155。')
        self.rejected(self.low, '现在心率95，高于你设的下限105。')

    def test_missing_history_cannot_disable_event_role_checks(self):
        for speech in ('上次心率155，现在心率165。', '上次现在心率155。',
                       '平时心率155，现在心率165。'):
            with self.subTest(speech=speech):
                result = self.build(self.high, speech)
                self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')
                self.assertEqual(result['fallback'], 'template')

    def test_history_clause_keeps_its_own_provenance_guard(self):
        facts = dict(self.facts, previous={'avg_hr': 130})
        good = live.build_live_speech([self.high], facts, api_key='synthetic-key',
            http_post=transport('现在心率165，上次心率130。'))
        self.assertEqual(good['llm_status'], 'ok')
        bad = live.build_live_speech([self.high], facts, api_key='synthetic-key',
            http_post=transport('现在心率130，上次心率165。'))
        self.assertNotEqual(bad['llm_status'], 'ok')


class LiveAnswerRoleGroundingTests(unittest.TestCase):
    def setUp(self):
        self.facts = {'speed_mps': 2, 'avg_speed_kmh': 22.5,
                      'hr': 165, 'hr_upper': 155, 'hr_lower': 95, 'activity': 'ride'}

    def answer(self, speech, facts=None, history=None):
        post = transport(speech)
        result = live.build_live_answer('现在情况怎么样', self.facts if facts is None else facts,
            history=history, api_key='synthetic-key', http_post=post)
        self.assertEqual(post.call_count, 1)
        return result

    def rejected(self, speech, facts=None, history=None):
        result = self.answer(speech, facts, history)
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference', speech)
        self.assertEqual(result['fallback'], 'template')
        self.assertEqual(result['speech'], live.LIVE_TEMPLATE)

    def test_current_and_average_speed_cannot_be_swapped(self):
        self.rejected('当前速度22.5公里/小时，均速7.2公里/小时。')

    def test_current_and_upper_limit_cannot_be_swapped(self):
        self.rejected('现在心率155，上限165。')

    def test_current_and_lower_limit_cannot_be_swapped(self):
        self.rejected('现在心率95，下限165。')

    def test_correct_roles_and_unit_conversion_still_pass(self):
        for speech in ('当前速度7.2公里/小时，均速22.5公里/小时。',
                       '当前速度2米/秒，均速6.25米/秒。',
                       '当前速度七点二公里/小时，均速二十二点五公里/小时。',
                       '现在心率165，上限155，下限95。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.answer(speech)['llm_status'], 'ok')

    def test_correct_speed_values_cannot_reverse_relation(self):
        self.rejected('当前速度7.2公里/小时，比均速22.5公里/小时快。')
        self.assertEqual(self.answer('当前速度7.2公里/小时，比均速22.5公里/小时慢。')['llm_status'], 'ok')

    def test_limit_relation_must_match_current_reading(self):
        self.rejected('现在心率165，低于上限155。')
        self.rejected('现在心率165，低于下限95。')
        self.assertEqual(self.answer('现在心率165，高于上限155。')['llm_status'], 'ok')

    def test_average_does_not_fill_missing_current_speed(self):
        facts = dict(self.facts)
        del facts['speed_mps']
        self.rejected('当前速度22.5公里/小时。', facts)
        self.assertEqual(self.answer('均速22.5公里/小时。', facts)['llm_status'], 'ok')

    def test_current_reading_does_not_fill_missing_limit(self):
        facts = dict(self.facts)
        del facts['hr_upper']
        self.rejected('上限165。', facts)
        self.rejected('现在心率165，高于上限。', facts)

    def test_dialog_does_not_establish_historical_measurement(self):
        self.rejected('上次心率155，现在心率165。', history=[
            {'q': '上次我的心率是155吗？', 'a': '还没有确认。'}])

    def test_reference_cannot_borrow_current_or_limit_value(self):
        self.rejected('心率165，比参考心率155高。')

    def test_equal_speed_cannot_claim_direction(self):
        facts = dict(self.facts, speed_mps=6.25)
        self.rejected('当前速度22.5公里/小时，比均速22.5公里/小时快。', facts)



if __name__ == '__main__':
    unittest.main()
