"""Offline ASR tests: WAV gate, WS relay flow, endpoint wiring (no network)."""
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server
import ride_asr
from action_cards import ActionError


def wav(pcm, rate=16000, channels=1, bits=16):
    block_align = channels * bits // 8
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * block_align, block_align, bits)
    return (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE'
            + b'fmt ' + struct.pack('<I', 16) + fmt
            + b'data' + struct.pack('<I', len(pcm)) + pcm)


PCM = b'\x01\x00' * 3200  # 0.2s of tone-ish samples


def server_frame(opcode, payload=b''):
    header = bytes((0x80 | opcode,))
    n = len(payload)
    if n < 126:
        header += bytes((n,))
    elif n <= 0xFFFF:
        header += bytes((126,)) + struct.pack('>H', n)
    else:
        header += bytes((127,)) + struct.pack('>Q', n)
    return header + payload


class FakeUpstream:
    """In-memory DashScope double: parses masked client frames, answers events."""

    def __init__(self, sentences=('这次心率多少', '今天怎么样'), fail=False, pong_probe=True):
        self.inbox = bytearray()
        self.outbox = bytearray()
        self.audio = bytearray()
        self.sentences = sentences
        self.fail = fail
        self.closed = False
        self.task_id = None

    def settimeout(self, _):
        pass

    def close(self):
        self.closed = True

    def sendall(self, data):
        self.inbox += data
        self._pump()

    def recv(self, count):
        if not self.outbox:
            return b''
        chunk = bytes(self.outbox[:count])
        del self.outbox[:count]
        return chunk

    def _client_frames(self):
        frames = []
        while True:
            if len(self.inbox) < 2:
                return frames
            first, second = self.inbox[0], self.inbox[1]
            opcode = first & 0x0F
            masked = second & 0x80
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(self.inbox) < 4:
                    return frames
                length = struct.unpack('>H', self.inbox[2:4])[0]
                offset = 4
            elif length == 127:
                if len(self.inbox) < 10:
                    return frames
                length = struct.unpack('>Q', self.inbox[2:10])[0]
                offset = 10
            if not masked:
                raise AssertionError('client frames must be masked')
            if len(self.inbox) < offset + 4 + length:
                return frames
            mask = bytes(self.inbox[offset:offset + 4])
            payload = bytes(b ^ mask[i & 3]
                            for i, b in enumerate(self.inbox[offset + 4:offset + 4 + length]))
            del self.inbox[:offset + 4 + length]
            frames.append((opcode, payload))

    def _emit(self, event, payload):
        self.outbox += server_frame(0x1, json.dumps(
            {'header': {'event': event, 'task_id': self.task_id},
             'payload': payload}, ensure_ascii=False).encode('utf-8'))

    def _pump(self):
        for opcode, payload in self._client_frames():
            if opcode == 0x8:
                self.closed = True
                continue
            if opcode == 0x2:
                self.audio += payload
                continue
            message = json.loads(payload)
            self.task_id = message['header']['task_id']
            action = message['header']['action']
            if action == 'run-task':
                self._emit('task-failed' if self.fail else 'task-started',
                           {} if not self.fail else {})
            elif action == 'finish-task':
                for text in self.sentences:
                    self._emit('result-generated', {'output': {'sentence': {
                        'text': text, 'sentence_end': True}},
                        'usage': {'duration': 2}})
                self._emit('task-finished', {})


class WavGateTests(unittest.TestCase):
    def test_valid_wav_passes(self):
        rate, pcm = ride_asr.check_wav(wav(PCM))
        self.assertEqual((rate, pcm), (16000, PCM))

    def test_invalid_wavs_rejected(self):
        cases = [b'', b'RIFF', b'not a wav', wav(b''),
                 wav(PCM, channels=2), wav(PCM, bits=8), wav(PCM, rate=4000),
                 wav(PCM, rate=96000), wav(PCM)[:-10]]
        for blob in cases:
            with self.subTest(blob=blob[:12]):
                with self.assertRaises(ValueError):
                    ride_asr.check_wav(blob)

    def test_oversize_rejected(self):
        with self.assertRaises(ValueError):
            ride_asr.check_wav(wav(b'\x00' * (ride_asr.MAX_AUDIO_BYTES + 1)))


class TranscribeTests(unittest.TestCase):
    def run_asr(self, blob=None, **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_asr.transcribe_wav(wav(PCM) if blob is None else blob, **kwargs)

    def test_ok_collects_final_sentences(self):
        fake = FakeUpstream()
        result = self.run_asr(ws_dial=lambda url, headers: fake)
        self.assertEqual(result['asr_status'], 'ok')
        self.assertEqual(result['text'], '这次心率多少今天怎么样')
        self.assertEqual(bytes(fake.audio), PCM)
        self.assertTrue(fake.closed)
        self.assertEqual(result['usage'], {'duration': 2})

    def test_no_speech_when_no_sentence(self):
        result = self.run_asr(ws_dial=lambda url, headers: FakeUpstream(sentences=()))
        self.assertEqual(result['asr_status'], 'no_speech')

    def test_task_failed_and_dial_failure_map_upstream(self):
        result = self.run_asr(ws_dial=lambda url, headers: FakeUpstream(fail=True))
        self.assertEqual(result['asr_status'], 'upstream_error')
        def boom(url, headers):
            raise ride_asr.WsError('dial failed')
        self.assertEqual(self.run_asr(ws_dial=boom)['asr_status'], 'upstream_error')

    def test_not_configured_never_dials(self):
        for key in (None, '', '   '):
            with self.subTest(key=key):
                result = self.run_asr(api_key=key,
                                      ws_dial=Mock_boom())
                self.assertEqual(result['asr_status'], 'not_configured')

    def test_invalid_input_never_dials(self):
        result = self.run_asr(blob=b'garbage', ws_dial=Mock_boom())
        self.assertEqual(result['asr_status'], 'invalid_input')


def Mock_boom():
    def dial(url, headers):
        raise AssertionError('must not dial')
    return dial


class AsrEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeUpstream()
        self.app = coach_server.App(
            store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
            ws_dial=lambda url, headers: self.fake)

    def request(self, body, headers=None):
        default = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                   'Content-Type': 'audio/wav', 'Content-Length': str(len(body)),
                   'X-Coach-Token': self.app.token}
        default.update(headers or {})
        return self.app.dispatch('POST', '/api/coach/ride/asr', default, body)

    def test_ok_with_key(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.request(wav(PCM))
        self.assertEqual(result['asr_status'], 'ok')
        self.assertEqual(result['text'], '这次心率多少今天怎么样')

    def test_not_configured_without_key(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            result = self.request(wav(PCM))
        self.assertEqual(result['asr_status'], 'not_configured')

    def test_content_type_and_size_enforced(self):
        with self.assertRaises(ActionError) as error:
            self.request(wav(PCM), {'Content-Type': 'application/json'})
        self.assertEqual(error.exception.status, 415)
        big = b'\x00' * (ride_asr.MAX_AUDIO_BYTES + 1)
        with self.assertRaises(ActionError) as error:
            self.request(big, {'Content-Length': str(len(big))})
        self.assertEqual(error.exception.status, 413)

    def test_invalid_wav_400_and_token_required(self):
        with self.assertRaises(ActionError) as error:
            self.request(b'garbage')
        self.assertEqual(error.exception.status, 400)
        with self.assertRaises(ActionError) as error:
            self.request(wav(PCM), {'X-Coach-Token': 'bad'})
        self.assertEqual(error.exception.status, 403)

    def test_config_advertises_asr_flag(self):
        self.app.store = SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3',
                                         get_key=lambda: None, encryption_status={})
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'k'}):
            config = self.app.dispatch('GET', '/api/coach/config',
                                       {'Host': '127.0.0.1:8794'})
        self.assertTrue(config['asr_available'])
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}):
            config = self.app.dispatch('GET', '/api/coach/config',
                                       {'Host': '127.0.0.1:8794'})
        self.assertFalse(config['asr_available'])


class StreamSessionTests(unittest.TestCase):
    def setUp(self):
        ride_asr._sessions.clear()
        self.addCleanup(ride_asr._sessions.clear)

    def feed(self, asr_id='session-01', blob=None, **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_asr.feed_segment(asr_id, wav(PCM) if blob is None else blob, **kwargs)

    def test_segments_then_finish_transcript(self):
        fake = FakeUpstream()
        dial = lambda url, headers: fake
        self.assertEqual(self.feed(ws_dial=dial)['asr_status'], 'ok')
        self.assertEqual(self.feed(ws_dial=Mock_boom())['asr_status'], 'ok')  # 同会话不重拨
        result = ride_asr.finish_session('session-01', api_key='synthetic-key')
        self.assertEqual(result['asr_status'], 'ok')
        self.assertEqual(result['text'], '这次心率多少今天怎么样')
        self.assertEqual(bytes(fake.audio), PCM * 2)
        self.assertTrue(fake.closed)
        self.assertEqual(ride_asr._sessions, {})

    def test_finish_unknown_or_bad_id_rejected(self):
        self.assertEqual(ride_asr.finish_session('nope-nope', api_key='k')['asr_status'],
                         'invalid_input')
        self.assertEqual(ride_asr.finish_session('bad id!!', api_key='k')['asr_status'],
                         'invalid_input')
        self.assertEqual(self.feed(asr_id='bad id!!')['asr_status'], 'invalid_input')

    def test_rate_mismatch_and_total_cap(self):
        self.feed(ws_dial=lambda url, headers: FakeUpstream())
        self.assertEqual(self.feed(blob=wav(PCM, rate=8000))['asr_status'], 'invalid_input')
        session = ride_asr._sessions['session-01']
        session['bytes'] = ride_asr.MAX_AUDIO_BYTES
        self.assertEqual(self.feed()['asr_status'], 'invalid_input')

    def test_not_configured_never_dials(self):
        result = ride_asr.feed_segment('session-02', wav(PCM), api_key='  ',
                                       ws_dial=Mock_boom())
        self.assertEqual(result['asr_status'], 'not_configured')
        self.assertEqual(ride_asr._sessions, {})

    def test_upstream_failure_marks_session_failed(self):
        def boom(url, headers):
            raise ride_asr.WsError('dial failed')
        self.assertEqual(self.feed(ws_dial=boom)['asr_status'], 'upstream_error')
        self.assertEqual(self.feed(ws_dial=boom)['asr_status'], 'upstream_error')
        self.assertEqual(ride_asr.finish_session('session-01', api_key='synthetic-key')['asr_status'],
                         'upstream_error')
        self.assertEqual(ride_asr._sessions, {})

    def test_ttl_reap_closes_socket(self):
        fake = FakeUpstream()
        self.feed(ws_dial=lambda url, headers: fake)
        ride_asr._sessions['session-01']['touched'] -= ride_asr.SESSION_TTL_S + 1
        self.feed(asr_id='session-02', ws_dial=lambda url, headers: FakeUpstream())
        self.assertNotIn('session-01', ride_asr._sessions)
        self.assertTrue(fake.closed)


class StreamEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeUpstream()
        self.app = coach_server.App(
            store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
            ws_dial=lambda url, headers: self.fake)
        ride_asr._sessions.clear()
        self.addCleanup(ride_asr._sessions.clear)

    def segment(self, body, asr_id='session-ep-1', headers=None):
        default = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                   'Content-Type': 'audio/wav', 'Content-Length': str(len(body)),
                   'X-Coach-Token': self.app.token, 'X-ASR-Id': asr_id}
        default.update(headers or {})
        return self.app.dispatch('POST', '/api/coach/ride/asr/segment', default, body)

    def test_segment_and_finish_roundtrip(self):
        payload = json.dumps({'asr_id': 'session-ep-1'}).encode()
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            self.assertEqual(self.segment(wav(PCM))['asr_status'], 'ok')
            result = self.app.dispatch('POST', '/api/coach/ride/asr/finish',
                                       {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                                        'Content-Type': 'application/json',
                                        'Content-Length': str(len(payload)),
                                        'X-Coach-Token': self.app.token}, payload)
        self.assertEqual(result['asr_status'], 'ok')
        self.assertEqual(result['text'], '这次心率多少今天怎么样')

    def test_segment_rejects_bad_header_and_type(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            with self.assertRaises(ActionError) as error:
                self.segment(wav(PCM), headers={'X-ASR-Id': 'bad id!!'})
            self.assertEqual(error.exception.status, 400)
            with self.assertRaises(ActionError) as error:
                self.segment(wav(PCM), headers={'Content-Type': 'application/json'})
            self.assertEqual(error.exception.status, 415)
            with self.assertRaises(ActionError) as error:
                self.segment(wav(PCM), headers={'X-Coach-Token': 'bad'})
            self.assertEqual(error.exception.status, 403)
