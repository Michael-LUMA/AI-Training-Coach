"""Synthetic two-hour endurance sample; no real rider, API key or network.

中文摘要：完全合成的室内耐力骑演示数据，禁止当作用户实测或训练处方。
"""
from __future__ import annotations

import math
from typing import Any

DEMO_BANNER = "DEMO — synthetic sample data, not your ride"


def demo_inputs() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return fresh synthetic activity, streams, settings and wellness objects.

    Samples span exactly 7200 seconds at 10-second intervals. FTP and wellness
    are invented context, not measurements or a recommended training session.
    No GPS route is invented for this indoor trainer sample.
    """
    times = list(range(0, 7201, 10))
    activity = {
        'id': 'synthetic-demo-endurance-2h', 'name': 'Synthetic endurance demo',
        'synthetic': True, 'type': 'VirtualRide', 'trainer': True,
        'start_date': '2026-11-06T08:00:00Z',
        'icu_ctl': 40, 'icu_atl': 45, 'icu_training_load': 98,
    }
    values = {
        'time': times,
        'watts': [round(175 + 12 * math.sin(2 * math.pi * t / 600)) for t in times],
        'heartrate': [round(130 + 5 * math.sin(2 * math.pi * t / 900) + 4 * t / 7200) for t in times],
        'cadence': [round(85 + 4 * math.sin(2 * math.pi * t / 300)) for t in times],
    }
    streams = [{'type': name, 'data': data} for name, data in values.items()]
    settings = {'ftp': 250, 'power_zones': [0, 138, 188, 226, 263, 301],
                'lthr': 165, 'hr_zones': [0, 115, 140, 155, 165]}
    wellness = {'resting_hr': 52, 'sleep_secs': 8 * 3600}
    return activity, streams, settings, wellness


def build_demo_review() -> dict[str, Any]:
    """Normalize the synthetic fixture and build the normal English review.

    Returns a visibly labeled demo envelope. Uses the same fact/review pipeline
    as live rides but never reads credentials, persists data or calls a provider.
    Pipeline validation exceptions propagate to the server's error handler.
    """
    from coach_icu import normalize_activity
    from coach_review_en import build_review
    record = normalize_activity(*demo_inputs())
    record['limitations'].append(DEMO_BANNER)
    review = build_review(record)
    review['synthetic'] = True
    review['facts']['messages'].insert(0, DEMO_BANNER)
    return {'review': review, 'synthetic': True, 'demo_banner': DEMO_BANNER,
            'verified': False, 'warnings': []}
