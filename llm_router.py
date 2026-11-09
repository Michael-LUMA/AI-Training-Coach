"""Lane-aware model routing across a multi-provider brain pool.

中文摘要：每条语音泳道得到一份有序的「服务商尝试计划」。脑池内置多家
OpenAI 兼容服务商（千问/DeepSeek/Moonshot/智谱/豆包），谁家配了 key 谁入池；
每条泳道（快/智）可用 LLM_FAST_ORDER / LLM_SMART_ORDER 指定优先级顺序，
默认千问在前、其余按注册表顺序、旧版 LLM_BACKUP_* 兜底。故障转移只针对
upstream_error（传输或服务商故障）；核验链的拒稿是内容判决，绝不换家重试。

环境契约（仅服务端读取；各 builder 仍只收显式凭证，自己不碰环境变量）：
  <NAME>_API_KEY           服务商 key（NAME ∈ DASHSCOPE/DEEPSEEK/MOONSHOT/ZHIPU/DOUBAO）
  <NAME>_BASE_URL          可选，覆盖注册表默认端点（仅 https）
  <NAME>_MODEL_FAST        可选，覆盖该家在快速泳道的模型名
  <NAME>_MODEL_SMART       可选，覆盖该家在智能泳道的模型名
  LLM_FAST_ORDER           可选，快速泳道的供应商顺序（逗号分隔）
  LLM_SMART_ORDER          可选，智能泳道同上
  LLM_BACKUP_*             旧版备份四件套，映射为名为 backup 的供应商

豆包（火山方舟）无通用模型名，需显式配置 DOUBAO_MODEL_FAST/SMART 才入池。
"""
from __future__ import annotations

import os

from llm_review import ENDPOINT as _PRIMARY_ENDPOINT

FAST = 'fast'
SMART = 'smart'

# Lane → tier. Fast lanes answer mid-ride or under silence windows; smart lanes
# answer after the ride where phrasing quality wins at tolerable latency.
LANES = {'live': FAST, 'live_answer': FAST, 'review': FAST, 'review_l2': FAST,
         'weekly': FAST, 'planner': FAST, 'personal': SMART, 'knowledge': FAST,
         'general': FAST}  # 交互道全部求快；记录道保 SMART 求准

# Brain pool registry: default endpoint and per-tier model names. None model
# means the provider only joins when its model env var is set explicitly.
REGISTRY = {
    'dashscope': {'url': _PRIMARY_ENDPOINT, FAST: 'qwen-flash', SMART: 'qwen-plus'},
    'deepseek': {'url': 'https://api.deepseek.com/v1/chat/completions',
                 FAST: 'deepseek-chat', SMART: 'deepseek-chat'},
    'moonshot': {'url': 'https://api.moonshot.cn/v1/chat/completions',
                 FAST: 'moonshot-v1-8k', SMART: 'moonshot-v1-32k'},
    'zhipu': {'url': 'https://open.bigmodel.cn/api/paas/v4/chat/completions',
              FAST: 'glm-4-flash', SMART: 'glm-4-plus'},
    'doubao': {'url': 'https://ark.cn-beijing.volces.com/api/v3/chat/completions',
               FAST: None, SMART: None},
    'backup': {'url': None, FAST: None, SMART: None},  # legacy LLM_BACKUP_* mapping
}
ORDER_DEFAULT = ('dashscope', 'deepseek', 'moonshot', 'zhipu', 'doubao', 'backup')

# Short social openers neither need nor repay the smart model. Long questions
# that merely contain a greeting keep the smart lane (「你好，这次心率多少」).
_SMALL_TALK = ('你好', '您好', '在吗', '谢谢', '谢了', '早上好', '晚上好', '晚安',
               '哈哈', '哈喽', '嗨', 'hi', 'hello', 'hey')
_SMALL_TALK_MAX_LEN = 6

# Transport/provider failure statuses across lanes. The live lane spells it
# 'error_upstream'; every other lane uses llm_review's 'upstream_error'. Both
# mean "no answer ever arrived" — the only condition that may fail over.
_UPSTREAM_STATUSES = ('upstream_error', 'error_upstream')


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


def _provider_attempt(name, tier, env):
    """One provider's attempt config for this tier, or None when not configured."""
    if name == 'backup':
        url = env.get('LLM_BACKUP_BASE_URL')
        key = env.get('LLM_BACKUP_API_KEY')
        model = env.get('LLM_BACKUP_MODEL_FAST' if tier == FAST else 'LLM_BACKUP_MODEL_SMART')
    else:
        prefix = name.upper()
        key = env.get(prefix + '_API_KEY')
        url = env.get(prefix + '_BASE_URL') or REGISTRY[name]['url']
        model = env.get(prefix + '_MODEL_' + ('FAST' if tier == FAST else 'SMART')) or REGISTRY[name][tier]
    if not (_check_url(url) and _check_key(key) and _check_model(model)):
        return None
    return {'provider': name, 'endpoint': url, 'model': model, 'api_key': key}


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
    order_env = env.get('LLM_FAST_ORDER' if tier == FAST else 'LLM_SMART_ORDER')
    if type(order_env) is str and order_env.strip():
        order = [name.strip().lower() for name in order_env.split(',')
                 if name.strip().lower() in REGISTRY]
    else:
        order = ORDER_DEFAULT
    attempts = []
    for name in order:
        attempt = _provider_attempt(name, tier, env)
        if attempt is not None:
            attempts.append(attempt)
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
