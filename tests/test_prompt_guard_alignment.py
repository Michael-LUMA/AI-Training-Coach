"""Production prompt/guard alignment, fixed synthetic fixtures; no network."""
import json
import unittest

import ride_brain as brain
import ride_live as live
from memory_service import SummarySession
from tests.test_profile_service import record
from tests.test_ride_brain import transport


class PromptGuardAlignmentTests(unittest.TestCase):
    def test_every_review_few_shot_obeys_unchanged_hard_guard(self):
        for index, (_, speech) in enumerate(brain._REVIEW_EXAMPLES, 1):
            with self.subTest(example=index):
                self.assertIsNone(brain._FORBIDDEN_HARD.search(speech), speech)

    def test_equal_summary_review_is_accepted_without_invented_progress(self):
        result = brain.build_ride_review(
            SummarySession(record(1)), record(2)['summary'],
            api_key='synthetic-key',
            http_post=transport('这次和上次都是30分钟、均速12公里/小时，心率也都是130。'))
        self.assertEqual(result['llm_status'], 'ok')

    def test_effect_medical_and_subjective_red_lines_still_reject(self):
        for speech in ('你变强了。', '这次退步了。', '不是退步。', '不等于能力提升。',
                       '建议服药。', '心率130，你感觉更轻松。'):
            with self.subTest(speech=speech):
                result = brain.build_ride_review(
                    SummarySession(record(1)), record(2)['summary'],
                    api_key='synthetic-key', http_post=transport(speech))
                self.assertNotEqual(result['llm_status'], 'ok')
                self.assertIsNone(result['draft'])

    def live_call(self, speech):
        events = [{'type': 'speed_drop', 'current_speed_mps': 1,
                   'avg_speed_mps': 5.761904761904762, 'ratio': .17355371900826447}]
        facts = {'activity': 'ride', 'paused': False, 'distance_m': 3630, 'duration_s': 630,
                 'avg_speed_kmh': 20.742857142857144, 'previous': record(1)['summary']}
        post = transport(speech)
        result = live.build_live_speech(events, facts, api_key='synthetic-key', http_post=post)
        return result, json.loads(post.call_args.args[0].data)['messages'][0]['content']

    def test_current_event_baseline_and_unit_conversion_pass(self):
        result, _ = self.live_call('现在速度3.6公里/小时，比本次全程均速20.7公里/小时慢，原因还不清楚。')
        self.assertEqual(result['llm_status'], 'ok')

    def test_history_must_not_be_relabelled_current_baseline(self):
        result, _ = self.live_call('现在速度3.6公里/小时，本次全程均速12公里/小时。')
        self.assertNotEqual(result['llm_status'], 'ok')
        self.assertEqual(result['fallback'], 'template')

    def test_unrecorded_road_or_fatigue_cause_still_rejects(self):
        for cause in ('路况', '疲劳'):
            with self.subTest(cause=cause):
                result, _ = self.live_call('速度3.6公里/小时，可能是' + cause + '导致的。')
                self.assertNotEqual(result['llm_status'], 'ok')
                self.assertEqual(result['fallback'], 'template')

    def test_live_prompt_explicitly_separates_event_and_history(self):
        _, prompt = self.live_call('这段速度比全程均速慢了不少。')
        for constraint in ('current_speed_mps', 'events.avg_speed_mps', 'previous',
                           '公里/小时', '原因没记录就明确说不清楚'):
            self.assertIn(constraint, prompt)


    def heart_call(self, speech, *, low=False, extra_events=()):
        event = ({'type': 'hr_low', 'hr': 80, 'hr_lower': 90, 'duration_s': 630}
                 if low else {'type': 'hr_high', 'hr': 160, 'hr_upper': 150, 'duration_s': 630})
        facts = {'activity': 'ride', 'paused': False, 'distance_m': 3630,
                 'duration_s': 630, 'avg_speed_kmh': 20.742857142857144}
        return live.build_live_speech([event, *extra_events], facts, api_key='synthetic-key',
                                      http_post=transport(speech))

    def test_activity_duration_is_not_threshold_duration(self):
        # Actual once-only production response, plus equivalent numeral/clause forms.
        for speech in ('心率有点高，现在160了，已经超出上限150有十分钟了。',
                       '心率160，超出上限150已经10分钟了。',
                       '已经10分钟了，心率160高于上限150。',
                       '心率160超过150；持续630秒了。',
                       '心率160，高于上限150已经10 min。',
                       '心率160，已经一直高于上限150。'):
            with self.subTest(speech=speech):
                result = self.heart_call(speech)
                self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')
                self.assertEqual(result['fallback'], 'template')
                self.assertTrue(result['urgent'])
                self.assertNotEqual(result['speech'], speech)

    def test_low_threshold_duration_cannot_borrow_activity_time(self):
        result = self.heart_call('心率80，低于下限90有十分钟了。', low=True)
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')
        self.assertTrue(result['urgent'])

    def test_mixed_milestone_does_not_justify_threshold_duration(self):
        result = self.heart_call('心率160超过150已经10分钟了。',
                                 extra_events=[{'type': 'duration_milestone', 'minutes': 10,
                                                'distance_m': 3630}])
        self.assertEqual(result['llm_status'], 'rejected_unsupported_inference')

    def test_current_hr_and_explicit_total_activity_duration_remain_valid(self):
        for speech in ('现在心率160，高于上限150。',
                       '这次骑了10分钟，心率160高于上限150。',
                       '心率160，高于上限150；本次活动时长630秒。'):
            with self.subTest(speech=speech):
                self.assertEqual(self.heart_call(speech)['llm_status'], 'ok')
        self.assertEqual(self.heart_call('现在心率80，低于下限90。', low=True)['llm_status'], 'ok')

    def test_nonurgent_duration_milestone_is_not_restricted(self):
        result = live.build_live_speech(
            [{'type': 'duration_milestone', 'minutes': 10, 'distance_m': 3630}],
            {'activity': 'ride', 'paused': False, 'distance_m': 3630,
             'duration_s': 630, 'avg_speed_kmh': 20.742857142857144},
            api_key='synthetic-key', http_post=transport('已经骑了10分钟。'))
        self.assertEqual(result['llm_status'], 'ok')

    def test_hr_prompt_does_not_treat_duration_as_threshold_time(self):
        self.assertIn('duration_s 是整次活动时长，不是心率越限时长', live.SYSTEM_PROMPT)

    def test_equal_dimensions_must_not_claim_unobserved_stability(self):
        result = brain.build_ride_review(
            SummarySession(record(1)), record(2)['summary'], api_key='synthetic-key',
            http_post=transport('这次骑了30分钟，均速12公里/小时；数据和上次一样，保持得挺稳。'))
        self.assertNotEqual(result['llm_status'], 'ok')
        self.assertIsNone(result['draft'])


if __name__ == '__main__':
    unittest.main()
