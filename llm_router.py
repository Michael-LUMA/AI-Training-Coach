"""Lane-aware model routing with bounded cross-provider failover.

中文摘要：每条语音泳道得到一份有序的「服务商尝试计划」。主用阿里云 DashScope；
当 LLM_BACKUP_* 环境变量完整配置时，一个通用 OpenAI 兼容备份自动加入计划末尾。
故障转移只针对 upstream_error（传输或服务商故障）；核验链的拒稿是内容判决，
绝不换家重试——同样的事实门槛也会拦住下一份草稿。这不是「重试一份被拒的
草稿」，而是「一次从未得到应答的请求换一个通道完成」。

环境契约（仅服务端读取；各 builder 仍只收显式凭证，自己不碰环境变量）：
  DASHSCOPE_API_KEY        主服务商 key（现有）
  LLM_BACKUP_BASE_URL      备份服务商的 https chat/completions 完整 URL
  LLM_BACKUP_API_KEY       备份服务商 bearer key
  LLM_BACKUP_MODEL_FAST    备份在快速泳道（live/review/weekly/闲聊）用的模型名
  LLM_BACKUP_MODEL_SMART   备份在智能泳道（personal/knowledge）用的模型名

备份四项缺一即整体不启用；BASE_URL 只接受 https（事实与 key 不走明文）。
"""
from __future__ import annotations

import os

from llm_review import ENDPOINT as _PRIMARY_ENDPOINT

FAST = 'fast'
SMART = 'smart'

# Lane → tier. Fast lanes answer mid-ride or under silence windows; smart lanes
# answer after the ride where phrasing quality wins at tolerable latency.
LANES = {'live': FAST, 'live_answer': FAST, 'review': FAST, 'review_l2': FAST,
         'weekly': FAST, 'personal': SMART, 'knowledge': SMART}

_PRIMARY_MODELS = {FAST: 'qwen-flash', SMART: 'qwen-plus'}

# Short social openers neither need nor repay the smart model. Long questions
# that merely contain a greeting keep the smart lane (「你好，这次心率多少」).
_SMALL_TALK = ('你好', '您好', '在吗', '谢谢', '谢了', '早上好', '晚上好', '晚安',
               '哈哈', '哈喽', '嗨', 'hi', 'hello', 'hey')
_SMALL_TALK_MAX_LEN = 6

# Transport/provider failure statuses across lanes. The live lane spells it
# 'error_upstream'; every other lane uses llm_review's 'upstream_error'. Both
# mean "no answer ever arrived" — the only condition that may fail over.
_UPSTREAM_STATUSES = ('upstream_error', 'error_upstream')

_BACKUP_URL = 'LLM_BACKUP_BASE_URL'
_BACKUP_KEY = 'LLM_BACKUP_API_KEY'
_BACKUP_MODELS = {FAST: 'LLM_BACKUP_MODEL_FAST', SMART: 'LLM_BACKUP_MODEL_SMART'}


def _check_key(key):
    return (type(key) is str and 1 <= len(key) <= 4096
            and all(33 <= ord(c) <= 126 for c in key))


def _check_url(url):
    return (type(url) is str and 8 <= len(url) <= 200
            and url.startswith('https://')
            and not any(ord(c) <= 32 or ord(c) == 127 for c in url))


def _check_model(name):
    return (type(name) is str and 1 <= len(name) <= 100
            and not any(ord(c) <= 32 or ord(c) == 127 for c in name))


def model_tier(lane, question=None):
    """Resolve a lane's tier; small talk in the personal lane drops to fast."""
    if lane not in LANES:
        raise ValueError('unknown lane')
    tier = LANES[lane]
    if (lane == 'personal' and type(question) is str
            and 1 <= len(question) <= _SMALL_TALK_MAX_LEN
            and any(marker in question.lower() for marker in _SMALL_TALK)):
        return FAST
    return tier


def plan(lane, *, question=None, environ=None):
    """Ordered provider attempts for one lane. Empty list means not configured."""
    env = os.environ if environ is None else environ
    tier = model_tier(lane, question)
    attempts = []
    key = env.get('DASHSCOPE_API_KEY')
    if _check_key(key):
        attempts.append({'provider': 'dashscope', 'endpoint': _PRIMARY_ENDPOINT,
                         'model': _PRIMARY_MODELS[tier], 'api_key': key})
    url, bkey = env.get(_BACKUP_URL), env.get(_BACKUP_KEY)
    bmodel = env.get(_BACKUP_MODELS[tier])
    if _check_url(url) and _check_key(bkey) and _check_model(bmodel):
        attempts.append({'provider': 'backup', 'endpoint': url,
                         'model': bmodel, 'api_key': bkey})
    return attempts


def run(lane, call, *, question=None, environ=None):
    """Execute one lane call with bounded failover; return the builder's result.

    call(**{endpoint, model, api_key}) must return the lane builder's native
    result dict. With no configured provider the call runs once with None
    credentials so the builder returns its own well-shaped not_configured.
    Failover continues only while the result is exactly 'upstream_error';
    verification rejections and ok stop the plan immediately.
    """
    attempts = plan(lane, question=question, environ=environ)
    if not attempts:
        return call(endpoint=None, model=None, api_key=None)
    result = None
    for attempt in attempts:
        result = call(endpoint=attempt['endpoint'], model=attempt['model'],
                      api_key=attempt['api_key'])
        if not (type(result) is dict and result.get('llm_status') in _UPSTREAM_STATUSES):
            break
    return result
