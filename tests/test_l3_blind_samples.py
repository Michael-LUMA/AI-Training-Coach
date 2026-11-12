"""Offline V1 mechanics only: mock speech is never human-evaluation evidence."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tools import generate_l3_blind_samples as samples


def post(request, *, timeout):
    payload = json.loads(request.data)
    facts = json.loads(payload['messages'][-1]['content'])
    duration = facts['display']['duration_min']
    speech = f'模拟记录：这次记录了{duration}分钟。'
    if '你可以基于数据做有据推断' in payload['messages'][0]['content']:
        speech = f'模拟记录：记录了{duration}分钟，看起来这是本次的时长。'
    raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'speech': speech})}}]}
    return 200, {}, json.dumps(raw).encode()


class BlindSampleTests(unittest.TestCase):
    def report(self, **kwargs):
        return samples.generate(samples.load_cases(), api_key='synthetic-key',
               endpoint='https://example.invalid/chat/completions', post=post, **kwargs)

    def test_ten_consistent_synthetic_aggregate_fixtures(self):
        cases = samples.load_cases()
        self.assertEqual(len(cases), 10)
        for case in cases:
            session = samples.session_for(case)
            self.assertEqual(session.record['source'], 'simulated')
            self.assertNotIn('series', session.record)

    def test_offline_outputs_never_release_participant_packets(self):
        report = self.report()
        self.assertTrue(report['production_chain_passed'])
        self.assertFalse(report['blind_ready'])
        self.assertEqual(report['real_model_calls'], 0)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                samples.write_packets(report, Path(directory)/'packets')

    def test_paid_mode_is_bounded_to_twenty_attempts_without_fallback(self):
        mock = Mock(side_effect=post)
        report = samples.generate(samples.load_cases(), api_key='synthetic-key',
                 endpoint='https://example.invalid/chat/completions', post=mock, live=True)
        self.assertEqual(mock.call_count, 20)
        self.assertTrue(report['blind_ready'])
        self.assertEqual(report['human_evaluation'], 'not_started')
        for case in report['cases']:
            self.assertNotEqual(case['variants']['A']['speech'], case['variants']['B']['speech'])

    def test_rejected_inference_blocks_packet_export_no_retry(self):
        raw = {'choices': [{'message': {'content': '{"speech":"看起来状态不错"}'}}]}
        mock = Mock(return_value=(200, {}, json.dumps(raw).encode()))
        report = samples.generate(samples.load_cases(), api_key='synthetic-key',
                 endpoint='https://example.invalid/chat/completions', post=mock, live=True)
        self.assertEqual(mock.call_count, 20)
        self.assertFalse(report['blind_ready'])
        self.assertFalse(report['production_chain_passed'])

    def test_packets_conceal_variants_and_leave_scores_blank(self):
        report = self.report(live=True)  # Mock transport: testing packet mechanics, not real evidence.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)/'packets'
            samples.write_packets(report, out, seed=42)
            key = json.loads((out/'主持人密钥_不要发给受试者.json').read_text(encoding='utf-8'))
            self.assertEqual(len(key), 50)
            for file in out.glob('*.csv'):
                with file.open(encoding='utf-8-sig', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 10)
                self.assertNotIn('A', rows[0])
                self.assertNotIn('B', rows[0])
                self.assertTrue(all(row['continue_choice'] == '' and row['understands_1'] == '' for row in rows))
            with self.assertRaises(FileExistsError):
                samples.write_packets(report, out)

    def test_unapproved_model_fails_before_network(self):
        mock = Mock(side_effect=AssertionError('no call'))
        with self.assertRaises(ValueError):
            samples.generate(samples.load_cases(), api_key='synthetic-key',
                 endpoint='https://example.invalid', model='qwen-flash', post=mock, live=True)
        mock.assert_not_called()


    def test_numeric_comparison_without_interpretation_is_not_l3_style(self):
        def numeric_only(request, *, timeout):
            payload = json.loads(request.data)
            duration = json.loads(payload['messages'][-1]['content'])['display']['duration_min']
            raw = {'choices': [{'message': {'content': json.dumps({'speech': f'模拟记录：记录了{duration}分钟，比上次有变化。'})}}]}
            return 200, {}, json.dumps(raw).encode()
        report = samples.generate(samples.load_cases(), api_key='synthetic-key',
                 endpoint='https://example.invalid', post=numeric_only, live=True)
        self.assertTrue(report['production_chain_passed'])
        self.assertFalse(report['blind_ready'])
        self.assertTrue(all(not row['variants']['B']['style_passed'] for row in report['cases']))
