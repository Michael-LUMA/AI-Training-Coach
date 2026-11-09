"""Offline general-lane tests (synthetic data, no network)."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ride_general as gen

PREFIX = gen.GENERAL_PREFIX
OK_SPEECH = PREFIX + '金字塔是古埃及法老的陵墓群，多在尼罗河西岸。'


def transport(speech=OK_SPEECH, *, status=200, draft=None, finish='stop'):
    envelope = {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(
        {'speech': speech} if draft is None else draft, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 12}}
    return Mock(return_value=(status, {}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


class GeneralAnswerTests(unittest.TestCase):
    def answer(self, speech=OK_SPEECH, question='聊聊古埃及金字塔', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return gen.build_general_answer(question, http_post=transport(speech), **kwargs)

    def test_labeled_number_free_answer_passes(self):
        result = self.answer()
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['draft'], {'speech': OK_SPEECH})
        self.assertIsNone(result['fallback'])

    def test_invalid_question_rejected_without_upstream(self):
        post = Mock(side_effect=AssertionError('no network'))
        for question in ('', '   ', '问' * 201, '问\x00', None, 42, ['问']):
            with self.subTest(question=repr(question)):
                result = gen.build_general_answer(question, api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_not_configured_never_calls_transport(self):
        post = Mock(side_effect=AssertionError('no network'))
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'must-not-read'}):
            for key in (None, '', '   '):
                result = gen.build_general_answer('聊聊古埃及金字塔', api_key=key, http_post=post)
                self.assertEqual(result['llm_status'], 'not_configured')
        post.assert_not_called()

    def test_upstream_failures_map_to_fallback(self):
        for post in (Mock(return_value=(500, {}, b'{}')),
                     Mock(return_value=(200, {}, b'not json')),
                     Mock(return_value=(200, {}, json.dumps({'error': {'message': 'x'}}).encode())),
                     Mock(side_effect=TimeoutError())):
            with self.subTest(post=post):
                result = gen.build_general_answer('聊聊古埃及金字塔',
                                                  api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], 'upstream_error')
                self.assertEqual(result['fallback'], 'template')

    def test_missing_label_prefix_rejected(self):
        result = self.answer('金字塔是古埃及法老的陵墓群。')
        self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        self.assertIsNone(result['draft'])

    def test_any_number_rejected_arabic_and_han(self):
        for speech in (PREFIX + '金字塔建于公元前2560年。',
                       PREFIX + '金字塔建于公元前两千多年。',
                       PREFIX + '胡夫金字塔高约一百多米。',
                       PREFIX + '它用了二百三十万块石头。'):
            with self.subTest(speech=speech):
                result = self.answer(speech)
                self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_formal_numeral_glyphs_in_common_words_pass(self):
        result = self.answer(PREFIX + '油漆未干别碰，这话可以参考。')
        self.assertEqual(result['llm_status'], 'ok')

    def test_forbidden_words_rejected(self):
        result = self.answer(PREFIX + '坚持训练效果明显，你会变强。')
        self.assertEqual(result['llm_status'], 'rejected_unsafe_speech')

    def test_malformed_drafts_rejected(self):
        for draft in ({}, {'speech': ''}, {'speech': '   '}, {'speech': '好' * 301},
                      {'speech': '行', 'extra': 1}, ['speech'], 'raw'):
            with self.subTest(draft=repr(draft)[:30]):
                result = gen.build_general_answer('聊聊古埃及金字塔', api_key='synthetic-key',
                                                  http_post=transport(draft=draft))
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')

    def test_history_projects_into_facts(self):
        post = transport()
        history = [{'q': '聊聊古埃及金字塔', 'a': OK_SPEECH}]
        result = gen.build_general_answer('它们为什么建在西岸？', history=history,
                                          api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        facts = json.loads(json.loads(post.call_args.args[0].data)['messages'][1]['content'])
        self.assertEqual(facts['recent_dialog'], history)
        self.assertNotIn(b'synthetic-key', post.call_args.args[0].data)

    def test_invalid_history_rejected_without_upstream(self):
        post = Mock(side_effect=AssertionError('no network'))
        for history in ([{'q': '问', 'a': '答'}] * 4, [{'q': '问'}], '问', 42):
            with self.subTest(history=repr(history)[:40]):
                result = gen.build_general_answer('聊聊古埃及金字塔', history=history,
                                                  api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_number_free_gate(self):
        self.assertTrue(gen._number_free('没有数字的句子，参考油漆工艺。'))
        self.assertFalse(gen._number_free('公元前2560年'))
        self.assertFalse(gen._number_free('两千多年'))
        self.assertFalse(gen._number_free('心率一百二'))
        self.assertFalse(gen._number_free('第①座'))
