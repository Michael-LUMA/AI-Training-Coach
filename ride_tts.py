"""Mark's voice: server-side Qwen3-TTS synthesis proxy (stdlib only).

中文摘要：浏览器把已核验的播报文本 POST 给本服务端，服务端持 key 调
DashScope Qwen3-TTS HTTP 非流式合成，返回 24 小时有效的音频 URL。
文本是数据不是指令：1..300 个可打印字符，逐字校验；key 只从显式参数
进入，不读环境变量；无重试、不落盘、不记日志。上游 URL 过域名校验，
防任何畸形响应外泄。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from llm_review import MAX_RESPONSE_BYTES, TIMEOUT_S, _http_post, _json, _usage

ENDPOINT = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'
MODEL = 'qwen3-tts-flash'
VOICE = 'Ethan'
VOICES = frozenset({'Ethan', 'Cherry', 'Serena'})
MAX_TEXT = 300

# Signed OSS URLs carry no API key; still, only DashScope's own result buckets
# are plausible — anything else means the upstream response was tampered with.
_HOST_RE = re.compile(r'^dashscope-result-[a-z0-9-]+\.oss-[a-z0-9-]+\.aliyuncs\.com$')


def _check_text(text):
    if (type(text) is not str or not 1 <= len(text) <= MAX_TEXT
            or not text.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in text)):
        raise ValueError('invalid text')
    return text


def _check_audio_url(url):
    if type(url) is not str or not 8 <= len(url) <= 2048:
        raise ValueError('invalid audio url')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or _HOST_RE.fullmatch(parsed.hostname) is None):
        raise ValueError('invalid audio url host')
    return url


def _result(status, audio_url=None, expires_at=None, usage=None):
    return {'tts_status': status, 'audio_url': audio_url,
            'expires_at': expires_at, 'usage': usage}


def synthesize(text, *, api_key, voice=VOICE, http_post=None):
    """Synthesize one spoken line; return a bounded audio URL or honest status.

    text: the exact verified speech, 1..300 printable characters.
    api_key: caller-supplied DashScope key; never read from the environment.
    http_post: injectable offline transport(request, timeout) returning
        (status, headers, bytes).
    """
    if api_key is None or (type(api_key) is str and not api_key.strip()):
        return _result('not_configured')
    if (type(api_key) is not str or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in api_key)):
        return _result('upstream_error')
    try:
        text = _check_text(text)
        if type(voice) is not str or voice not in VOICES:
            return _result('rejected_bad_schema')
        payload = {'model': MODEL,
                   'input': {'text': text, 'voice': voice,
                             'language_type': 'Chinese'}}
        request = Request(ENDPOINT,
                          data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Authorization': 'Bearer ' + api_key,
                                   'Content-Type': 'application/json',
                                   'Accept': 'application/json'}, method='POST')
    except (ValueError, TypeError, UnicodeError):
        return _result('rejected_bad_schema')
    try:
        response = (_http_post if http_post is None else http_post)(request, timeout=TIMEOUT_S)
    except HTTPError as error:
        error.close()
        return _result('upstream_error')
    except (TimeoutError, OSError, URLError, ValueError):
        return _result('upstream_error')
    if (type(response) is not tuple or len(response) != 3 or type(response[0]) is not int
            or not isinstance(response[1], Mapping)
            or type(response[2]) is not bytes or len(response[2]) > MAX_RESPONSE_BYTES):
        return _result('upstream_error')
    try:
        envelope = _json(response[2])
        usage = _usage(envelope)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return _result('upstream_error')
    if response[0] != 200 or type(envelope) is not dict or envelope.get('error') is not None:
        return _result('upstream_error', usage=usage)
    try:
        output = envelope.get('output')
        if type(output) is not dict:
            return _result('rejected_bad_schema', usage=usage)
        audio = output.get('audio')
        if type(audio) is not dict:
            return _result('rejected_bad_schema', usage=usage)
        url = _check_audio_url(audio.get('url'))
        expires = audio.get('expires_at')
        if type(expires) is not int or expires <= 0:
            return _result('rejected_bad_schema', usage=usage)
        return _result('ok', url, expires, usage)
    except (ValueError, TypeError):
        return _result('rejected_bad_schema', usage=usage)
