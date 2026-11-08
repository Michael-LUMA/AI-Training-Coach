"""Mark's Chinese weekly memory summary; local facts, bounded cloud wording.

中文摘要：纯函数聚合，不读磁盘；过滤混合模拟来源，复用既有数字及安全核验。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from urllib.error import HTTPError, URLError
from urllib.request import Request

from llm_review import (ENDPOINT, MODEL, TIMEOUT_S, MAX_RESPONSE_BYTES,
                        _http_post, _json, _usage)
from ride_brain import (_normalized_numbers, _verified_text, _FORBIDDEN,
                        SYSTEM_PROMPT as RIDE_PROMPT, MAX_TOKENS, _result)
from ride_memory import validate_session, RideMemoryError

# Weekly facts never contain weekly averages (averaging per-ride means is
# unsound), so any "平均" claim is unverifiable by construction, coincidence or not.
_WEEKLY_UNVERIFIABLE = re.compile(r'平均')

WEEKLY_COPY = {
    'empty': '这周还没骑车，随时开骑我都在。',
    'current': '这周骑了{count}次，一共{minutes}分钟。',
    'comparison': '比上周{count_direction}{count}次，时长{duration_direction}{minutes}分钟。',
    'simulated': '模拟记录：',
}
SYSTEM_PROMPT = RIDE_PROMPT + """
本次任务改为每周记录汇总：只说 week / previous_week / differences 中的事实。
缺少 previous_week 时不提上周或比较。本周为零时由本地模板处理。
avg_hr 是逐次样本均值，不能再求均值或当成全周平均心率。
多次骑行时禁止使用“平均”一词（全周平均心率、平均每次时长都不可核验）；
如需概括心率，用 avg_hr_range 的 low/high 说「心率在 low 到 high 之间」
（数字必须来自 avg_hr_range）。
时长用 total_duration_min / duration_diff_min；不要发明日期、趋势或效果。
只陈述次数、时长、逐次心率及已给出的差值，不建议训练量、强度或频率。
避免“一共”“一次”等隐含数字修辞，除非其中数字也有事实依据。
"""


def _minutes(seconds):
    value = (Decimal(str(seconds)) / 60).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    return int(value) if value == value.to_integral() else float(value)


def split_week_sessions(sessions: list[dict], week_start: date, *, tz=None) -> tuple[list[dict], list[dict]]:
    """Partition validated sessions by their start instant, not by string dates.

    Args:
        sessions: Validated memories (not mutated).
        week_start: Calendar Monday; caller validates Monday/future constraints.
        tz: Explicit test timezone; None uses server-local calendar boundaries.
    Returns:
        (current, previous), both half-open seven-day windows. Durations are not
        split for rides crossing midnight; the start instant selects the week.
    Raises:
        ValueError: Invalid timestamp/date supplied outside the validated contract.
    """
    def boundary(day):
        value = datetime.combine(day, datetime.min.time())
        return value.replace(tzinfo=tz) if tz is not None else value.astimezone()
    begin, end, before = (boundary(week_start), boundary(week_start + timedelta(days=7)),
                          boundary(week_start - timedelta(days=7)))
    current, previous = [], []
    for session in sessions:
        when = datetime.fromisoformat(session['started_at'].replace('Z', '+00:00'))
        if begin <= when < end:
            current.append(session)
        elif before <= when < begin:
            previous.append(session)
    return current, previous


def aggregate_ride_weeks(week_sessions: list[dict], prev_week_sessions: list[dict]) -> dict:
    """Project validated memories into local weekly facts without averaging HR.

    Mixed real/simulated input excludes simulated rides across both windows;
    all-simulated input remains explicitly labelled. Total durations are summed
    exactly, then rounded once to a whole second (half up), so deltas are ints.
    Input lists are detached by validation; raw samples, notes and IDs never
    enter the returned facts. Invalid input raises RideMemoryError/ValueError.
    """
    current = [validate_session(item) for item in week_sessions]
    previous = [validate_session(item) for item in prev_week_sessions]
    combined = current + previous
    simulated_only = bool(combined) and all(item['source'] == 'simulated' for item in combined)
    excluded = 0
    if not simulated_only:
        excluded = sum(item['source'] == 'simulated' for item in combined)
        current = [item for item in current if item['source'] != 'simulated']
        previous = [item for item in previous if item['source'] != 'simulated']
    def aggregate(items):
        seconds = sum((Decimal(str(item['summary']['duration_s'])) for item in items), Decimal(0))
        duration = int(seconds.quantize(Decimal(1), rounding=ROUND_HALF_UP))
        result = {'count': len(items), 'total_duration_s': duration,
                  'total_duration_min': _minutes(duration),
                  'avg_hr': [item['summary']['avg_hr'] for item in items]}
        recorded = [hr for hr in result['avg_hr'] if hr is not None]
        if len(recorded) > 1:
            result['avg_hr_range'] = {'low': min(recorded), 'high': max(recorded)}
        return result
    week, prev = aggregate(current), aggregate(previous) if previous else None
    differences = None
    if prev is not None:
        delta = week['total_duration_s'] - prev['total_duration_s']
        differences = {'count_diff': week['count'] - prev['count'],
                       'total_duration_diff_s': delta,
                       'duration_diff_min': _minutes(abs(delta))}
    return {'week': week, 'previous_week': prev, 'differences': differences,
            'excluded_simulated': excluded, 'source': 'simulated' if simulated_only else 'ble_gatt_0x180d'}


def weekly_template(week_agg: dict, prev_agg: dict | None) -> str:
    """Render local count/duration facts, never a training prescription.

    Takes aggregate count/total_duration_s and optional source='simulated'.
    Returns one Chinese statement and, with previous data, a numeric comparison.
    Invalid aggregate shapes raise KeyError/TypeError/ValueError.
    """
    if not week_agg['count']:
        return WEEKLY_COPY['empty']
    text = WEEKLY_COPY['current'].format(count=week_agg['count'], minutes=_minutes(week_agg['total_duration_s']))
    if prev_agg is not None and prev_agg['count']:
        count = week_agg['count'] - prev_agg['count']
        duration = week_agg['total_duration_s'] - prev_agg['total_duration_s']
        text += WEEKLY_COPY['comparison'].format(count_direction='多' if count >= 0 else '少', count=abs(count),
                      duration_direction='多' if duration >= 0 else '少', minutes=_minutes(abs(duration)))
    return (WEEKLY_COPY['simulated'] if week_agg.get('source') == 'simulated' else '') + text


def _registered_numbers(facts):
    numbers = []
    def register(value, unit):
        if value is not None:
            numeric = Decimal(str(value));numbers.extend([(numeric, unit), (abs(numeric), unit)])
    for aggregate in (facts['week'], facts['previous_week']):
        if aggregate is None:
            continue
        register(aggregate['count'], None)
        register(aggregate['total_duration_s'], 's')
        register(aggregate['total_duration_min'], 'min')
        for average in aggregate['avg_hr']:
            register(average, 'bpm')
        for bound in (aggregate.get('avg_hr_range') or {}).values():
            register(bound, 'bpm')
    if facts['differences'] is not None:
        register(facts['differences']['count_diff'], None)
        register(facts['differences']['total_duration_diff_s'], 's')
        register(facts['differences']['duration_diff_min'], 'min')
    register(facts['excluded_simulated'], None)
    return numbers


def build_ride_weekly(week_sessions: list[dict], prev_week_sessions: list[dict], *,
                      api_key: str | None = None, http_post=None,
                      endpoint: str | None = None, model: str | None = None) -> dict:
    """Generate a verified Chinese weekly draft, or an explicit template fallback.

    Args:
        week_sessions, prev_week_sessions: Validated v1 memories; never mutated.
        api_key: Explicit printable ASCII provider key; not read from environment.
        http_post: Optional offline transport(request, timeout=TIMEOUT_S).
    Returns:
        llm_status/draft/usage/fallback (same contract as ride_brain). No data
        never calls a transport; rejected drafts are discarded in their entirety.
    Raises:
        Unexpected programming errors from an injected transport propagate.
    Example:
        >>> build_ride_weekly([], [])['llm_status']
        'no_data'
    """
    try:
        facts = aggregate_ride_weeks(week_sessions, prev_week_sessions)
        numbers = _registered_numbers(facts)
    except (RideMemoryError, ValueError, TypeError, OverflowError, InvalidOperation, RecursionError):
        return _result('rejected_bad_schema')
    if facts['week']['count'] == 0:
        return _result('no_data')
    if api_key is None or (type(api_key) is str and not api_key.strip()):
        return _result('not_configured')
    if type(api_key) is not str or not 1 <= len(api_key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in api_key):
        return _result('upstream_error')
    if facts['source'] == 'simulated':
        facts['speech_rule'] = '必须以「模拟记录：」开头'
    payload = {'model': model or MODEL, 'max_tokens': MAX_TOKENS, 'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': json.dumps(facts, ensure_ascii=False, allow_nan=False)}]}
    request = Request(endpoint or ENDPOINT, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                      headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        response = (_http_post if http_post is None else http_post)(request, timeout=TIMEOUT_S)
    except HTTPError as error:
        error.close();return _result('upstream_error')
    except (TimeoutError, OSError, URLError, ValueError):
        return _result('upstream_error')
    if (type(response) is not tuple or len(response) != 3 or type(response[0]) is not int
            or not isinstance(response[1], Mapping) or type(response[2]) is not bytes or len(response[2]) > MAX_RESPONSE_BYTES):
        return _result('upstream_error')
    try:
        envelope = _json(response[2]);usage = _usage(envelope)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return _result('upstream_error')
    if response[0] != 200 or type(envelope) is not dict or envelope.get('error') is not None:
        return _result('upstream_error', usage)
    try:
        choices = envelope.get('choices')
        if type(choices) is not list or not choices or type(choices[0]) is not dict or choices[0].get('finish_reason') not in (None, 'stop'):
            return _result('rejected_bad_schema', usage)
        message = choices[0].get('message')
        content = message.get('content') if type(message) is dict else None
        draft = _json(content) if type(content) is str else None
        if (type(draft) is not dict or set(draft) != {'speech'} or type(draft['speech']) is not str
                or not draft['speech'].strip() or len(draft['speech']) > 300 or any(ord(c) < 32 for c in draft['speech'])):
            return _result('rejected_bad_schema', usage)
        speech = draft['speech']
        with localcontext() as ctx:
            ctx.prec = 100
            if not _verified_text(_normalized_numbers(speech), numbers):
                return _result('rejected_unverifiable_number', usage)
        if facts['week']['count'] > 1 and _WEEKLY_UNVERIFIABLE.search(speech):
            return _result('rejected_unverifiable_number', usage)
        if _FORBIDDEN.search(speech) or (facts['source'] == 'simulated' and not speech.startswith(WEEKLY_COPY['simulated'])):
            return _result('rejected_unsafe_speech', usage)
        return _result('ok', usage, speech)
    except (ValueError, UnicodeError, RecursionError, OverflowError, InvalidOperation):
        return _result('rejected_bad_schema', usage)
