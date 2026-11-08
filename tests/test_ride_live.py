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

    def test_urgent_hr_bypasses_silence_window(self):
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
        milestone = self.request('/api/coach/ride/live',
                                 {'live_id': 'ride1', 'point': self.point(T0 + 600.0, km_east=5.1)})
        self.assertEqual(len(milestone['calls']), 1)  # \u9759\u9ed8\u671f\u4ece\u6b64\u523b\u8d77\u7b97
        point = {**self.point(T0 + 610.0, km_east=5.1),
                 'hr': 150, 'hr_upper': 140, 'hr_lower': 90}
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': point})
        point = {**point, 't': T0 + 621.0}  # \u8d8a\u9650\u6301\u7eed 11 \u79d2\uff0c\u5904\u4e8e\u9759\u9ed8\u671f\u5185
        result = self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': point})
        self.assertEqual(result['events'], ['hr_high'])
        self.assertEqual(len(result['calls']), 1)  # \u5b89\u5168\u4e8b\u4ef6\u76f4\u901a
        self.assertTrue(result['calls'][0]['urgent'])
        self.assertIn('150', result['calls'][0]['speech'])

    def chat(self, payload):
        return self.request('/api/coach/ride/chat', payload)

    def test_activity_selects_scene_config(self):
        body = {'live_id': 'h1', 'activity': 'hike', 'point': self.point(T0)}
        self.request('/api/coach/ride/live', body)
        near = self.request('/api/coach/ride/live',
                            {'live_id': 'h1', 'activity': 'hike', 'point': self.point(T0 + 600.0, km_east=1.5)})
        self.assertEqual(near, {'calls': []})  # hike 里程碑 2km，1.5km 不报
        hit = self.request('/api/coach/ride/live',
                           {'live_id': 'h1', 'activity': 'hike', 'point': self.point(T0 + 900.0, km_east=2.2)})
        self.assertEqual(hit['events'], ['distance_milestone'])
        self.assertIn('走了2公里', hit['calls'][0]['speech'])
        with self.assertRaises(ActionError) as error:
            self.request('/api/coach/ride/live',
                         {'live_id': 'x1', 'activity': 'swim', 'point': self.point(T0)})
        self.assertEqual(error.exception.status, 400)

    def test_live_lane_answers_present_metrics(self):
        point = {**self.point(T0), 'hr': 152, 'hr_upper': 160, 'hr_lower': 90}
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': point})
        self.app.llm_post = transport('\u73b0\u5728\u5fc3\u7387152\u3002')
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.chat({'question': '\u6211\u73b0\u5728\u5fc3\u7387\u591a\u5c11\uff1f', 'live_id': 'ride1'})
        self.assertEqual(result['lane'], 'live')
        self.assertEqual(result['speech_status'], 'ok')
        self.assertEqual(result['speech'], '\u73b0\u5728\u5fc3\u7387152\u3002')

    def test_live_lane_short_circuits_missing_metrics(self):
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.chat({'question': '\u6211\u73b0\u5728\u5fc3\u7387\u591a\u5c11\uff1f', 'live_id': 'ride1'})
        self.assertEqual(result['lane'], 'live')
        self.assertEqual(result['speech_status'], 'ok')
        self.assertEqual(result['speech'], '\u8fd9\u4e2a\u6211\u73b0\u5728\u770b\u4e0d\u5230\u3002')
        self.post.assert_not_called()

    def test_live_lane_answers_deterministically_without_key(self):
        point = {**self.point(T0), 'hr': 152}
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': point})
        result = self.chat({'question': '\u6211\u73b0\u5728\u5fc3\u7387\u591a\u5c11\uff1f', 'live_id': 'ride1'})
        self.assertEqual(result['lane'], 'live')
        self.assertEqual(result['speech_status'], 'ok')
        self.assertEqual(result['speech'], '\u73b0\u5728\u5fc3\u7387152\u3002')
        self.post.assert_not_called()

    def test_live_lane_uses_latest_point_not_first(self):
        # Regression: the door once merged hr/speed from entry[3] (the first
        # point ever), so live answers went stale as the ride progressed.
        first = {**self.point(T0), 'hr': 120}
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': first})
        second = {**self.point(T0 + 300.0, km_east=1.0), 'hr': 152}
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': second})
        self.app.llm_post = transport('现在心率152。')
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.chat({'question': '我现在心率多少？', 'live_id': 'ride1'})
        self.assertEqual((result['lane'], result['speech_status']), ('live', 'ok'))
        self.assertEqual(result['speech'], '现在心率152。')
        self.app.llm_post = transport('现在心率120。')  # stale value must fail verification
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            stale = self.chat({'question': '我现在心率多少？', 'live_id': 'ride1'})
        self.assertNotEqual((stale['lane'], stale['speech_status']), ('live', 'ok'))

    def test_live_lane_verifies_run_pace_speech(self):
        # Regression: pace phrasing (分钟每公里) was unverifiable with a key
        # configured, so every LLM pace answer fell back to template.
        import ride_live
        facts = {'activity': 'run', 'speed_mps': 3.0, 'duration_s': 600,
                 'distance_m': 1800, 'avg_speed_kmh': 10.8, 'paused': False}
        ok = ride_live.build_live_answer('现在配速多少？', facts, api_key='k',
                                         http_post=transport('现在配速5.6分钟每公里。'))
        self.assertEqual(ok['llm_status'], 'ok')
        self.assertEqual(ok['speech'], '现在配速5.6分钟每公里。')
        wrong = ride_live.build_live_answer('现在配速多少？', facts, api_key='k',
                                            http_post=transport('现在配速7.2分钟每公里。'))
        self.assertEqual(wrong['llm_status'], 'rejected_unverifiable_number')

    def test_live_lane_requires_marker_and_valid_id(self):
        self.request('/api/coach/ride/live', {'live_id': 'ride1', 'point': self.point(T0)})
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.chat({'question': '\u8e0f\u9891\u662f\u4ec0\u4e48\uff1f', 'live_id': 'ride1'})
        self.assertIn(result['lane'], ('personal', 'knowledge'))  # \u65e0\u5b9e\u65f6\u6807\u8bb0 \u2192 \u8d70\u539f\u53cc\u9053
        for bad in ('../x', [], 'x' * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(ActionError) as error:
                    self.chat({'question': '\u6211\u73b0\u5728\u5fc3\u7387\u591a\u5c11\uff1f', 'live_id': bad})
                self.assertEqual(error.exception.status, 400)


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


class LiveHeartRateTests(unittest.TestCase):
    @staticmethod
    def point(seconds, **extra):
        return {'t': T0 + seconds, 'lat': LAT, 'lng': LNG, 'speed_mps': 5.0,
                'hr_upper': 160, 'hr_lower': 100, **extra}

    @staticmethod
    def high_event(hr=170.0, upper=160.0):
        return {'type': 'hr_high', 'hr': hr, 'hr_upper': upper, 'duration_s': 10.0}

    def test_high_requires_ten_seconds_and_fires_once(self):
        session = ride_live.LiveSession()
        for seconds in (0, 9):
            self.assertEqual(session.ingest(self.point(seconds, hr=170)), [])
        events = session.ingest(self.point(10, hr=171))
        self.assertEqual(events, [self.high_event(hr=171.0)])
        for name in ('hr', 'hr_upper', 'duration_s'):
            self.assertIs(type(events[0][name]), float)
        self.assertEqual(feed(session, (self.point(t, hr=180)
                                        for t in (10, 20, 100))), [])
        # Only an upper limit is needed to detect a high episode.
        session = ride_live.LiveSession()
        for t in (0, 10):
            point = self.point(t, hr=170)
            del point['hr_lower']
            events = session.ingest(point)
        self.assertEqual(events, [self.high_event()])

    def test_short_spikes_and_missing_samples_break_confirmation(self):
        session = ride_live.LiveSession()
        events = feed(session, (self.point(t, hr=hr) for t, hr in
                               ((0, 170), (9, 120), (10, 170), (19, 120))))
        self.assertEqual(events, [])
        session.ingest(self.point(20, hr=170))
        self.assertEqual(session.ingest(self.point(25)), [])
        for t in (30, 39):
            self.assertEqual(session.ingest(self.point(t, hr=170)), [])
        events = session.ingest(self.point(40, hr=170))
        self.assertEqual(events, [dict(self.high_event(), duration_s=40.0)])

    def test_back_after_ten_seconds_and_rearm_after_fifteen(self):
        for outside, boundary, kind in ((170, 160, 'hr_high'), (90, 100, 'hr_low')):
            with self.subTest(kind=kind):
                session = ride_live.LiveSession()
                session.ingest(self.point(0, hr=outside))
                self.assertEqual(session.ingest(self.point(10, hr=outside))[0]['type'], kind)
                for t in (20, 29):
                    self.assertEqual(session.ingest(self.point(t, hr=boundary)), [])
                self.assertEqual(session.ingest(self.point(30, hr=boundary)),
                                 [{'type': 'hr_back', 'hr': float(boundary)}])
                self.assertEqual(session.ingest(self.point(35, hr=boundary)), [])
                self.assertEqual(session.ingest(self.point(40, hr=outside)), [])
                self.assertEqual(session.ingest(self.point(50, hr=outside))[0]['type'], kind)
                # A recovery lasting only fourteen seconds cannot rearm.
                self.assertEqual(session.ingest(self.point(60, hr=boundary)), [])
                self.assertEqual(session.ingest(self.point(74, hr=boundary)),
                                 [{'type': 'hr_back', 'hr': float(boundary)}])
                self.assertEqual(session.ingest(self.point(75, hr=outside)), [])
                self.assertEqual(session.ingest(self.point(85, hr=outside)), [])

    def test_low_is_symmetric_and_can_cross_from_high(self):
        session = ride_live.LiveSession()
        self.assertEqual(session.ingest(self.point(0, hr=90)), [])
        self.assertEqual(session.ingest(self.point(9, hr=90)), [])
        events = session.ingest(self.point(10, hr=89))
        self.assertEqual(events, [{'type': 'hr_low', 'hr': 89.0,
                                  'hr_lower': 100.0, 'duration_s': 10.0}])
        self.assertEqual(session.ingest(self.point(20, hr=80)), [])
        self.assertEqual(session.ingest(self.point(30, hr=170)), [])
        self.assertEqual(session.ingest(self.point(40, hr=170)),
                         [dict(self.high_event(), duration_s=40.0)])
        session = ride_live.LiveSession()
        for t in (0, 10):
            point = self.point(t, hr=90)
            del point['hr_upper']
            events = session.ingest(point)
        self.assertEqual(events[0]['type'], 'hr_low')

    def test_missing_hr_preserves_episode_until_sixty_second_reset(self):
        for kind, outside in (('hr_high', 170), ('hr_low', 90)):
            for include_timeout_sample in (False, True):
                with self.subTest(kind=kind, timeout_sample=include_timeout_sample):
                    session = ride_live.LiveSession()
                    session.ingest(self.point(0, hr=outside))
                    session.ingest(self.point(10, hr=outside))
                    # Short gaps retain the episode and the pending back event,
                    # but break the ten/fifteen-second recovery timers.
                    self.assertEqual(session.ingest(self.point(20, hr=120)), [])
                    self.assertEqual(session.ingest(self.point(25)), [])
                    self.assertEqual(session.ingest(self.point(30, hr=outside)), [])
                    self.assertEqual(session.ingest(self.point(40, hr=outside)), [])
                    self.assertEqual(session.ingest(self.point(50, hr=120)), [])
                    self.assertEqual(session.ingest(self.point(55)), [])
                    self.assertEqual(session.ingest(self.point(60, hr=120)), [])
                    self.assertEqual(session.ingest(self.point(69, hr=120)), [])
                    self.assertEqual(session.ingest(self.point(70, hr=120)),
                                     [{'type': 'hr_back', 'hr': 120.0}])
                    session.ingest(self.point(75, hr=120))
                    session.ingest(self.point(80, hr=outside))
                    self.assertEqual(session.ingest(self.point(90, hr=outside))[0]['type'], kind)
                    self.assertEqual(session.ingest(self.point(100)), [])
                    self.assertEqual(session.ingest(self.point(159)), [])
                    if include_timeout_sample:
                        self.assertEqual(session.ingest(self.point(160)), [])
                    self.assertEqual(session.ingest(self.point(160, hr=120)), [])
                    self.assertEqual(session.ingest(self.point(170, hr=120)), [])
                    self.assertEqual(session.ingest(self.point(180, hr=outside)), [])
                    self.assertEqual(session.ingest(self.point(190, hr=outside))[0]['type'], kind)

    def test_no_thresholds_never_emit_heart_events(self):
        session = ride_live.LiveSession()
        for t in range(100):
            point = LiveEventExtensionTests.point(t, hr=240 if t < 50 else 20)
            self.assertEqual(session.ingest(point), [])
        # Limits are optional on every point, not silently carried forward.
        session.ingest(self.point(100, hr=170))
        self.assertEqual(session.ingest(LiveEventExtensionTests.point(110, hr=170)), [])
        self.assertEqual(session.ingest(self.point(120, hr=170)), [])
        self.assertEqual(session.ingest(self.point(130, hr=170)),
                         [dict(self.high_event(), duration_s=130.0)])
        self.assertEqual(session.ingest(LiveEventExtensionTests.point(140, hr=120)), [])
        self.assertEqual(session.ingest(LiveEventExtensionTests.point(160, hr=120)), [])

    def test_invalid_heart_fields_rejected_without_mutation(self):
        session = ride_live.LiveSession()
        session.ingest(self.point(0, hr=170))
        before = session.facts()
        states = {kind: dict(state) for kind, state in session._hr_states.items()}
        invalid = [{'hr_upper': 100, 'hr_lower': 100},
                   {'hr_upper': 99, 'hr_lower': 100}]
        for name, values in (
                ('hr', (0, 300, 19.9, 240.1)),
                ('hr_upper', (39.9, 220.1)), ('hr_lower', (39.9, 220.1))):
            invalid.extend({name: value} for value in
                           (*values, float('nan'), float('inf'), float('-inf'),
                            True, False, None, '170', 10 ** 1000))
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                session.ingest(self.point(10, **extra))
            self.assertEqual(session.facts(), before)
            self.assertEqual(session._hr_states, states)
        self.assertEqual(session.ingest(self.point(10, hr=170)), [self.high_event()])
        for name, values in (('hr', (20, 240)), ('hr_upper', (40, 220)),
                              ('hr_lower', (40, 220))):
            for value in values:
                with self.subTest(boundary=(name, value)):
                    point = LiveEventExtensionTests.point(0, **{name: value})
                    self.assertEqual(ride_live.LiveSession().ingest(point), [])

    def test_heart_templates_without_key(self):
        for event, expected in (
                (self.high_event(hr=171.9, upper=160.9),
                 '\u5fc3\u7387171\uff0c\u8d85\u8fc7\u4f60\u8bbe\u7684\u4e0a\u9650160\u4e86\u3002'),
                ({'type': 'hr_low', 'hr': 89.9, 'hr_lower': 100.9, 'duration_s': 10.0},
                 '\u5fc3\u738789\uff0c\u4f4e\u4e8e\u4f60\u8bbe\u7684\u4e0b\u9650100\u4e86\u3002'),
                ({'type': 'hr_back', 'hr': 120.0},
                 '\u5fc3\u7387\u56de\u5230\u533a\u95f4\u91cc\u4e86\u3002')):
            with self.subTest(event=event):
                result = ride_live.build_live_speech([event], live_facts())
                self.assertEqual(result['llm_status'], 'not_configured')
                self.assertEqual(result['fallback'], 'template')
                self.assertEqual(result['speech'], expected)

    def test_llm_heart_fabrication_rejected_and_registered_bpm_pass(self):
        event = self.high_event(hr=171.9, upper=160.9)
        fallback = ride_live.build_live_speech([event], live_facts())['speech']
        for speech, status in (
                ('\u5fc3\u7387999\u3002', 'rejected_unverifiable_number'),
                ('\u5fc3\u7387999 bpm\u3002', 'rejected_unverifiable_number'),
                ('\u5fc3\u7387171.9 bpm\uff0c\u4e0a\u9650160.9 bpm\u3002', 'ok'),
                ('\u5fc3\u7387171 bpm\uff0c\u4e0a\u9650160 bpm\u3002', 'ok'),
                ('\u5fc3\u7387171\uff0c\u4e0a\u9650160\u3002', 'ok'),
                ('\u5fc3\u7387171\u7c73\u3002', 'rejected_unverifiable_number')):
            with self.subTest(speech=speech):
                post = transport(speech)
                result = ride_live.build_live_speech(
                    [event], live_facts(), api_key='synthetic-key', http_post=post)
                self.assertEqual(result['llm_status'], status)
                if status != 'ok':
                    self.assertEqual(result['fallback'], 'template')
                    self.assertEqual(result['speech'], fallback)
                payload = json.loads(post.call_args.args[0].data)
                self.assertIn('\u7981\u6b62\u533b\u7597\u5224\u65ad',
                              payload['messages'][0]['content'])

    def test_urgent_for_high_and_low_across_speech_paths(self):
        low = {'type': 'hr_low', 'hr': 90, 'hr_lower': 100, 'duration_s': 10}
        for events, urgent in (([self.high_event()], True), ([low], True),
                               (milestone_event() + [self.high_event()], True),
                               (milestone_event(), False), ([], False),
                               ([{'type': 'hr_back', 'hr': 120}], False)):
            for options in ({}, {'api_key': 'synthetic-key', 'http_post': transport()},
                            {'api_key': 'synthetic-key',
                             'http_post': Mock(side_effect=URLError('down'))},
                            {'api_key': 'bad key'}):
                with self.subTest(events=events, options=options):
                    result = ride_live.build_live_speech(events, live_facts(), **options)
                    self.assertIs(result['urgent'], urgent)
        result = ride_live.build_live_speech([{'type': 'unknown'}], live_facts())
        self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        self.assertIs(result['urgent'], False)



class LiveAnswerTests(unittest.TestCase):
    def assert_answer(self, result, status, speech=None, usage=None):
        self.assertEqual(result, {
            'llm_status': status,
            'speech': ride_live.LIVE_TEMPLATE if speech is None else speech,
            'usage': usage,
            'fallback': None if status == 'ok' else 'template',
        })

    def test_heart_rate_answer_passes_and_only_live_facts_are_sent(self):
        question = '\u6211\u73b0\u5728\u5fc3\u7387\u591a\u5c11\uff1f'
        speech = '\u73b0\u5728\u5fc3\u7387152\u3002'
        facts = {**live_facts(), 'hr': 152, 'hr_upper': 160, 'hr_lower': 100}
        before = dict(facts)
        post = transport(speech)
        result = ride_live.build_live_answer(
            question, facts, api_key='synthetic-key', http_post=post)
        self.assert_answer(result, 'ok', speech, {'prompt_tokens': 11, 'completion_tokens': 12})
        self.assertEqual(facts, before)
        post.assert_called_once()
        request = post.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload['messages'][0],
                         {'role': 'system', 'content': ride_live.LIVE_SYSTEM_PROMPT})
        self.assertEqual(json.loads(payload['messages'][1]['content']),
                         {'question': question, 'live_facts': {**facts, 'activity': 'ride'}})
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(post.call_args.kwargs, {'timeout': ride_live.TIMEOUT_S})
        for facts, speech in (
                ({'hr': 152.9}, '\u73b0\u5728\u5fc3\u7387152 bpm\u3002'),
                ({'distance_m': 5001, 'duration_s': 510},
                 '\u9a91\u4e865\u516c\u91cc\uff0c\u7528\u65f68.5\u5206\u949f\u3002')):
            with self.subTest(facts=facts):
                result = ride_live.build_live_answer(
                    '\u6211\u9a91\u5f97\u600e\u4e48\u6837\uff1f', facts,
                    api_key='synthetic-key', http_post=transport(speech))
                self.assert_answer(result, 'ok', speech,
                                   {'prompt_tokens': 11, 'completion_tokens': 12})

    def test_missing_metrics_short_circuit_before_key_and_transport(self):
        for question, facts in (
                ('\u5fc3\u7387\u591a\u5c11\uff1f', live_facts()),
                ('\u5fc3\u8df3\u591a\u5c11\uff1f', {'hr_upper': 160, 'hr_lower': 100}),
                ('\u8ddd\u79bb\u591a\u5c11\uff1f', {}),
                ('\u9a91\u4e86\u591a\u8fdc\uff1f', {}),
                ('\u9a91\u4e86\u51e0\u516c\u91cc\uff1f', {}),
                ('\u5fc3\u7387\u548c\u8ddd\u79bb\uff1f', {'hr': 152})):
            for key in (None, 'synthetic-key'):
                with self.subTest(question=question, key=key):
                    post = Mock()
                    result = ride_live.build_live_answer(
                        question, facts, api_key=key, http_post=post)
                    self.assert_answer(result, 'ok')
                    post.assert_not_called()

    def test_speed_missing_short_circuits_but_average_and_current_speed_pass(self):
        for word in ('\u901f\u5ea6', '\u591a\u5feb', '\u914d\u901f'):
            with self.subTest(word=word):
                post = Mock()
                result = ride_live.build_live_answer(
                    word + '\uff1f', {'distance_m': 5001},
                    api_key='synthetic-key', http_post=post)
                self.assert_answer(result, 'ok')
                post.assert_not_called()
        for facts, speech in (
                ({'avg_speed_kmh': 35.3}, '\u5747\u901f35.3\u516c\u91cc\u6bcf\u5c0f\u65f6\u3002'),
                ({'avg_speed_kmh': 0}, '\u5747\u901f0 km/h\u3002'),
                ({'speed_mps': 0}, '\u73b0\u5728\u901f\u5ea60 km/h\u3002'),
                ({'speed_mps': 10}, '\u73b0\u5728\u901f\u5ea636 km/h\u3002'),
                ({'speed_mps': 7.23}, '\u73b0\u5728\u901f\u5ea626.0\u516c\u91cc/\u5c0f\u65f6\u3002'),
                ({'speed_mps': 7.23}, '\u73b0\u5728\u901f\u5ea67.23\u7c73\u6bcf\u79d2\u3002')):
            with self.subTest(facts=facts, speech=speech):
                post = transport(speech)
                result = ride_live.build_live_answer(
                    '\u6211\u901f\u5ea6\u591a\u5c11\uff1f', facts,
                    api_key='synthetic-key', http_post=post)
                self.assert_answer(result, 'ok', speech,
                                   {'prompt_tokens': 11, 'completion_tokens': 12})
                post.assert_called_once()

    def test_no_key_returns_template_without_transport(self):
        for key in (None, '', '   '):
            with self.subTest(key=key):
                post = Mock()
                result = ride_live.build_live_answer(
                    '\u73b0\u5728\u600e\u4e48\u6837\uff1f', {'hr': 152}, api_key=key, http_post=post)
                self.assert_answer(result, 'not_configured')
                post.assert_not_called()

    def test_no_key_deterministic_metric_answers(self):
        post = Mock()
        cases = (('\u6211\u73b0\u5728\u5fc3\u7387\u591a\u5c11\uff1f', {'hr': 152.0}, '\u73b0\u5728\u5fc3\u7387152\u3002'),
                 ('\u6211\u73b0\u5728\u901f\u5ea6\u591a\u5feb\uff1f', {'speed_mps': 10.0}, '\u73b0\u5728\u901f\u5ea636\u516c\u91cc\u6bcf\u5c0f\u65f6\u3002'),
                 ('\u6211\u73b0\u5728\u9a91\u4e86\u591a\u8fdc\uff1f', {'distance_m': 5200.0}, '\u73b0\u5728\u9a91\u4e865.2\u516c\u91cc\u3002'),
                 ('\u6211\u73b0\u5728\u9a91\u4e86\u591a\u4e45\uff1f', {'duration_s': 1500.0}, '\u73b0\u5728\u9a91\u4e8625\u5206\u949f\u3002'))
        for question, facts, speech in cases:
            with self.subTest(question=question):
                result = ride_live.build_live_answer(question, facts, http_post=post)
                self.assertEqual(result['llm_status'], 'ok')
                self.assertEqual(result['speech'], speech)
                post.assert_not_called()
        for key in (False, 123, 'bad key', 'x' * 4097):
            with self.subTest(invalid_key=key):
                post = Mock()
                result = ride_live.build_live_answer(
                    '\u73b0\u5728\u600e\u4e48\u6837\uff1f', {}, api_key=key, http_post=post)
                self.assert_answer(result, 'error_upstream')
                post.assert_not_called()

    def test_fabricated_numbers_and_bad_drafts_fall_back(self):
        for speech in ('\u5fc3\u7387999\u3002', '\u5fc3\u7387999 bpm\u3002',
                       '\u5fc3\u7387152\u7c73\u3002', '\u5fc3\u7387\u4e5d\u767e\u4e5d\u5341\u4e5d\u3002'):
            with self.subTest(speech=speech):
                result = ride_live.build_live_answer(
                    '\u5fc3\u7387\u591a\u5c11\uff1f', {'hr': 152},
                    api_key='synthetic-key', http_post=transport(speech))
                self.assert_answer(result, 'rejected_unverifiable_number',
                                   usage={'prompt_tokens': 11, 'completion_tokens': 12})
        for draft in ({'speech': ''}, {'speech': ' '}, {'speech': 152},
                      {'speech': 'x' * 61}, {'speech': 'bad\ntext'},
                      {'speech': '\u597d\u3002\u597d\u3002\u597d\u3002'},
                      {'speech': '\u597d\u3002', 'urgent': True}):
            with self.subTest(draft=draft):
                result = ride_live.build_live_answer(
                    '\u73b0\u5728\u600e\u4e48\u6837\uff1f', {'hr': 152},
                    api_key='synthetic-key', http_post=transport(draft=draft))
                self.assert_answer(result, 'rejected_bad_schema',
                                   usage={'prompt_tokens': 11, 'completion_tokens': 12})

    def test_upstream_failures_return_template(self):
        for post, usage in (
                (Mock(side_effect=URLError('down')), None),
                (Mock(side_effect=TimeoutError('timeout')), None),
                (Mock(return_value=None), None),
                (Mock(return_value=(200, {}, b'not json')), None),
                (Mock(return_value=(200, {}, b'[]')), None),
                (Mock(return_value=(200, {}, b'{"error":"down"}')), None),
                (Mock(return_value=(200, {}, b'x' * (ride_live.MAX_RESPONSE_BYTES + 1))), None),
                (transport(status=503), {'prompt_tokens': 11, 'completion_tokens': 12})):
            with self.subTest(post=post):
                result = ride_live.build_live_answer(
                    '\u5fc3\u7387\u591a\u5c11\uff1f', {'hr': 152},
                    api_key='synthetic-key', http_post=post)
                self.assert_answer(result, 'error_upstream', usage=usage)

    def test_invalid_questions_and_facts_raise_value_error(self):
        post = Mock()
        for question in (None, 123, '', ' ', '\t', 'x' * 201,
                         'bad\x00text', 'bad\ntext', 'bad\x7ftext'):
            with self.subTest(question=question), self.assertRaises(ValueError):
                ride_live.build_live_answer(
                    question, {}, api_key='synthetic-key', http_post=post)
        for facts in (None, [], 'facts', {'paused': 0}, {'paused': None}):
            with self.subTest(facts=facts), self.assertRaises(ValueError):
                ride_live.build_live_answer('q', facts, http_post=post)
        for field in ('distance_m', 'duration_s', 'avg_speed_kmh',
                      'hr', 'hr_upper', 'hr_lower', 'speed_mps'):
            for value in (-1, float('nan'), float('inf'), float('-inf'),
                          True, False, None, '152', 10 ** 1000):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    ride_live.build_live_answer('q', {field: value}, http_post=post)
        post.assert_not_called()
        for question in ('q', 'x' * 200):
            for paused in (False, True):
                with self.subTest(boundary=question, paused=paused):
                    result = ride_live.build_live_answer(question, {'paused': paused, 'hr': 0})
                    self.assert_answer(result, 'not_configured')

    def test_training_advice_and_forbidden_wording_are_rejected(self):
        for speech, status in (
                ('\u5efa\u8bae\u4f60\u628a\u5fc3\u7387\u538b\u5230130', 'rejected_unverifiable_number'),
                ('\u5fc3\u7387152\uff0c\u5efa\u8bae\u4f60\u51cf\u901f\u3002', 'rejected_unsafe_speech'),
                ('\u5fc3\u7387152\uff0c\u72b6\u6001\u63d0\u5347\u3002', 'rejected_unsafe_speech')):
            with self.subTest(speech=speech):
                result = ride_live.build_live_answer(
                    '\u5fc3\u7387\u591a\u5c11\uff1f', {'hr': 152},
                    api_key='synthetic-key', http_post=transport(speech))
                self.assert_answer(result, status,
                                   usage={'prompt_tokens': 11, 'completion_tokens': 12})


if __name__ == '__main__':
    unittest.main()


class SceneConfigTests(unittest.TestCase):
    M = M_PER_DEG_LNG

    def feed_scene(self, activity, count, speed=4.0, alt_step=0.0):
        session = ride_live.LiveSession(activity)
        events = []
        for i in range(count):
            point = {'t': T0 + i * 10.0, 'lat': LAT,
                     'lng': LNG + i * 10.0 * speed / self.M, 'speed_mps': speed}
            if alt_step:
                point['alt'] = 100.0 + i * alt_step
            events.extend(session.ingest(point))
        return session, events

    def test_hike_two_km_milestone_not_one(self):
        _, events = self.feed_scene('hike', 15)   # ~0.6 km
        self.assertNotIn('distance_milestone', [e['type'] for e in events])
        _, events = self.feed_scene('hike', 60)   # ~2.4 km
        self.assertEqual([e['km'] for e in events if e['type'] == 'distance_milestone'], [2])

    def test_hike_climb_thresholds_lower(self):
        _, events = self.feed_scene('hike', 40, speed=1.5, alt_step=1.0)  # 400s, +40m
        self.assertIn('climb_start', [e['type'] for e in events])
        # 同坡度下 ride（阈值 15m/5m）不触发而 hike（10m/3m）触发 → 阈值确实分场景
        _, ride_events = self.feed_scene('ride', 40, speed=1.5, alt_step=1.0)
        self.assertNotIn('climb_start', [e['type'] for e in ride_events])

    def test_run_one_km_milestone(self):
        _, events = self.feed_scene('run', 30)    # ~1.2 km
        self.assertEqual([e['km'] for e in events if e['type'] == 'distance_milestone'], [1])

    def test_unknown_activity_rejected(self):
        with self.assertRaises(ValueError):
            ride_live.LiveSession('swim')

    def test_hike_template_uses_walk_verb(self):
        session, events = self.feed_scene('hike', 60)
        milestones = [e for e in events if e['type'] == 'distance_milestone']
        result = ride_live.build_live_speech(milestones, session.facts())
        self.assertIn('走了2公里', result['speech'])

    def test_run_pace_deterministic_answer(self):
        result = ride_live.build_live_answer('我现在配速多少？',
                                             {'speed_mps': 3.0, 'activity': 'run'})
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['speech'], '现在配速5.6分钟每公里。')

    def test_hike_distance_deterministic_answer(self):
        result = ride_live.build_live_answer('我现在走了多远？',
                                             {'distance_m': 3400.0, 'activity': 'hike'})
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['speech'], '现在走了3.4公里。')

    def test_invalid_activity_in_facts_rejected(self):
        session, events = self.feed_scene('ride', 60, speed=10.0)
        milestones = [e for e in events if e['type'] == 'distance_milestone']
        facts = dict(session.facts())
        facts['activity'] = 'fly'
        result = ride_live.build_live_speech(milestones, facts)
        self.assertEqual(result['llm_status'], 'rejected_bad_schema')

    def test_facts_carry_activity(self):
        session, _ = self.feed_scene('hike', 3)
        self.assertEqual(session.facts()['activity'], 'hike')
