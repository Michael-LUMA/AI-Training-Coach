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


if __name__ == '__main__':
    unittest.main()
