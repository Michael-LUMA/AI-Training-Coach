"""Server-side CosyVoice proxy tests (synthetic data, no network)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server
import ride_tts
from action_cards import ActionError


GOOD_URL = ('https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/'
            'prod/cosyvoice-v3-flash/20261109/x/abc.mp3?sig=1')


def tts_transport(url=GOOD_URL, *, status=200, envelope=None):
    if envelope is None:
        envelope = {'output': {'finish_reason': 'stop',
                               'audio': {'url': url, 'expires_at': 1772697707}},
                    'usage': {'characters': 8}}
    return Mock(return_value=(status, {'Content-Type': 'application/json'},
                              json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


class SynthesizeTests(unittest.TestCase):
    def call(self, text='现在心率142。', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_tts.synthesize(text, **kwargs)

    def test_ok_and_request_shape(self):
        post = tts_transport()
        result = self.call(http_post=post)
        self.assertEqual(result['tts_status'], 'ok')
        self.assertEqual(result['audio_url'], GOOD_URL)
        self.assertEqual(result['expires_at'], 1772697707)
        request = post.call_args.args[0]
        self.assertEqual(request.full_url, ride_tts.ENDPOINT)
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-key')
        body = json.loads(post.call_args.args[0].data)
        self.assertEqual(body['input']['text'], '现在心率142。')
        self.assertEqual(body['model'], ride_tts.MODEL)
        self.assertNotIn(b'synthetic-key', post.call_args.args[0].data)

    def test_not_configured_never_calls_transport(self):
        post = Mock(side_effect=AssertionError('no network'))
        for key in (None, '', '   '):
            with self.subTest(key=repr(key)):
                result = self.call(api_key=key, http_post=post)
                self.assertEqual(result['tts_status'], 'not_configured')
                self.assertIsNone(result['audio_url'])
        post.assert_not_called()

    def test_invalid_text_rejected_without_upstream(self):
        post = Mock(side_effect=AssertionError('no network'))
        for text in ('', '   ', '字' * 301, '心率\x00', '心率\n', '心率\x7f', None, 42, ['心率']):
            with self.subTest(text=repr(text)[:30]):
                result = self.call(text, http_post=post)
                self.assertEqual(result['tts_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_boundary_text_lengths_accepted(self):
        for text in ('啊', '字' * 300):
            with self.subTest(length=len(text)):
                self.assertEqual(self.call(text, http_post=tts_transport())['tts_status'], 'ok')

    def test_audio_url_host_whitelist(self):
        bad_urls = ('https://evil.invalid/x.mp3', 'https://aliyuncs.com.evil.invalid/x.mp3',
                    'ftp://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/x.mp3',
                    '', 'x' * 3000, 42, None)
        for url in bad_urls:
            with self.subTest(url=repr(url)[:50]):
                result = self.call(http_post=tts_transport(url))
                self.assertEqual(result['tts_status'], 'rejected_bad_schema')
                self.assertIsNone(result['audio_url'])

    def test_http_audio_url_accepted(self):
        url = GOOD_URL.replace('https://', 'http://')
        result = self.call(http_post=tts_transport(url))
        self.assertEqual((result['tts_status'], result['audio_url']), ('ok', url))

    def test_malformed_envelopes_rejected(self):
        for envelope in ({}, {'output': {}}, {'output': {'audio': {}}},
                         {'output': {'audio': {'url': GOOD_URL}}},
                         {'output': {'audio': {'url': GOOD_URL, 'expires_at': 'soon'}}},
                         {'output': {'audio': {'url': GOOD_URL, 'expires_at': -1}}},
                         {'error': {'message': 'x'}}):
            with self.subTest(envelope=repr(envelope)[:50]):
                result = self.call(http_post=tts_transport(envelope=envelope))
                self.assertEqual(result['tts_status'],
                                 'upstream_error' if 'error' in envelope else 'rejected_bad_schema')

    def test_upstream_failures_map_to_upstream_error(self):
        for post in (Mock(return_value=(500, {}, b'{}')),
                     Mock(return_value=(200, {}, b'not json')),
                     Mock(side_effect=TimeoutError())):
            with self.subTest(post=post):
                self.assertEqual(self.call(http_post=post)['tts_status'], 'upstream_error')


class SpeakEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.post = tts_transport()
        self.app = coach_server.App(
            store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
            llm_post=self.post)
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        default = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                   'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                   'X-Coach-Token': self.app.token}
        default.update(headers or {})
        return self.app.dispatch('POST', '/api/coach/ride/speak', default, body)

    def test_ok_round_trip(self):
        result = self.request({'text': '现在心率142。'})
        self.assertEqual(result['tts_status'], 'ok')
        self.assertEqual(result['audio_url'], GOOD_URL)

    def test_field_validation(self):
        for payload in ({}, {'text': '心率？', 'extra': 1}, {'text': ''}, {'text': '字' * 301}):
            with self.subTest(payload=repr(payload)[:40]):
                with self.assertRaises(ActionError) as error:
                    self.request(payload)
                self.assertEqual(error.exception.status, 400)
        self.post.assert_not_called()

    def test_security_headers_required(self):
        for headers in ({'X-Coach-Token': 'bad'}, {'Origin': 'https://evil.invalid'},
                        {'Host': 'evil.invalid'}):
            with self.subTest(headers=headers):
                with self.assertRaises(ActionError) as error:
                    self.request({'text': '心率142。'}, headers)
                self.assertEqual(error.exception.status, 403)
        self.post.assert_not_called()

    def test_unconfigured_key_reports_not_configured(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            result = self.request({'text': '现在心率142。'})
        self.assertEqual(result['tts_status'], 'not_configured')
        self.assertIsNone(result['audio_url'])
        self.post.assert_not_called()

    def test_counts_toward_shared_post_bucket(self):
        for _ in range(10):
            self.request({'text': '心率142。'})
        with self.assertRaises(ActionError) as error:
            self.request({'text': '心率142。'})
        self.assertEqual((error.exception.status, error.exception.code), (429, 'rate_limited'))


if __name__ == '__main__':
    unittest.main()
