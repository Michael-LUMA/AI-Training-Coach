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


def transport(speech='\u5df2\u7ecf\u9a91\u4e865\u516c\u91cc\u3002', *, status=200, draft=None, finish='stop'):
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
        bad = [{'t': T0, 'lat': LAT, 'lng': LNG, 'speed_mps': 5.0},          # \u4e71\u5e8f
               {'t': T0 + 20.0, 'lat': 91.0, 'lng': LNG, 'speed_mps': 5.0},  # \u975e\u6cd5\u7eac\u5ea6
               {'t': T0 + 20.0, 'lat': LAT, 'lng': LNG, 'speed_mps': -1.0},  # \u8d1f\u901f\u5ea6
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
        self.assertIn('5\u516c\u91cc', result['speech'])

    def test_mock_llm_ok_speech_passes(self):
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key',
            http_post=transport('\u5df2\u7ecf\u9a91\u4e865\u516c\u91cc\uff0c\u7528\u65f68.5\u5206\u949f\u3002'))
        self.assertEqual(result['llm_status'], 'ok')
        self.assertIsNone(result['fallback'])

    def test_fabricated_number_rejected_falls_back(self):
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key',
            http_post=transport('\u5747\u901f99\u516c\u91cc\u6bcf\u5c0f\u65f6\uff0c\u72b6\u6001\u7206\u8868\u3002'))
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')
        self.assertEqual(result['fallback'], 'template')
        self.assertIn('5\u516c\u91cc', result['speech'])

    def test_forbidden_wording_rejected(self):
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key',
            http_post=transport('\u5df2\u7ecf\u9a91\u4e865\u516c\u91cc\uff0c\u5efa\u8bae\u4f60\u52a0\u901f\u51b2\u523a\u3002'))
        self.assertTrue(result['llm_status'].startswith('rejected'))
        self.assertEqual(result['fallback'], 'template')

    def test_upstream_error_falls_back(self):
        failing = Mock(side_effect=URLError('down'))
        result = ride_live.build_live_speech(
            milestone_event(), live_facts(), api_key='synthetic-key', http_post=failing)
        self.assertEqual(result['llm_status'], 'error_upstream')
        self.assertEqual(result['fallback'], 'template')
        self.assertIn('5\u516c\u91cc', result['speech'])


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
        self.assertIn('5\u516c\u91cc', call['speech'])
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
        self.assertEqual(call['speech'], '\u5df2\u7ecf\u9a91\u4e865\u516c\u91cc\u3002')

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


class LiveEventExtensionTests(unittest.TestCase):
    @staticmethod
    def point(seconds, **extra):
        return {'t': T0 + seconds, 'lat': LAT, 'lng': LNG,
                'speed_mps': 5.0, **extra}

    def test_sustained_climb_fires_once_with_window_ascent(self):
        session = ride_live.LiveSession()
        events = feed(session, (self.point(i * 10, alt=100.0 + i * 3)
                                for i in range(260)))
        climbs = [event for event in events if event['type'] == 'climb_start']
        self.assertEqual(len(climbs), 1)
        event = climbs[0]
        self.assertGreaterEqual(event['ascent_m'], 15.0)
        self.assertAlmostEqual(event['ascent_m'], min(event['duration_s'], 180) * 0.3)
        self.assertIs(type(event['ascent_m']), float)
        self.assertEqual(event['distance_m'], 0.0)
        self.assertGreaterEqual(len(session._alt_samples), 200)

    def test_flat_altitude_and_single_spike_do_not_start_climb(self):
        for spike in (0.0, -100.0, 100.0):
            for spike_index in (0, 1, 20):
                with self.subTest(spike=spike, spike_index=spike_index):
                    session = ride_live.LiveSession()
                    events = feed(session, (self.point(
                        i * 10, alt=100.0 + (spike if i == spike_index else 0))
                        for i in range(60)))
                    self.assertNotIn('climb_start', [event['type'] for event in events])

    def test_flat_for_160_seconds_ends_episode_and_climb_can_restart(self):
        session = ride_live.LiveSession()
        events = feed(session, (self.point(i * 10, alt=100.0 + i * 3)
                                for i in range(21)))
        events.extend(feed(session, (self.point(i * 10, alt=160.0)
                                     for i in range(21, 37))))
        self.assertIsNone(session._climb_since)
        events.extend(feed(session, (self.point(i * 10, alt=160.0 + (i - 36) * 3)
                                     for i in range(37, 57))))
        self.assertEqual(sum(event['type'] == 'climb_start' for event in events), 2)

    def test_missing_altitude_is_legal_and_skipped(self):
        session = ride_live.LiveSession()
        events = feed(session, (self.point(i * 10) for i in range(100)))
        self.assertNotIn('climb_start', [event['type'] for event in events])
        self.assertEqual(len(session._alt_samples), 0)
        events = []
        for i in range(100, 121):
            extra = {'alt': 100.0 + (i - 100) * 3} if i % 2 == 0 else {}
            events.extend(session.ingest(self.point(i * 10, **extra)))
        self.assertEqual(sum(event['type'] == 'climb_start' for event in events), 1)

    def test_fuel_crosses_45_and_90_minutes_independently(self):
        for steps in ((2699, 2700, 2700, 5399, 5400, 5400), (5401,)):
            with self.subTest(steps=steps):
                session = ride_live.LiveSession()
                session.ingest(self.point(0))
                events = feed(session, (self.point(seconds) for seconds in steps))
                self.assertEqual([event for event in events if event['type'] == 'fuel'],
                                 [{'type': 'fuel', 'minutes': 45},
                                  {'type': 'fuel', 'minutes': 90}])
                self.assertEqual([event['minutes'] for event in events
                                  if event['type'] == 'duration_milestone'], [30, 60])

    def test_off_route_edges_missing_state_and_boolean_validation(self):
        session = ride_live.LiveSession()
        states = ({'off_route': False}, {'off_route': True}, {}, {'off_route': True},
                  {'off_route': False}, {}, {'off_route': True})
        for i, extra in enumerate(states):
            with self.subTest(i=i):
                events = session.ingest(self.point(i * 10, **extra))
                expected = [{'type': 'off_route', 'distance_m': 0.0}] if i in (1, 6) else []
                self.assertEqual(events, expected)
        for value in (None, 0, 1, 'true', []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                session.ingest(self.point(100, off_route=value))

    def test_invalid_altitudes_rejected_without_mutating_session(self):
        session = ride_live.LiveSession()
        session.ingest(self.point(0, alt=100.0))
        before = session.facts()
        for value in (float('nan'), float('inf'), float('-inf'), -500.1, 9000.1,
                      None, True, '100', 10 ** 1000):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    session.ingest(self.point(10, alt=value, off_route=True))
                self.assertEqual(session.facts(), before)
                self.assertEqual(len(session._alt_samples), 1)
                self.assertFalse(session._off_route)
        for value in (-500.0, 9000.0):
            with self.subTest(boundary=value):
                ride_live.LiveSession().ingest(self.point(0, alt=value))

    def test_climb_template_without_key_uses_rounded_meters(self):
        for ascent in (18.6, 15.5, 16.5):
            with self.subTest(ascent=ascent):
                events = [{'type': 'climb_start', 'ascent_m': ascent,
                           'duration_s': 60.0, 'distance_m': 100.0}]
                result = ride_live.build_live_speech(events, live_facts())
                self.assertEqual(result['llm_status'], 'not_configured')
                self.assertEqual(result['fallback'], 'template')
                self.assertEqual(result['speech'],
                                 f'\u5728\u722c\u5761\u4e86\uff0c\u8fd9\u6bb5\u5df2\u7ecf\u722c\u4e86{int(round(ascent))}\u7c73\u3002')

    def test_climb_llm_fabrication_rejected_but_registered_meters_pass(self):
        events = [{'type': 'climb_start', 'ascent_m': 18.6,
                   'duration_s': 60.0, 'distance_m': 100.0}]
        for meters, status in (('999', 'rejected_unverifiable_number'),
                               ('18.6', 'ok'), ('19', 'ok')):
            with self.subTest(meters=meters):
                result = ride_live.build_live_speech(
                    events, live_facts(), api_key='synthetic-key',
                    http_post=transport(f'\u8fd9\u6bb5\u5df2\u7ecf\u722c\u4e86{meters}\u7c73\u3002'))
                self.assertEqual(result['llm_status'], status)
                if status != 'ok':
                    self.assertEqual(result['fallback'], 'template')
                    self.assertEqual(result['speech'],
                                     '\u5728\u722c\u5761\u4e86\uff0c\u8fd9\u6bb5\u5df2\u7ecf\u722c\u4e8619\u7c73\u3002')

    def test_fuel_and_off_route_templates_and_fuel_minutes_registration(self):
        events = [{'type': 'fuel', 'minutes': 45},
                  {'type': 'off_route', 'distance_m': 100.0}]
        expected = ('\u9a91\u4e8645\u5206\u949f\u4e86\uff0c\u559d\u53e3\u6c34\u3001\u8865\u70b9\u5403\u7684\u3002'
                    '\u597d\u50cf\u504f\u822a\u4e86\uff0c\u5230\u5b89\u5168\u7684\u5730\u65b9\u505c\u4e0b\u6765\u53ef\u4ee5\u91cd\u65b0\u89c4\u5212\u3002')
        result = ride_live.build_live_speech(events, live_facts())
        self.assertEqual(result['llm_status'], 'not_configured')
        self.assertEqual(result['speech'], expected)
        result = ride_live.build_live_speech(events, live_facts(), api_key='synthetic-key',
                                             http_post=transport(expected))
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['speech'], expected)


if __name__ == '__main__':
    unittest.main()
