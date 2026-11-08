"""Offline live-companion engine tests (synthetic telemetry, no network)."""
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server
import ride_live
from coach_server import ActionError

T0 = 1_700_000_000.0
LAT = 31.0
LNG = 121.0
M_PER_DEG_LNG = 111320.0 * math.cos(math.radians(LAT))


def ride_points(speed_mps=10.0, count=700, step_s=10.0, start_index=0):
    """Straight eastbound synthetic ride; index continues across calls."""
    for i in range(start_index, start_index + count):
        yield {'t': T0 + i * step_s, 'lat': LAT,
               'lng': LNG + i * step_s * speed_mps / M_PER_DEG_LNG,
               'speed_mps': speed_mps}


def feed(session, points):
    events = []
    for point in points:
        events.extend(session.ingest(point))
    return events


def transport(speech='已经骑了5公里。', *, status=200, draft=None, finish='stop'):
    envelope = {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(
        {'speech': speech} if draft is None else draft, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 12}}
    return Mock(return_value=(status, {}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


def milestone_event():
    return [{'type': 'distance_milestone', 'km': 5, 'distance_m': 5001.0,
             'duration_s': 510.0, 'avg_speed_kmh': 35.3}]


def live_facts():
    return {'distance_m': 5001.0, 'duration_s': 510.0,
            'avg_speed_kmh': 35.3, 'paused': False}


class LiveSessionTests(unittest.TestCase):
    def test_distance_accumulates_within_one_percent(self):
        session = ride_live.LiveSession()
        feed(session, ride_points(speed_mps=10.0, count=301))  # 3010 s -> 30.1 km
        facts = session.facts()
        self.assertAlmostEqual(facts['distance_m'], 30000.0, delta=300.0)
        self.assertAlmostEqual(facts['avg_speed_kmh'], 36.0, delta=0.5)

    def test_distance_milestones_fire_once_each(self):
        session = ride_live.LiveSession()
        events = feed(session, ride_points(speed_mps=10.0, count=1130))  # ~113 km
        kms = [e['km'] for e in events if e['type'] == 'distance_milestone']
        self.assertEqual(kms, [5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                               55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110])

    def test_duration_milestone_at_thirty_minutes(self):
        session = ride_live.LiveSession()
        events = feed(session, ride_points(count=200))  # 2000 s >= 1800 s
        minutes = [e['minutes'] for e in events if e['type'] == 'duration_milestone']
        self.assertIn(30, minutes)
        self.assertNotIn(60, minutes)

    def test_pause_and_resume_events(self):
        session = ride_live.LiveSession()
        feed(session, ride_points(count=10))
        base = 10
        for i in range(base, base + 20):  # 200 s of crawling
            session.ingest({'t': T0 + i * 10.0, 'lat': LAT, 'lng': LNG, 'speed_mps': 0.1})
        self.assertTrue(session.facts()['paused'])
        events = session.ingest({'t': T0 + (base + 20) * 10.0, 'lat': LAT,
                                 'lng': LNG, 'speed_mps': 5.0})
        self.assertEqual(events[0]['type'], 'resume')
        self.assertGreaterEqual(events[0]['pause_s'], 180.0)

    def test_invalid_points_rejected(self):
        session = ride_live.LiveSession()
        session.ingest({'t': T0 + 10.0, 'lat': LAT, 'lng': LNG, 'speed_mps': 5.0})
        bad = [{'t': T0, 'lat': LAT, 'lng': LNG, 'speed_mps': 5.0},          # 乱序
               {'t': T0 + 20.0, 'lat': 91.0, 'lng': LNG, 'speed_mps': 5.0},  # 非法纬度
               {'t': T0 + 20.0, 'lat': LAT, 'lng': LNG, 'speed_mps': -1.0},  # 负速度
               {'t': T0 + 20.0, 'lat': LAT, 'lng': LNG, 'speed_mps': float('nan')}]
        for point in bad:
            with self.subTest(point=point):
                with self.assertRaises(ValueError):
                    session.ingest(point)


class LiveSpeechTests(unittest.TestCase):
    def test_no_key_uses_template_with_correct_numbers(self):
        result = ride_live.build_live_speech(milestone_event(), live_facts())
        self.assertEqual(result['llm_status'], 'not_configured')
        self.assertEqual(result['fallback'], 'template')
        self.assertIn('5公里', result['speech'])

    def test_mock_llm_ok_speech_passes(self):
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key',
            http_post=transport('已经骑了5公里，用时8.5分钟。'))
        self.assertEqual(result['llm_status'], 'ok')
        self.assertIsNone(result['fallback'])

    def test_fabricated_number_rejected_falls_back(self):
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key',
            http_post=transport('均速99公里每小时，状态爆表。'))
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')
        self.assertEqual(result['fallback'], 'template')
        self.assertIn('5公里', result['speech'])

    def test_forbidden_wording_rejected(self):
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key',
            http_post=transport('已经骑了5公里，建议你加速冲刺。'))
        self.assertTrue(result['llm_status'].startswith('rejected'))
        self.assertEqual(result['fallback'], 'template')

    def test_upstream_error_falls_back(self):
        failing = Mock(side_effect=URLError('down'))
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key', http_post=failing)
        self.assertEqual(result['llm_status'], 'error_upstream')
        self.assertEqual(result['fallback'], 'template')
        self.assertIn('5公里', result['speech'])


class LiveEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.post = transport()
        self.app = coach_server.App(
            store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
            llm_post=self.post)
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, path, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        default = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                   'Content-Type': 'application/json',
                   'Content-Length': str(len(body)), 'X-Coach-Token': self.app.token}
        default.update(headers or {})
        return self.app.dispatch('POST', path, default, body)

    def point(self, t, km_east=0.0, speed=10.0):
        return {'t': t, 'lat': LAT,
                'lng': LNG + km_east * 1000.0 / M_PER_DEG_LNG, 'speed_mps': speed}

    def test_whitelist_and_field_validation(self):
        with self.assertRaises(ActionError) as error:
            self.request('/api/coach/ride/lives', {'live_id': 'a', 'point': {}})
        self.assertEqual(error.exception.status, 404)
        for payload in ({}, {'live_id': 'a'}, {'point': {}},
                        {'live_id': '../x', 'point': {}},
                        {'live_id': 'a', 'point': [], 'extra': 1}):
            with self.subTest(payload=payload):
                with self.assertRaises(ActionError) as error:
                    self.request('/api/coach/ride/live', payload)
                self.assertEqual(error.exception.status, 400)

    def test_no_events_no_calls(self):
        result = self.request('/api/coach/ride/live',
                              {'live_id': 'ride1', 'point': self.point(T0)})
        self.assertEqual(result, {'calls': []})

    def test_milestone_call_template_when_unconfigured(self):
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
        result = self.request('/api/coach/ride/live',
                              {'live_id': 'ride1', 'point': self.point(T0 + 600.0, km_east=5.1)})
        self.assertEqual(result['events'], ['distance_milestone'])
        self.assertEqual(len(result['calls']), 1)
        call = result['calls'][0]
        self.assertEqual(call['speech_status'], 'not_configured')
        self.assertEqual(call['fallback'], 'template')
        self.assertIn('5公里', call['speech'])
        self.post.assert_not_called()

    def test_silence_window_suppresses_second_call(self):
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
        first = self.request('/api/coach/ride/live',
                             {'live_id': 'ride1', 'point': self.point(T0 + 600.0, km_east=5.1)})
        self.assertEqual(len(first['calls']), 1)
        second = self.request('/api/coach/ride/live',
                              {'live_id': 'ride1', 'point': self.point(T0 + 1200.0, km_east=10.2)})
        self.assertEqual(second, {'calls': []})

    def test_llm_ok_path_speaks_verified_numbers(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
            result = self.request('/api/coach/ride/live',
                                  {'live_id': 'ride1', 'point': self.point(T0 + 600.0, km_east=5.1)})
        call = result['calls'][0]
        self.assertEqual(call['speech_status'], 'ok')
        self.assertEqual(call['speech'], '已经骑了5公里。')

    def test_invalid_telemetry_point_rejected(self):
        with self.assertRaises(ActionError) as error:
            self.request('/api/coach/ride/live',
                         {'live_id': 'ride1',
                          'point': {'t': T0, 'lat': 91.0, 'lng': LNG, 'speed_mps': 5.0}})
        self.assertEqual(error.exception.status, 400)

    def test_sessions_isolated_by_live_id(self):
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
        result = self.request('/api/coach/ride/live',
                              {'live_id': 'ride2', 'point': self.point(T0 + 600.0, km_east=0.0)})
        self.assertEqual(result, {'calls': []})


if __name__ == '__main__':
    unittest.main()
