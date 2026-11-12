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
        self.assertTrue(brain._inference_has_basis('看起来速度低.4', [(Decimal('.4'), 'm/s')]))
        self.assertFalse(brain._inference_has_basis('心率132.看起来不错', self.numbers))

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


class MemoryWindowTests(unittest.TestCase):
    def test_orchestrator_explicitly_requests_five_rounds_one_hour(self):
        from unittest.mock import patch
        from tests.test_dialog_store import NOW
        import dialog_store
        import memory_service
        with patch.object(memory_service, '_records', return_value=[]), \
             patch.object(dialog_store, 'get_recent', return_value=None) as get:
            memory_service.get_context_for_chat('synthetic-user', '这次呢', now=NOW)
        self.assertEqual(get.call_args.args, ('synthetic-user',))
        self.assertEqual(get.call_args.kwargs['n'], 5)
        self.assertEqual(get.call_args.kwargs['within_hours'], 1)

    def test_anonymous_retains_three_rounds_thirty_minutes(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import coach_server
        app = coach_server.App(store=SimpleNamespace(path=None))
        result = {'speech_status': 'ok', 'speech': '已记下'}
        with patch.object(coach_server.time, 'monotonic', return_value=100):
            for i in range(6):
                app._dialog_record('synthetic-conv', str(i), result)
        with patch.object(coach_server.time, 'monotonic', return_value=1899):
            self.assertEqual([row['q'] for row in app._dialog_read('synthetic-conv')], ['3','4','5'])
        with patch.object(coach_server.time, 'monotonic', return_value=3699):
            self.assertIsNone(app._dialog_read('synthetic-conv'))

    def test_five_context_rounds_cannot_authorize_fabricated_numbers(self):
        import ride_chat
        history = [{'q': '心率999?', 'a': '999'} for _ in range(5)]
        result = ride_chat.build_ride_chat('最近心率呢', memory(), history=history,
                 api_key='synthetic-key', http_post=transport('心率999。'))
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')


class CompleteL3Tests(unittest.TestCase):
    def test_all_prompts_share_l3_constraints(self):
        import ride_chat, ride_live
        for prompt in (brain.SYSTEM_PROMPT, ride_chat.SYSTEM_PROMPT,
                       ride_live.SYSTEM_PROMPT, ride_live.LIVE_SYSTEM_PROMPT):
            for rule in ('同句前后15字', '不确定性标记', '训练效果断言', '没有记录'):
                self.assertIn(rule, prompt)
        self.assertIn('最多200字', brain.SYSTEM_PROMPT)
        self.assertIn('最多100字', ride_live.SYSTEM_PROMPT)

    def test_valid_unit_conversion_is_numeric_basis(self):
        self.assertTrue(brain._inference_has_basis('看起来不错，记录0.5小时', [(Decimal(1800), 's')]))
        self.assertFalse(brain._inference_has_basis('看起来不错，记录0.5公里', [(Decimal(1800), 's')]))

    def test_review_length_limit_is_enforced_at_200(self):
        for size, status in ((200, 'ok'), (201, 'rejected_bad_schema')):
            result = brain.build_ride_review(memory(), api_key='synthetic-key',
                     http_post=transport('记录' + '啊'*(size-2)))
            self.assertEqual(result['llm_status'], status)

    def test_live_length_limit_is_enforced_at_100(self):
        import ride_live
        from tests.test_ride_live import milestone_event, live_facts, transport as live_post
        for size, status in ((81, 'ok'), (100, 'ok'), (101, 'rejected_bad_schema')):
            result = ride_live.build_live_speech(milestone_event(), live_facts(),
                     api_key='synthetic-key', http_post=live_post('记录' + '啊'*(size-2)))
            self.assertEqual(result['llm_status'], status)

    def test_chat_cannot_bypass_inference_guard(self):
        import ride_chat
        for text, status in [('心率120，看起来在下降。', 'ok'),
                             ('看起来你状态不错。', 'rejected_unsupported_inference'),
                             ('心率120，看起来变强了。', 'rejected_unsafe_speech')]:
            with self.subTest(text=text):
                result = ride_chat.build_ride_chat('这次怎么样', memory(),
                         api_key='synthetic-key', http_post=transport(text))
                self.assertEqual(result['llm_status'], status)

    def test_live_answer_cannot_bypass_inference_guard(self):
        import ride_live
        from tests.test_ride_live import live_facts, transport as live_post
        for text, status in [('看起来你状态不错。', 'rejected_unsupported_inference'),
                             ('看起来已骑5公里。', 'ok')]:
            result = ride_live.build_live_answer('这次如何', live_facts(),
                     api_key='synthetic-key', http_post=live_post(text))
            self.assertEqual(result['llm_status'], status)


class LiveGenerationRegressionTests(unittest.TestCase):
    def test_qualitative_one_compounds_are_not_numeric_claims(self):
        for text in ('心率120，和上次一样。', '心率120，比上次低了一些。'):
            result = brain.build_ride_review(memory(), api_key='synthetic-key',
                                             http_post=transport(text))
            self.assertEqual(result['llm_status'], 'ok')
        self.assertFalse(brain._inference_has_basis('和上次一样，看起来很轻松', [(Decimal(1), None)]))
        self.assertFalse(brain._inference_has_basis('看起来轻松了一些', [(Decimal(1), None)]))

    def test_real_count_one_still_requires_evidence(self):
        normalized = brain._normalized_numbers('骑了一次，一公里')
        self.assertIn('1', normalized)
        self.assertFalse(brain._verified_text(normalized, [(Decimal(135), 'bpm')]))


class LexicalShiRegressionTests(unittest.TestCase):
    def test_question_word_is_not_a_ten_claim(self):
        self.assertTrue(brain._verified_text(brain._normalized_numbers('心率120，没什么变化。'), [(Decimal(120), 'bpm')]))
        self.assertFalse(brain._verified_text(brain._normalized_numbers('骑了什公里'), [(Decimal(120), 'bpm')]))
        self.assertFalse(brain._inference_has_basis('看起来没什么变化', [(Decimal(10), None)]))


class CompoundSpeedUnitRegressionTests(unittest.TestCase):
    def test_compound_speed_units_preserve_dimension(self):
        numbers = [(Decimal(6000), 'm'), (Decimal(1800), 's'), (Decimal(12), 'km/h')]
        for text in ('骑了6公里，平均速度12公里/小时。', '平均速度12千米/小时。', '速度3.33米/秒。'):
            self.assertTrue(brain._verified_text(brain._normalized_numbers(text), numbers), text)
        for text in ('速度6公里/小时。', '速度12米/秒。', '骑了12公里。'):
            self.assertFalse(brain._verified_text(brain._normalized_numbers(text), numbers), text)

    def test_full_review_accepts_recorded_speed(self):
        from tests.test_profile_service import record
        from memory_service import SummarySession
        session = SummarySession(record(1, source='simulated'))
        result = brain.build_ride_review(session, None, recent_summaries=[],
                 api_key='synthetic-key', http_post=transport('模拟记录：时长30分钟，骑了6公里，平均速度12公里/小时。'))
        self.assertEqual(result['llm_status'], 'ok')

    def test_nearby_speed_basis_uses_complete_compound_unit(self):
        self.assertTrue(brain._inference_has_basis('速度12公里/小时，看起来速度相近。', [(Decimal(12), 'km/h')]))
        self.assertFalse(brain._inference_has_basis('速度12公里/小时，看起来速度相近。', [(Decimal(12000), 'm')]))
