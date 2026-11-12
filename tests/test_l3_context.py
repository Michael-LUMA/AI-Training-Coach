"""L3 numeric gates: synthetic fixtures, no network or user data."""
from decimal import Decimal
import unittest

import ride_brain as brain
from tests.test_ride_brain import memory, transport


class ForbiddenTierTests(unittest.TestCase):
    def test_every_old_rule_stays_hard_or_soft(self):
        phrases = ('变强', '状态提升', '能力提升', '训练效果', '更健康',
                   '心肺功能提高', '心肺功能改善', '建议减速', '应该加速',
                   '务必降低心率', '请加大强度', '建议服药', '请吃药',
                   '继续保持强度', '继续保持心率', '燃脂效果', '减脂效果',
                   '诊断', '治疗', '有氧基础', '越来越轻松', '越来越快',
                   '越来越强', '进步明显', '退步', '疲劳积累', '效率高',
                   '效率低', '效率提升', '感觉轻松', '挺吃力', '很累',
                   '非常稳', '相当轻松', '强度不大', '强度大', '强度高',
                   '强度低', '挺好的一次')
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(brain._FORBIDDEN.search(phrase))
                self.assertTrue(brain._FORBIDDEN_HARD.search(phrase)
                                or brain._FORBIDDEN_SOFT.search(phrase))

    def test_hard_rules_never_relaxed_by_numbers(self):
        for phrase in ('变强', '状态提升', '能力提升', '训练效果', '更健康',
                       '心肺功能提高', '心肺功能改善', '建议减速', '应该加速',
                       '务必降低心率', '请加大强度', '建议服药', '请吃药',
                       '继续保持强度', '继续保持心率', '燃脂效果', '减脂效果',
                       '诊断', '治疗', '越来越轻松', '越来越快', '越来越强',
                       '进步明显', '退步', '疲劳积累'):
            with self.subTest(phrase=phrase):
                result = brain.build_ride_review(memory(), api_key='synthetic-key',
                           http_post=transport('平均心率120，' + phrase))
                self.assertEqual(result['llm_status'], 'rejected_unsafe_speech')

    def test_unsupported_soft_rules_rejected(self):
        for text in ('你轻松了', '很吃力', '很累', '很稳', '强度低', '效率高',
                     '有氧基础差', '挺好的一次'):
            with self.subTest(text=text):
                result = brain.build_ride_review(memory(), api_key='synthetic-key',
                                                 http_post=transport(text))
                self.assertEqual(result['llm_status'], 'rejected_unsafe_speech')

    def test_soft_requires_same_sentence_registered_number(self):
        numbers = [(Decimal(4), 'bpm')]
        for text, expected in [('比上次轻松点，心率低了4', True),
                               ('你轻松了', False), ('心率4。很轻松', False),
                               ('心率4；很轻松', False), ('心率4!很轻松', False),
                               ('很轻松，心率999', False), ('心率四，看起来很轻松', True),
                               ('很轻松' + '啊'*16 + '4', False),
                               ('很轻松' + '啊'*15 + '4', True),
                               ('很轻松，心率140', False)]:
            with self.subTest(text=text):
                self.assertEqual(brain._soft_has_basis(text, numbers), expected)


if __name__ == '__main__':
    unittest.main()


class InferenceBasisTests(unittest.TestCase):
    numbers = [(Decimal(4), 'bpm'), (Decimal('1.2'), 'km/h'),
               (Decimal(138), 'bpm'), (Decimal(135), 'bpm'), (Decimal(132), 'bpm')]

    def test_grounded_examples(self):
        for text in ('看起来强度在降，心率低了4、速度慢了1.2',
                     '近三次平均心率138、135、132，看起来在往下走',
                     '心率四，看起来很轻松', '比上次轻松点，心率低了4',
                     '相比上次，心率低了4', '似乎强度低，心率132',
                     '像是强度低，心率132', '可能是强度低，心率132'):
            with self.subTest(text=text):
                self.assertTrue(brain._inference_has_basis(text, self.numbers))

    def test_unsupported_examples(self):
        for text in ('看起来你状态不错', '似乎状态不错', '可能是状态不错',
                     '像是状态不错', '平均心率132。看起来不错',
                     '平均心率132；看起来不错', '心率132!看起来不错',
                     '心率132?看起来不错', '看起来不错，999',
                     '看起来不错，140', '看起来'+'啊'*16+'132',
                     '强度在降，心率132', '很轻松，心率132',
                     '平均心率132。似乎不错，平均心率999',
                     '心率132，看起来不错。似乎也不错'):
            with self.subTest(text=text):
                self.assertFalse(brain._inference_has_basis(text, self.numbers))

    def test_decimal_does_not_end_sentence(self):
        self.assertTrue(brain._inference_has_basis('速度慢1.2，看起来在下降', self.numbers))

    def test_plain_facts_unchanged(self):
        self.assertTrue(brain._inference_has_basis('心率132，心率135。', self.numbers))

    def test_review_enforces_inference_and_keeps_numeric_precedence(self):
        for text, expected in [('心率120，看起来在往下走', 'ok'),
                               ('看起来你状态不错', 'rejected_unsupported_inference'),
                               ('看起来心率999', 'rejected_unverifiable_number'),
                               ('心率120，看起来你变强了', 'rejected_unsafe_speech')]:
            with self.subTest(text=text):
                result = brain.build_ride_review(memory(), api_key='synthetic-key',
                                                 http_post=transport(text))
                self.assertEqual(result['llm_status'], expected)
                if expected != 'ok':
                    self.assertIsNone(result['draft'])

    def test_live_uses_same_gate_and_deterministic_fallback(self):
        import ride_live
        from tests.test_ride_live import milestone_event, live_facts, transport as live_post
        for text, expected in [('看起来不错，已经骑了5公里。', 'ok'),
                               ('看起来状态不错。', 'rejected_unsupported_inference'),
                               ('看起来骑了999公里。', 'rejected_unverifiable_number'),
                               ('骑了5公里，看起来变强了。', 'rejected_unsafe_speech')]:
            with self.subTest(text=text):
                result = ride_live.build_live_speech(milestone_event(), live_facts(),
                         api_key='synthetic-key', http_post=live_post(text))
                self.assertEqual(result['llm_status'], expected)
                if expected != 'ok':
                    self.assertNotEqual(result['speech'], text)


class TrendPromptTests(unittest.TestCase):
    def test_grounded_trend_passes_but_effect_claim_does_not(self):
        recents = [{'avg_hr': n} for n in (138, 135, 132)]
        for speech, expected in [
            ('近三次平均心率138、135、132，看起来在往下走。', 'ok'),
            ('近三次平均心率138、135、132，你心肺功能在改善。', 'rejected_unsafe_speech'),
            ('近三次平均心率138、135、999，看起来在往下走。', 'rejected_unverifiable_number')]:
            with self.subTest(speech=speech):
                result = brain.build_ride_review(memory(), recent_summaries=recents,
                         api_key='synthetic-key', http_post=transport(speech))
                self.assertEqual(result['llm_status'], expected)

    def test_trend_prompts_keep_uncertainty_and_causality_boundary(self):
        import ride_live
        for prompt in (brain.SYSTEM_PROMPT, ride_live.SYSTEM_PROMPT):
            self.assertIn('有据趋势判断', prompt)
            self.assertIn('不确定性标记', prompt)
            self.assertIn('15字', prompt)
