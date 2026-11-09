"""Model-bakeoff tooling tests: corpus validation and pure helpers, no network."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import model_bakeoff


class OpenCorpusTests(unittest.TestCase):
    def write(self, cases):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False,
                                             encoding='utf-8')
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        json.dump(cases, handle, ensure_ascii=False)
        handle.close()
        return handle.name

    def test_shipped_corpus_is_valid(self):
        cases = model_bakeoff.load_open_corpus(model_bakeoff.DEFAULT_OPEN_CORPUS)
        self.assertGreaterEqual(len(cases), 15)
        ids = [case['id'] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_valid_corpus_round_trips(self):
        path = self.write([{'id': 'open-x', 'question': '问？', 'note': '备注',
                            'reject_any': ['坏词']}])
        cases = model_bakeoff.load_open_corpus(path)
        self.assertEqual(cases[0]['reject_any'], ['坏词'])

    def test_invalid_corpora_rejected_with_systemexit(self):
        bad = (
            [],
            'not a list',
            [{'question': '问？', 'note': '缺 id'}],
            [{'id': 'BAD_ID', 'question': '问？', 'note': 'id 非 kebab'}],
            [{'id': 'open-x', 'question': '', 'note': '空问题'}],
            [{'id': 'open-x', 'question': '问' * 201, 'note': '超长'}],
            [{'id': 'open-x', 'question': '问\n', 'note': '控制符'}],
            [{'id': 'open-x', 'question': '问？', 'note': ''}],
            [{'id': 'open-x', 'question': '问？', 'note': 'n', 'reject_any': '坏词'}],
            [{'id': 'open-x', 'question': '问？', 'note': 'n', 'reject_any': ['']}],
            [{'id': 'open-x', 'question': '问？', 'note': 'n', 'mystery': 1}],
            [{'id': 'open-x', 'question': '问？', 'note': 'n'},
             {'id': 'open-x', 'question': '问2？', 'note': '重复 id'}],
        )
        for corpus in bad:
            with self.subTest(corpus=repr(corpus)[:60]):
                path = self.write(corpus)
                with self.assertRaises(SystemExit):
                    model_bakeoff.load_open_corpus(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(SystemExit):
            model_bakeoff.load_open_corpus('nonexistent-open-corpus.json')


class ParseSpecsTests(unittest.TestCase):
    def test_plain_and_model_pinned_specs(self):
        self.assertEqual(model_bakeoff.parse_specs('dashscope,deepseek:deepseek-chat'),
                         [('dashscope', None), ('deepseek', 'deepseek-chat')])

    def test_unknown_providers_rejected(self):
        for text in ('openai', 'dashscope,backup', 'dashscope:qwen-flash,unknown-x'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    model_bakeoff.parse_specs(text)

    def test_blank_items_skipped(self):
        self.assertEqual(model_bakeoff.parse_specs(' dashscope , ,'), [('dashscope', None)])


class ScopedEnvTests(unittest.TestCase):
    def test_scoped_env_pins_provider_and_models(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'k123'}, clear=False):
            env = model_bakeoff._scoped_env('dashscope', 'qwen-max')
        self.assertEqual(env, {'DASHSCOPE_API_KEY': 'k123',
                               'DASHSCOPE_MODEL_FAST': 'qwen-max',
                               'DASHSCOPE_MODEL_SMART': 'qwen-max',
                               'LLM_FAST_ORDER': 'dashscope',
                               'LLM_SMART_ORDER': 'dashscope'})

    def test_scoped_env_without_key_omits_key(self):
        with patch.dict(os.environ, {}, clear=True):
            env = model_bakeoff._scoped_env('deepseek', None)
        self.assertNotIn('DEEPSEEK_API_KEY', env)
        self.assertEqual(env['LLM_FAST_ORDER'], 'deepseek')


class SummarizeTests(unittest.TestCase):
    def test_summary_math(self):
        rows = {'door': [{'passed': True, 'seconds': 1.0},
                         {'passed': False, 'seconds': 3.0}],
                'open': [{'redline_hits': []}, {'redline_hits': ['变强']}]}
        summary = model_bakeoff.summarize('m', rows)
        self.assertEqual(summary, {'model': 'm', 'door_pass': 1, 'door_total': 2,
                                   'door_pass_rate': 0.5, 'avg_seconds': 2.0,
                                   'open_redline_rows': 1})


if __name__ == '__main__':
    unittest.main()
