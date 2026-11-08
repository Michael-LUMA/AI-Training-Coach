"""Offline knowledge-brain tests (synthetic data, no network)."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ride_knowledge as know

CARDS_PATH = Path(__file__).resolve().parents[1] / 'knowledge' / 'cards_zh_v1.json'


def transport(speech='踏频是曲柄每分钟转动的圈数，和车速不是一回事。（来源：通用运动常识）',
              *, status=200, draft=None, finish='stop'):
    envelope = {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(
        {'speech': speech} if draft is None else draft, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 12}}
    return Mock(return_value=(status, {}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


class KnowledgeCardLibraryTests(unittest.TestCase):
    def test_shipped_library_loads_eighty_valid_cards(self):
        cards = know.load_cards(CARDS_PATH)
        self.assertEqual(len(cards), 80)
        self.assertEqual(len({card['id'] for card in cards}), 80)

    def test_load_cards_rejects_bad_files(self):
        import tempfile
        cases = ('not json', '{}', '[{"id":"x"}]',
                 json.dumps([{'id': 'a', 'title': 't', 'tags': ['一', '二', '三'],
                              'body': 'x' * 60, 'source': 's'},
                             {'id': 'a', 'title': 't', 'tags': ['一', '二', '三'],
                              'body': 'y' * 60, 'source': 's'}], ensure_ascii=False),
                 json.dumps([{'id': 'a', 'title': 't', 'tags': ['一', '二', '三'],
                              'body': 'x' * 60, 'source': 'https://evil.invalid'}], ensure_ascii=False))
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
                    handle.write(content)
                self.addCleanup(Path(handle.name).unlink)
                with self.assertRaises(ValueError):
                    know.load_cards(handle.name)


class KnowledgeRetrieveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cards = know.load_cards(CARDS_PATH)

    def test_retrieve_hits_and_misses(self):
        self.assertEqual(retrieve_ids('踏频是什么？', self.cards)[0], 'cadence-basics')
        self.assertEqual(retrieve_ids('量子力学', self.cards), [])

    def test_retrieve_covers_new_domains(self):
        expected = {
            '岔气怎么办': 'run-side-stitch',
            '登山杖有用吗': 'hike-poles',
            '跑后要不要拉伸': 'recovery-stretch',
            '能量胶什么时候吃': 'gear-energy-gel',
            '夜跑怎么更安全': 'gear-reflective',
        }
        for question, top_id in expected.items():
            with self.subTest(question=question):
                self.assertEqual(retrieve_ids(question, self.cards)[0], top_id)

    def test_retrieve_is_deterministic(self):
        first = retrieve_ids('骑行怎么补水？', self.cards)
        second = retrieve_ids('骑行怎么补水？', self.cards)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 2)

    def test_retrieve_validates_question(self):
        with self.assertRaises(ValueError):
            know.retrieve('', self.cards)


def retrieve_ids(question, cards):
    return [card['id'] for card in know.retrieve(question, cards)]


class KnowledgeAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cards = know.load_cards(CARDS_PATH)

    def answer(self, speech, question='踏频是什么？', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return know.build_knowledge_answer(question, self.cards,
                                           http_post=transport(speech), **kwargs)

    def test_no_knowledge_never_calls_upstream(self):
        post = Mock(side_effect=AssertionError('no network'))
        result = know.build_knowledge_answer('量子力学', self.cards,
                                             api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'no_knowledge')
        self.assertEqual(result['fallback'], 'template')
        post.assert_not_called()

    def test_not_configured_never_calls_transport(self):
        post = Mock(side_effect=AssertionError('no network'))
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'must-not-read'}):
            result = know.build_knowledge_answer('踏频是什么？', self.cards,
                                                 api_key=None, http_post=post)
        self.assertEqual(result['llm_status'], 'not_configured')
        post.assert_not_called()

    def test_invalid_question_rejected(self):
        post = Mock(side_effect=AssertionError('no network'))
        for question in ('', '问' * 201, 'a\x00b', None, 42):
            with self.subTest(question=repr(question)):
                result = know.build_knowledge_answer(question, self.cards,
                                                     api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_card_grounded_answer_passes_with_source(self):
        result = self.answer('踏频是曲柄每分钟转动的圈数，和车速不同。（来源：通用运动常识）')
        self.assertEqual(result['llm_status'], 'ok')
        self.assertIn('（来源：', result['draft']['speech'])

    def test_number_outside_cards_rejected(self):
        result = self.answer('踏频一定要练到250转才行。（来源：通用运动常识）')
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_forbidden_words_rejected(self):
        result = self.answer('坚持高踏频训练效果明显，你会变强。（来源：通用运动常识）')
        self.assertEqual(result['llm_status'], 'rejected_unsafe_speech')

    def test_missing_source_rejected(self):
        result = self.answer('踏频是每分钟踩踏的转数。')
        self.assertEqual(result['llm_status'], 'rejected_bad_schema')

    def test_payload_contract_no_key_no_history(self):
        post = transport('踏频是曲柄每分钟转动的圈数。（来源：通用运动常识）')
        result = know.build_knowledge_answer('踏频是什么？', self.cards,
                                             api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        request = post.call_args.args[0]
        self.assertNotIn(b'synthetic-key', request.data)
        body = json.loads(request.data)
        self.assertEqual(len(body['messages']), 2)
        facts = json.loads(body['messages'][1]['content'])
        self.assertEqual(facts['question'], '踏频是什么？')
        self.assertTrue(facts['cards'])

    def test_template_snapshot(self):
        self.assertEqual(know.knowledge_template(), '这个我还没学过，等我的知识库收录了再聊。')

    def test_history_projects_into_facts(self):
        post = transport('踏频是曲柄每分钟转动的圈数。（来源：通用运动常识）')
        history = [{'q': '踏频是什么？', 'a': '曲柄每分钟转动的圈数。'}]
        result = know.build_knowledge_answer('那为什么不是车速？', self.cards, history=history,
                                             api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        facts = json.loads(json.loads(post.call_args.args[0].data)['messages'][1]['content'])
        self.assertEqual(facts['recent_dialog'], history)

    def test_invalid_history_rejected_without_upstream(self):
        post = Mock(side_effect=AssertionError('no network'))
        for history in ([{'q': '问', 'a': '答'}] * 4, [{'q': '问'}], '问'):
            with self.subTest(history=repr(history)[:40]):
                result = know.build_knowledge_answer('踏频是什么？', self.cards, history=history,
                                                     api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_upstream_failures_map_to_fallback(self):
        for post in (Mock(return_value=(500, {}, b'{}')),
                     Mock(return_value=(200, {}, b'not json')),
                     Mock(side_effect=TimeoutError())):
            with self.subTest(post=post):
                result = know.build_knowledge_answer('踏频是什么？', self.cards,
                                                     api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], 'upstream_error')
                self.assertEqual(result['fallback'], 'template')


if __name__ == '__main__':
    unittest.main()
