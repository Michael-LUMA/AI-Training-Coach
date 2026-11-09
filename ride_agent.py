"""Mark's planner lane: the model decides which tools fetch the facts.

中文摘要：调度器不是回答者。输入问题与已知记录日期，输出严格 JSON 的工具
调用计划；服务端逐项校验白名单与参数，任一畸形整计划作废退化为无检索，
绝不阻塞问答。检索本身是对本地记录的纯函数查询，无网络、无副作用。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request

from llm_review import (ENDPOINT, MAX_RESPONSE_BYTES, TIMEOUT_S,
                        _http_post, _json, _usage)

MAX_TOOLS = 2
MAX_MATCHES = 3

# Only questions carrying history-search hints pay the planner's extra call;
# everything else answers from the base bundle exactly as before.
SEARCH_HINTS = re.compile(
    r'哪天|哪次|那次|最长|最短|最快|最久|上周|星期|昨天|前天'
    r'|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{4}-\d{1,2}-\d{1,2}')

PLANNER_PROMPT = """你是 Mark 的调度器，不是回答者。根据用户问题决定要查哪些历史记录。
只输出 JSON {"tools":[...]}，最多2个调用；不需要查就输出 {"tools":[]}。
唯一可用工具 search_sessions，args 字段：
- date：YYYY-MM-DD（问题指向具体某天时必填；相对日期按 today 换算成绝对日期）
- activity：ride 骑行 / hike 徒步 / run 跑步（问题明确限定场景时才给）
- sort：longest（问「最长/最久的一次」时给）；默认按时间倒序
规则：问题是闲聊或问的是「最近/这次/本周/累计」就不调用（那些事实已有）。
不要输出任何解释。known_dates 是实际存在记录的日期，只能查这些日期。"""

_TOOL_NAME = 'search_sessions'


def _check_date(value):
    if type(value) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None:
        raise ValueError('invalid date')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError('invalid date') from None
    return value


def _check_search_args(args):
    """One tool call's args: known keys only, at least one constraint."""
    if type(args) is not dict or not (set(args) <= {'date', 'activity', 'sort'}):
        raise ValueError('invalid search args')
    out = {}
    if 'date' in args:
        out['date'] = _check_date(args['date'])
    if 'activity' in args:
        if args['activity'] not in ('ride', 'hike', 'run'):
            raise ValueError('invalid activity')
        out['activity'] = args['activity']
    if 'sort' in args:
        if args['sort'] not in ('latest', 'longest'):
            raise ValueError('invalid sort')
        out['sort'] = args['sort']
    if not out:
        raise ValueError('empty search args')
    return out


def _check_plan(plan):
    """Planner output: bounded tool whitelist; anything malformed voids it all."""
    if type(plan) is not dict or set(plan) != {'tools'} or type(plan['tools']) is not list:
        raise ValueError('invalid plan')
    if len(plan['tools']) > MAX_TOOLS:
        raise ValueError('too many tool calls')
    calls = []
    for item in plan['tools']:
        if type(item) is not dict or set(item) != {'name', 'args'} or item['name'] != _TOOL_NAME:
            raise ValueError('unknown tool')
        calls.append({'name': _TOOL_NAME, 'args': _check_search_args(item['args'])})
    return calls


def _result(status, tools=None, usage=None):
    return {'llm_status': status, 'tools': tools or [], 'usage': usage}


def plan_searches(question, *, today, known_dates, api_key, http_post=None,
                  endpoint=None, model=None):
    """Ask the model which history searches this question needs.

    The question is data, never an instruction channel. known_dates grounds
    relative dates (「上周三」) to days that actually have records. Any malformed
    plan degrades to no tools — the base bundle still answers, honestly.
    """
    if api_key is None or (type(api_key) is str and not api_key.strip()):
        return _result('not_configured')
    if type(api_key) is not str or not 1 <= len(api_key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in api_key):
        return _result('upstream_error')
    try:
        user = {'task': 'plan', 'question': question, 'today': today,
                'known_dates': list(known_dates)[:20]}
        payload = {'model': model or 'qwen-flash', 'max_tokens': 300,
                   'response_format': {'type': 'json_object'},
                   'messages': [{'role': 'system', 'content': PLANNER_PROMPT},
                                {'role': 'user', 'content': json.dumps(user, ensure_ascii=False)}]}
        request = Request(endpoint or ENDPOINT,
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
        choices = envelope.get('choices')
        if type(choices) is not list or not choices or type(choices[0]) is not dict:
            return _result('rejected_bad_schema', usage=usage)
        content = choices[0].get('message', {}).get('content') if type(choices[0].get('message')) is dict else None
        plan = _json(content) if type(content) is str else None
        return _result('ok', _check_plan(plan), usage)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return _result('rejected_bad_schema', usage=usage)


def search_sessions(entries, args, *, limit=MAX_MATCHES):
    """Pure local history search. entries: dicts with when/summary/activity.

    No network, no writes, no inference: date is the local-calendar day of the
    record's start; longest sorts by recorded duration; latest by start time.
    """
    rows = list(entries)
    activity = args.get('activity')
    if activity is not None:
        rows = [entry for entry in rows if entry['activity'] == activity]
    day = args.get('date')
    if day is not None:
        rows = [entry for entry in rows
                if entry['when'].astimezone().date().isoformat() == day]
    if args.get('sort') == 'longest':
        key = lambda entry: Decimal(str(entry['summary'].get('duration_s') or 0))
    else:
        key = lambda entry: entry['when'].timestamp()
    rows = sorted(rows, key=key, reverse=True)[:limit]
    return [{'started_at': entry['when'].astimezone().isoformat(),
             'summary': entry['summary']} for entry in rows]
