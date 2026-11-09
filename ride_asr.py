"""Push-to-talk ASR: one WAV clip in, one transcript out, via DashScope WS relay.

服务端作为 WebSocket 客户端连接 paraformer-realtime-v2：run-task →
task-started → 分块发送音频 → finish-task → 收集 sentence_end 文本。
key 不出服务器；本模块自带最小 RFC6455 客户端（仅 TLS、掩码、ping/pong），
ws_dial 可注入以便离线测试。无环境查找、无持久化、无重试。
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import struct
import time
from collections.abc import Mapping
from urllib.parse import urlsplit
import threading

from llm_review import _json

MODEL = 'paraformer-realtime-v2'
WS_URL = 'wss://dashscope.aliyuncs.com/api-ws/v1/inference'
MAX_AUDIO_BYTES = 1 << 20          # 32KB/s 的 16k 单声道 WAV，约 30 秒上限
MAX_RESPONSE_BYTES = 1 << 16
TASK_TIMEOUT_S = 30.0
CHUNK_BYTES = 3200                 # 100ms @16kHz/16bit/mono
CHUNK_PACE_S = 0.005
ASR_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,63}')
SESSION_TTL_S = 180.0              # 流式识别会话无活动自焚
MAX_ASR_SESSIONS = 50              # 进程内流式会话上限
FINISH_TIMEOUT_S = 20.0            # 收尾等待转写结果的上限


def _result(status, text=None, usage=None):
    return {'asr_status': status, 'text': text, 'usage': usage}


def check_wav(blob):
    """Strict RIFF/WAVE shape: PCM, 16-bit, mono, 8k..48k, bounded data."""
    if type(blob) is not bytes or not 44 + 2 <= len(blob) <= MAX_AUDIO_BYTES:
        raise ValueError('invalid wav size')
    if blob[0:4] != b'RIFF' or blob[8:12] != b'WAVE':
        raise ValueError('invalid wav header')
    offset = 12
    fmt = None
    data = None
    while offset + 8 <= len(blob):
        tag = blob[offset:offset + 4]
        size = struct.unpack('<I', blob[offset + 4:offset + 8])[0]
        start = offset + 8
        if start + size > len(blob):
            raise ValueError('truncated wav chunk')
        if tag == b'fmt ':
            if size < 16:
                raise ValueError('invalid fmt chunk')
            audio_format, channels, rate, _, _, bits = struct.unpack('<HHIIHH', blob[start:start + 16])
            fmt = (audio_format, channels, rate, bits)
        elif tag == b'data':
            data = blob[start:start + size]
        offset = start + size + (size & 1)
    if fmt is None or data is None or not data:
        raise ValueError('missing wav chunks')
    if fmt[0] != 1 or fmt[1] != 1 or fmt[3] != 16 or not 8000 <= fmt[2] <= 48000:
        raise ValueError('unsupported wav encoding')
    return fmt[2], data


# --- Minimal RFC6455 client -------------------------------------------------

class WsError(Exception):
    pass


def _read_exactly(sock, count):
    data = b''
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise WsError('connection closed')
        data += part
    return data


def _ws_send(sock, opcode, payload=b''):
    header = bytes((0x80 | opcode,))
    length = len(payload)
    if length < 126:
        header += bytes((0x80 | length,))
    elif length <= 0xFFFF:
        header += bytes((0x80 | 126,)) + struct.pack('>H', length)
    else:
        header += bytes((0x80 | 127,)) + struct.pack('>Q', length)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def _ws_recv(sock):
    """One complete message: (opcode, payload). Handles ping/continuation."""
    message = b''
    opcode = None
    while True:
        first, second = _read_exactly(sock, 2)
        fin = first & 0x80
        kind = first & 0x0F
        if second & 0x80:
            raise WsError('server frames must be unmasked')
        length = second & 0x7F
        if length == 126:
            length = struct.unpack('>H', _read_exactly(sock, 2))[0]
        elif length == 127:
            length = struct.unpack('>Q', _read_exactly(sock, 8))[0]
        if length > MAX_RESPONSE_BYTES:
            raise WsError('frame too large')
        payload = _read_exactly(sock, length) if length else b''
        if kind == 0x9:                      # ping -> pong, keep waiting
            _ws_send(sock, 0xA, payload)
            continue
        if kind == 0x8:
            raise WsError('closed by peer')
        if kind in (0x1, 0x2):
            opcode = kind
            message = payload
        elif kind == 0x0 and opcode is not None:
            message += payload
        else:
            raise WsError('unexpected frame')
        if fin:
            return opcode, message


def _ws_dial(url, *, headers, timeout=TASK_TIMEOUT_S):
    """TLS WebSocket client handshake; returns a connected socket."""
    parts = urlsplit(url)
    if parts.scheme != 'wss' or not parts.hostname or parts.port not in (None, 443):
        raise WsError('invalid ws url')
    key = base64.b64encode(os.urandom(16)).decode()
    lines = ['GET %s HTTP/1.1' % (parts.path or '/'),
             'Host: ' + parts.hostname,
             'Upgrade: websocket', 'Connection: Upgrade',
             'Sec-WebSocket-Key: ' + key, 'Sec-WebSocket-Version: 13']
    lines.extend('%s: %s' % pair for pair in headers)
    sock = socket.create_connection((parts.hostname, 443), timeout=timeout)
    try:
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        tls.settimeout(timeout)
        tls.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
        response = b''
        while b'\r\n\r\n' not in response:
            chunk = tls.recv(4096)
            if not chunk or len(response) > MAX_RESPONSE_BYTES:
                raise WsError('handshake failed')
            response += chunk
        head = response.split(b'\r\n\r\n', 1)[0].decode('latin-1')
        status = head.split('\r\n', 1)[0]
        if ' 101' not in status:
            raise WsError('upgrade refused: ' + status)
        accept = re.search(r'(?im)^sec-websocket-accept:\s*(\S+)\s*$', head)
        import hashlib
        expect = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if not accept or accept.group(1) != expect:
            raise WsError('bad accept key')
        return tls
    except Exception:
        sock.close()
        raise


def transcribe_wav(blob, *, api_key=None, ws_dial=None):
    """One WAV clip -> transcript. Statuses: ok/not_configured/invalid_input/
    upstream_error/no_speech. Never raises for upstream faults."""
    try:
        rate, pcm = check_wav(blob)
    except (ValueError, struct.error):
        return _result('invalid_input')
    if api_key is None or (type(api_key) is str and not api_key.strip()):
        return _result('not_configured')
    if (type(api_key) is not str or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in api_key)):
        return _result('upstream_error')
    dial = _ws_dial if ws_dial is None else ws_dial
    task_id = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip('=')
    sock = None
    try:
        sock = dial(WS_URL, headers=[('Authorization', 'bearer ' + api_key)])
        run_task = {'header': {'action': 'run-task', 'task_id': task_id, 'streaming': 'duplex'},
                    'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                                'model': MODEL,
                                'parameters': {'format': 'wav', 'sample_rate': rate,
                                               'language_hints': ['zh', 'en']},
                                'input': {}}}
        _ws_send(sock, 0x1, json.dumps(run_task, ensure_ascii=False).encode('utf-8'))
        deadline = time.monotonic() + TASK_TIMEOUT_S
        started = False
        finals = []
        last_text = ''
        usage = None
        while time.monotonic() < deadline:
            opcode, payload = _ws_recv(sock)
            if opcode != 0x1:
                continue
            event = _json(payload)
            if type(event) is not dict:
                raise WsError('bad event')
            header = event.get('header') if type(event.get('header')) is dict else {}
            name = header.get('event')
            if name == 'task-started':
                started = True
                for offset in range(0, len(pcm), CHUNK_BYTES):
                    _ws_send(sock, 0x2, pcm[offset:offset + CHUNK_BYTES])
                    if CHUNK_PACE_S:
                        time.sleep(CHUNK_PACE_S)
                finish = {'header': {'action': 'finish-task', 'task_id': task_id,
                                     'streaming': 'duplex'},
                          'payload': {'input': {}}}
                _ws_send(sock, 0x1, json.dumps(finish).encode())
            elif name == 'result-generated':
                body = event.get('payload') if type(event.get('payload')) is dict else {}
                raw_usage = body.get('usage')
                if type(raw_usage) is dict and type(raw_usage.get('duration')) is int:
                    usage = {'duration': raw_usage['duration']}
                output = body.get('output') if type(body.get('output')) is dict else {}
                sentence = output.get('sentence') if type(output.get('sentence')) is dict else {}
                text = sentence.get('text')
                if type(text) is str and text.strip():
                    last_text = text.strip()
                    if sentence.get('sentence_end') is True:
                        finals.append(last_text)
            elif name == 'task-finished':
                text = ''.join(finals).strip() or last_text
                return _result('ok', text, usage) if text else _result('no_speech', usage=usage)
            elif name == 'task-failed':
                return _result('upstream_error', usage)
        return _result('upstream_error')
    except (WsError, OSError, TimeoutError, ValueError, UnicodeError, RecursionError, OverflowError):
        return _result('upstream_error')
    finally:
        if sock is not None:
            try:
                _ws_send(sock, 0x8, b'')
            except (WsError, OSError):
                pass
            try:
                sock.close()
            except OSError:
                pass


# --- Streaming sessions: audio flows upstream while the user is still talking ---
#
# feed_segment() validates and forwards one small WAV segment; the first segment
# dials upstream and starts the task. finish_session() sends finish-task and
# collects the transcript. Sessions live in process memory only, TTL-bounded,
# keyed by a client-chosen id (single-user token already gates the endpoint).

_sessions = {}
_sessions_lock = threading.Lock()


def _check_key_stream(api_key):
    """Same key discipline as transcribe_wav; returns a failure result or None."""
    if api_key is None or (type(api_key) is str and not api_key.strip()):
        return _result('not_configured')
    if (type(api_key) is not str or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in api_key)):
        return _result('upstream_error')
    return None


def _close_sock(sock):
    if sock is None:
        return
    try:
        _ws_send(sock, 0x8, b'')
    except (WsError, OSError):
        pass
    try:
        sock.close()
    except OSError:
        pass


def _reap_locked(now):
    dead = [key for key, session in _sessions.items()
            if now - session['touched'] > SESSION_TTL_S]
    for key in dead:
        session = _sessions.pop(key)
        _close_sock(session['sock'])


def _run_task_frame(task_id, rate):
    return {'header': {'action': 'run-task', 'task_id': task_id, 'streaming': 'duplex'},
            'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                        'model': MODEL,
                        'parameters': {'format': 'wav', 'sample_rate': rate,
                                       'language_hints': ['zh', 'en']},
                        'input': {}}}


def _await_started(sock, deadline):
    """Read events until task-started; raise WsError on failure or timeout."""
    while time.monotonic() < deadline:
        opcode, payload = _ws_recv(sock)
        if opcode != 0x1:
            continue
        event = _json(payload)
        if type(event) is not dict:
            raise WsError('bad event')
        header = event.get('header') if type(event.get('header')) is dict else {}
        name = header.get('event')
        if name == 'task-started':
            return
        if name == 'task-failed':
            raise WsError('task failed')
    raise WsError('task start timeout')


def feed_segment(asr_id, blob, *, api_key=None, ws_dial=None):
    """One WAV segment in, forwarded upstream immediately. Statuses:
    ok/not_configured/invalid_input/upstream_error."""
    if type(asr_id) is not str or ASR_ID_RE.fullmatch(asr_id) is None:
        return _result('invalid_input')
    try:
        rate, pcm = check_wav(blob)
    except (ValueError, struct.error):
        return _result('invalid_input')
    failure = _check_key_stream(api_key)
    if failure is not None:
        return failure
    dial = _ws_dial if ws_dial is None else ws_dial
    now = time.monotonic()
    with _sessions_lock:
        _reap_locked(now)
        session = _sessions.get(asr_id)
        if session is None:
            if len(_sessions) >= MAX_ASR_SESSIONS:
                return _result('upstream_error')
            session = {'sock': None, 'lock': threading.Lock(), 'touched': now,
                       'bytes': 0, 'failed': False, 'rate': rate,
                       'task_id': base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip('=')}
            _sessions[asr_id] = session
        session['touched'] = now
    with session['lock']:
        if session['failed']:
            return _result('upstream_error')
        if rate != session['rate'] or session['bytes'] + len(pcm) > MAX_AUDIO_BYTES:
            return _result('invalid_input')
        try:
            if session['sock'] is None:
                session['sock'] = dial(WS_URL, headers=[('Authorization', 'bearer ' + api_key)])
                _ws_send(session['sock'], 0x1,
                         json.dumps(_run_task_frame(session['task_id'], rate),
                                    ensure_ascii=False).encode('utf-8'))
                _await_started(session['sock'], time.monotonic() + TASK_TIMEOUT_S)
            for offset in range(0, len(pcm), CHUNK_BYTES):
                _ws_send(session['sock'], 0x2, pcm[offset:offset + CHUNK_BYTES])
        except (WsError, OSError, TimeoutError, ValueError, UnicodeError):
            session['failed'] = True
            _close_sock(session['sock'])
            session['sock'] = None
            return _result('upstream_error')
        session['bytes'] += len(pcm)
        return _result('ok')


def finish_session(asr_id, *, api_key=None):
    """Close the audio stream and collect the transcript. Always reaps the session."""
    if type(asr_id) is not str or ASR_ID_RE.fullmatch(asr_id) is None:
        return _result('invalid_input')
    with _sessions_lock:
        session = _sessions.pop(asr_id, None)
    if session is None:
        return _result('invalid_input')
    try:
        with session['lock']:
            sock = session['sock']
            if sock is None or session['failed'] or session['bytes'] == 0:
                return _result('no_speech' if session['bytes'] == 0 and not session['failed']
                               else 'upstream_error')
            try:
                finish = {'header': {'action': 'finish-task', 'task_id': session['task_id'],
                                     'streaming': 'duplex'},
                          'payload': {'input': {}}}
                _ws_send(sock, 0x1, json.dumps(finish).encode())
                deadline = time.monotonic() + FINISH_TIMEOUT_S
                finals = []
                last_text = ''
                usage = None
                while time.monotonic() < deadline:
                    opcode, payload = _ws_recv(sock)
                    if opcode != 0x1:
                        continue
                    event = _json(payload)
                    if type(event) is not dict:
                        raise WsError('bad event')
                    header = event.get('header') if type(event.get('header')) is dict else {}
                    name = header.get('event')
                    if name == 'result-generated':
                        body = event.get('payload') if type(event.get('payload')) is dict else {}
                        raw_usage = body.get('usage')
                        if type(raw_usage) is dict and type(raw_usage.get('duration')) is int:
                            usage = {'duration': raw_usage['duration']}
                        output = body.get('output') if type(body.get('output')) is dict else {}
                        sentence = output.get('sentence') if type(output.get('sentence')) is dict else {}
                        text = sentence.get('text')
                        if type(text) is str and text.strip():
                            last_text = text.strip()
                            if sentence.get('sentence_end') is True:
                                finals.append(last_text)
                    elif name == 'task-finished':
                        text = ''.join(finals).strip() or last_text
                        return _result('ok', text, usage) if text else _result('no_speech', usage=usage)
                    elif name == 'task-failed':
                        return _result('upstream_error', usage)
                return _result('upstream_error')
            except (WsError, OSError, TimeoutError, ValueError, UnicodeError, RecursionError, OverflowError):
                return _result('upstream_error')
    finally:
        _close_sock(session['sock'])
