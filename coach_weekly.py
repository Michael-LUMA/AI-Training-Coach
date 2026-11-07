"""Offline English weekly record summary; not a training prescription.

中文摘要：按明确的自然周汇总已有记录；缺值不补零，功率覆盖率不冒充强度区间，
恢复信号只比较前14个完整日的报告值，不诊断疲劳或健康状态。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

DISCLAIMER = 'Record summary only; not a training prescription or medical advice.'


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _value(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        if name in row:
            return _number(row[name])
    return None


def _week(rows: list[tuple[int, date, dict[str, Any]]], start: date) -> dict[str, Any]:
    selected = [(index, row) for index, day, row in rows if start <= day < start + timedelta(days=7)]
    result: dict[str, Any] = {'start': start.isoformat(), 'end': (start + timedelta(days=6)).isoformat(),
                            'activity_count': len(selected), 'basis_refs': [f'activities[{i}]' for i, _ in selected],
                            'coverage': {}}
    for field, aliases in {'total_duration_s': ('moving_time', 'duration'),
                           'total_distance_m': ('distance',),
                           'total_training_load': ('icu_training_load', 'training_load')}.items():
        values = [_value(row, *aliases) for _, row in selected]
        valid = [v for v in values if v is not None]
        total = sum(valid)
        finite = math.isfinite(total)
        result[field] = total if finite and len(valid) == len(values) else None
        result['coverage'][field] = {'known': len(valid), 'total': len(values),
                                    'observed_sum': total if finite else None}
    power_count = sum(_value(row, 'icu_weighted_avg_watts', 'weighted_avg_watts') is not None for _, row in selected)
    result['power_coverage'] = {'activities_with_power': power_count,
        'activity_count': len(selected), 'percentage': 100 * power_count / len(selected) if selected else None,
        'method': 'activity_count_with_reported_nonnegative_weighted_power',
        'limitation': 'Power availability only; insufficient data for intensity zones or time-in-zone.'}
    return result


def build_weekly_summary(activities: list[dict[str, Any]],
                         wellness: list[dict[str, Any]] | None = None, *,
                         week_start: str, ctl: float | None = None,
                         atl: float | None = None) -> dict[str, Any]:
    """Summarize an explicit Monday-to-Sunday week and any supplied previous week.

    Args:
        activities: Activity summaries with start_date_local (or date/start_date),
            moving_time/duration in seconds, distance in metres, training_load or
            icu_training_load; optional weighted_avg_watts/icu_weighted_avg_watts,
            avg_hr/average_heartrate and trainer. Unknown fields are ignored.
        wellness: Daily objects with id/date and sleep_secs/resting_hr. Duplicate
            dates are excluded, not chosen arbitrarily. A comparison requires
            every one of the preceding 14 calendar days for that metric.
        week_start: ISO Monday date; explicit bounds avoid a guessed time zone.
        ctl: Optional reported latest CTL; no fitness/readiness conclusion.
        atl: Optional reported latest ATL; no fatigue/readiness conclusion.
    Returns:
        JSON-safe draft with facts, evidence-backed inferences, exactly one
        conservative suggestion and limitations. Missing totals remain None;
        observed sums and coverage are separate. Prior-week absence is not zero.
    Raises:
        ValueError: Invalid date, non-Monday start, oversized/non-object input.
    Example:
        >>> build_weekly_summary([], week_start='2026-11-02')['facts']['current']['activity_count']
        0
    """
    start = _day(week_start)
    if start is None or week_start != start.isoformat() or start.weekday() != 0 or start < date(1, 1, 22) or start > date(9999, 12, 20):
        raise ValueError('week_start must be an ISO Monday with room for date windows')
    wellness = [] if wellness is None else wellness
    if any(not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(r, dict) for r in rows)
           for rows in (activities, wellness)):
        raise ValueError('Expected bounded lists of activity/wellness objects')
    rows = []
    warnings = []
    seen = set()
    for index, row in enumerate(activities):
        day = _day(row.get('start_date_local', row.get('date', row.get('start_date'))))
        identifier = row.get('id')
        if day is None:
            warnings.append({'code': 'invalid_activity_date', 'ref': f'activities[{index}]'})
            continue
        if isinstance(identifier, (str, int)):
            if identifier in seen:
                warnings.append({'code': 'duplicate_activity_id', 'ref': f'activities[{index}]'})
                continue
            seen.add(identifier)
        rows.append((index, day, row))
    current = _week(rows, start)
    previous = _week(rows, start - timedelta(days=7))
    comparison = {}
    inferences = []
    for key in ('activity_count', 'total_duration_s', 'total_distance_m', 'total_training_load'):
        a, b = current[key], previous[key]
        known = bool(current['activity_count'] and previous['activity_count'] and a is not None and b is not None)
        comparison[key] = {'delta': a - b if known else None,
                          'percentage': (a / b - 1) * 100 if known and b > 0 and math.isfinite((a / b - 1) * 100) else None,
                          'missing_reason': None if known else 'insufficient data'}
    load = comparison['total_training_load']['delta']
    trend = None if load is None else ('up' if load > 0 else 'down' if load < 0 else 'flat')
    if trend:
        inferences.append({'kind': 'load_trend', 'direction': trend,
            'text': f'Reported weekly training load is {trend}; this does not establish fatigue or readiness.',
            'basis_refs': ['facts.current.total_training_load', 'facts.previous.total_training_load'],
            'limitations': ['Only supplied activities are included; completeness of the activity export is not verified.']})
    daily = {}
    duplicates = set()
    for index, row in enumerate(wellness):
        day = _day(row.get('id', row.get('date')))
        if day is None:
            warnings.append({'code': 'invalid_wellness_date', 'ref': f'wellness[{index}]'})
        elif day in daily:
            duplicates.add(day)
        else:
            daily[day] = (index, row)
    for day in duplicates:
        daily.pop(day)
        warnings.append({'code': 'duplicate_wellness_date', 'date': day.isoformat()})
    signals = {}
    for field in ('sleep_secs', 'resting_hr'):
        candidates = [day for day, (_, row) in daily.items() if start <= day < start + timedelta(days=7) and _number(row.get(field)) is not None]
        signal: dict[str, Any] = {'status': 'insufficient data', 'baseline_days': 0}
        if candidates:
            day = max(candidates)
            index, row = daily[day]
            history = [(d, daily[d]) for d in (day - timedelta(days=i) for i in range(1, 15)) if d in daily and _number(daily[d][1].get(field)) is not None]
            signal.update(date=day.isoformat(), value=_number(row[field]), baseline_days=len(history))
            if len(history) == 14:
                # Exact summation then one division: divide-first loses precision
                # (50/14*14 != 50); fsum keeps the mean exact and guards overflow.
                try:
                    mean = math.fsum(_number(item[field]) for _, (_, item) in history) / 14
                except OverflowError:
                    mean = 0
                delta = (signal['value'] / mean - 1) * 100 if mean > 0 else None
                if delta is not None and math.isfinite(delta):
                    refs = [f'wellness[{index}].{field}'] + [f'wellness[{i}].{field}' for _, (i, _) in history]
                    signal.update(status='compared', baseline_mean=mean, percentage=delta, basis_refs=refs)
                    relation = 'above' if delta > 0 else 'below' if delta < 0 else 'equal to'
                    inferences.append({'kind': 'wellness_comparison', 'field': field,
                        'text': f'Reported {field} is {abs(delta):.2f}% {relation} your preceding 14-day mean. No recovery or health diagnosis is inferred.',
                        'basis_refs': refs, 'limitations': ['Reported values only; no abnormality threshold or causal conclusion.']})
        signals[field] = signal
    enough = trend is not None
    suggestion = {'text': ('For next week, avoid increasing volume or intensity until you have reviewed these records and your own context.'
                           if enough else 'Insufficient data to suggest next-week volume or intensity changes; first complete the activity and wellness records.'),
                  'basis_refs': ['facts.comparison.total_training_load', 'facts.recovery_signals'],
                  'disclaimer': DISCLAIMER}
    return {'schema_version': 'luma-coach-weekly/1', 'status': 'draft', 'language': 'en',
        'facts': {'current': current, 'previous': previous if previous['activity_count'] else None,
                  'comparison': comparison, 'load_trend': trend, 'recovery_signals': signals,
                  'reported_context': {'ctl': _number(ctl), 'atl': _number(atl)}, 'warnings': warnings},
        'inferences': inferences, 'suggestions': [suggestion], 'disclaimer': DISCLAIMER,
        'limitations': ['Summary covers supplied records only, not proof of complete calendar coverage.',
                       'Reported CTL/ATL and wellness do not establish readiness, illness or fatigue.',
                       'No FTP, intensity-zone duration or heart-rate intensity is inferred from summary averages.']}
