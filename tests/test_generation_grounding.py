"""Generation evidence presentation; synthetic inputs, no external services."""
import copy
import json
import unittest

import ride_brain as brain
import ride_live as live
from tests.test_ride_brain import transport
from tests.test_profile_service import record
from memory_service import SummarySession


class GenerationGroundingTests(unittest.TestCase):
    def setUp(self):
        self.facts = {'distance_m': 3630, 'duration_s': 630,
                      'avg_speed_kmh': 20.742857142857144,
                      'paused': False, 'activity': 'ride'}
        self.drop = {'type': 'speed_drop', 'current_speed_mps': 1,
                     'avg_speed_mps': 5.761904761904762, 'ratio': .17355371900826447}

    def payload(self, events, facts=None):
        post = transport('速度慢下来了。')
        result = live.build_live_speech(events, self.facts if facts is None else facts,
                                       api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        return json.loads(post.call_args.args[0].data)

    def test_speed_display_is_derived_from_event_with_exact_rounding(self):
        events = [self.drop]
        before = copy.deepcopy(events)
        payload = self.payload(events)
        final = json.loads(payload['messages'][-1]['content'])
        self.assertEqual(final['display_events'], [{
            'type': 'speed_drop', 'current_speed_kmh': 3.6,
            'ride_avg_speed_kmh': 20.7}])
        self.assertEqual(final['events'], events)
        self.assertEqual(events, before)

    def test_display_cannot_be_injected_or_borrowed_from_previous(self):
        facts = dict(self.facts, display_events=[{'current_speed_kmh': 999}],
                     previous={'duration_s': 1800, 'avg_speed_mps': 10})
        final = json.loads(self.payload([self.drop], facts)['messages'][-1]['content'])
        self.assertEqual(final['display_events'][0]['ride_avg_speed_kmh'], 20.7)
        self.assertNotIn('999', json.dumps(final))

    def test_drift_display_uses_event_reference_not_profile(self):
        event = {'type': 'hr_drift', 'current_hr': 140, 'expected_hr': 132,
                 'drift_bpm': 8, 'speed_mps': 5}
        final = json.loads(self.payload([event])['messages'][-1]['content'])
        self.assertEqual(final['display_events'], [{
            'type': 'hr_drift', 'current_hr': 140, 'reference_hr': 132,
            'drift_bpm': 8, 'speed_kmh': 18.0}])

    def test_threshold_display_does_not_present_total_time_as_exceedance(self):
        for kind, field, hr, limit in (('hr_high', 'hr_upper', 160, 150),
                                       ('hr_low', 'hr_lower', 80, 90)):
            event = {'type': kind, 'hr': hr, field: limit, 'duration_s': 630}
            with self.subTest(kind=kind):
                final = json.loads(self.payload([event])['messages'][-1]['content'])
                self.assertEqual(final['display_events'], [
                    {'type': kind, 'hr': hr, field: limit}])

    def test_every_live_example_passes_the_full_unchanged_verifier(self):
        payload = self.payload([self.drop])
        messages = payload['messages']
        self.assertGreaterEqual(len(messages), 8)
        for i in range(1, len(messages) - 1, 2):
            data = json.loads(messages[i]['content'])
            speech = json.loads(messages[i + 1]['content'])['speech']
            with self.subTest(example=i):
                response = live.build_live_speech(
                    data['events'], data['live_facts'], api_key='synthetic-key',
                    http_post=transport(speech))
                self.assertEqual(response['llm_status'], 'ok', speech)

    def test_display_does_not_authorize_bad_rounding_or_bad_inference(self):
        for speech in ('当前速度3.6公里/小时，本次均速21.0公里/小时。',
                       '速度3.6公里/小时，可能是你想休息了。'):
            with self.subTest(speech=speech):
                result = live.build_live_speech([self.drop], self.facts,
                    api_key='synthetic-key', http_post=transport(speech))
                self.assertNotEqual(result['llm_status'], 'ok')
                self.assertEqual(result['fallback'], 'template')

    def test_prompts_require_comparability_and_forbid_intent_guessing(self):
        for phrase in ('不猜用户意图', '不满足同句近数要求', '不得只报数'):
            self.assertIn(phrase, brain.SYSTEM_PROMPT)
        self.assertIn('display_events', live.SYSTEM_PROMPT)


    def comparative_call(self, speech):
        current = record(1, duration=3600, distance=18000, hr=142)
        current['summary']['ascent_m'] = 420
        previous = record(2, duration=3600, distance=25000, hr=142)
        previous['summary']['ascent_m'] = 140
        return brain.build_ride_review(SummarySession(current), previous['summary'],
            api_key='synthetic-key', http_post=transport(speech))

    def test_oral_climbing_label_keeps_numeric_two_dimension_basis(self):
        result = self.comparative_call(
            '多爬280米、均速慢7公里/小时，可能与坡多了有关；不能只凭均速判断能力。')
        self.assertEqual(result['llm_status'], 'ok')

    def test_two_numbers_still_cannot_prove_unrecorded_intent(self):
        for intent in ('你更注重爬坡', '你想休息', '你刻意控制速度'):
            with self.subTest(intent=intent):
                result = self.comparative_call(
                    '爬升多280米、均速慢7公里/小时，可能是' + intent + '。')
                self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')
                self.assertIsNone(result['draft'])

    def test_oral_alias_never_borrows_across_clause_or_wrong_unit(self):
        for speech in (
            '多爬280米、均速慢7公里/小时；可能与坡多了有关。',
            '多爬280分钟、均速慢7公里/小时，可能与坡多了有关。',
            '多爬280米，可能与坡多了有关。'):
            with self.subTest(speech=speech):
                self.assertNotEqual(self.comparative_call(speech)['llm_status'], 'ok')

    def test_v4_unsupported_normality_and_unbound_drift_still_fall_back(self):
        current = record(1, duration=3600, distance=18000, hr=146)
        previous = record(2, duration=1800, distance=9000, hr=138)
        result = brain.build_ride_review(SummarySession(current), previous['summary'],
            api_key='synthetic-key', http_post=transport(
                '这次骑了60分钟，心率高8，均速还是18公里/小时；时长翻倍，心率变化可能是正常的。'))
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')
        event = {'type': 'hr_drift', 'current_hr': 140, 'expected_hr': 132,
                 'drift_bpm': 8, 'speed_mps': 5}
        result = live.build_live_speech([event], self.facts, api_key='synthetic-key',
            http_post=transport('心率140，比相近速度时参考的132高8；可能是这次速度更快了。'))
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')
        self.assertEqual(result['fallback'], 'template')

    def test_recorded_equality_does_not_prove_conditions_or_performance(self):
        current = record(1, duration=1800, distance=6000, hr=130)
        previous = record(2, duration=1800, distance=6000, hr=130)
        for speech in (
            '这次和上次一样，30分钟、均速12公里/小时、心率130；条件相同，表现也一致。',
            '心率130，均速12公里/小时；条件相同但速度不同，无法确认其他原因。'):
            with self.subTest(speech=speech):
                result = brain.build_ride_review(SummarySession(current), previous['summary'],
                    api_key='synthetic-key', http_post=transport(speech))
                self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')
        result = brain.build_ride_review(SummarySession(current), previous['summary'],
            api_key='synthetic-key', http_post=transport(
                '这次和上次一样，30分钟、均速12公里/小时、心率130；已记录的数据相同。'))
        self.assertEqual(result['llm_status'], 'ok')

    def test_review_recency_instruction_is_trusted_not_mixed_into_facts(self):
        messages = brain._review_messages({'summary': {}})
        self.assertEqual(messages[-2]['role'], 'system')
        self.assertIn('分号', messages[-2]['content'])
        self.assertEqual(json.loads(messages[-1]['content']), {'summary': {}})


if __name__ == '__main__':
    unittest.main()
