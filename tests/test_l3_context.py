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
