"""Planner-lane and history-search tests (synthetic data, no network)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server
import ride_agent
import ride_chat


def plan_transport(plan, *, status=200):
    content = json.dumps(plan, ensure_ascii=False)
    envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 6}}
    return Mock(return_value=(status, {'Content-Type': 'application/json'},
                              json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


def speech_transport(speech):
    content = json.dumps({'speech': speech}, ensure_ascii=False)
    envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
    return Mock(return_value=(200, {}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


def plan_body(post):
    return json.loads(post.call_args.args[0].data)


class SearchHintsTests(unittest.TestCase):
    def test_history_search_phrases_trigger(self):
        for question in ('我11月1日那次骑了多久？', '我最长的一次骑了多久？',
                         '我上周哪天骑了？', '我昨天骑了多远？', '2026-11-01 我骑了吗？'):
            with self.subTest(question=question):
                self.assertTrue(ride_agent.SEARCH_HINTS.search(question))

    def test_plain_questions_do_not_trigger(self):
        for question in ('这次心率多少？', '我累计骑了多少？', '今天天气怎么样？', '踏频是什么？'):
            with self.subTest(question=question):
                self.assertFalse(ride_agent.SEARCH_HINTS.search(question))


class PlanSearchesTests(unittest.TestCase):
    def call(self, question='我11月1日那次骑了多久？', **kwargs):
        kwargs.setdefault('today', '2026-11-09')
        kwargs.setdefault('known_dates', ['2026-11-05', '2026-11-01'])
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_agent.plan_searches(question, **kwargs)

    def test_not_configured_never_calls_transport(self):
        post = Mock(side_effect=AssertionError('no network'))
        for key in (None, '', '   '):
            with self.subTest(key=repr(key)):
                result = self.call(api_key=key, http_post=post)
                self.assertEqual(result, {'llm_status': 'not_configured', 'tools': [], 'usage': None})
        post.assert_not_called()

    def test_good_plan_returned_and_question_is_data(self):
        plan = {'tools': [{'name': 'search_sessions', 'args': {'date': '2026-11-01'}}]}
        post = plan_transport(plan)
        result = self.call(http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['tools'], plan['tools'])
        post.assert_called_once()
        body = plan_body(post)
        self.assertEqual(body['model'], 'qwen-flash')
        self.assertNotIn(b'synthetic-key', post.call_args.args[0].data)
        user = json.loads(body['messages'][1]['content'])
        self.assertEqual(user, {'task': 'plan', 'question': '我11月1日那次骑了多久？',
                                'today': '2026-11-09',
                                'known_dates': ['2026-11-05', '2026-11-01']})

    def test_empty_plan_is_a_valid_no_search(self):
        result = self.call(question='这次心率多少？',
                           http_post=plan_transport({'tools': []}))
        self.assertEqual((result['llm_status'], result['tools']), ('ok', []))

    def test_malformed_plans_rejected(self):
        bad_plans = (
            {'tool': []},                                                    # wrong key
            {'tools': {}, },                                                 # tools not a list
            {'tools': [{'name': 'search_sessions', 'args': {}}]},            # empty args
            {'tools': [{'name': 'search_sessions'}]},                        # missing args
            {'tools': [{'name': 'delete_all', 'args': {'date': '2026-11-01'}}]},
            {'tools': [{'name': 'search_sessions', 'args': {'date': '2026-13-01'}}]},
            {'tools': [{'name': 'search_sessions', 'args': {'date': '01/11/2026'}}]},
            {'tools': [{'name': 'search_sessions', 'args': {'activity': 'swim'}}]},
            {'tools': [{'name': 'search_sessions', 'args': {'sort': 'fastest'}}]},
            {'tools': [{'name': 'search_sessions', 'args': {'date': '2026-11-01',
                                                            'extra': 1}}]},
            {'tools': [{'name': 'search_sessions', 'args': {'date': '2026-11-01'}}] * 3},
            {'tools': [], 'extra': 1},
        )
        for plan in bad_plans:
            with self.subTest(plan=repr(plan)[:60]):
                result = self.call(http_post=plan_transport(plan))
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
                self.assertEqual(result['tools'], [])

    def test_non_json_and_speech_shaped_content_rejected(self):
        for content in ('not json', json.dumps({'speech': '11月1日那次骑了60分钟。'})):
            with self.subTest(content=content[:20]):
                envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
                post = Mock(return_value=(200, {}, json.dumps(envelope).encode()))
                result = self.call(http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
                self.assertEqual(result['tools'], [])

    def test_upstream_failures_map_to_upstream_error(self):
        for post in (Mock(return_value=(500, {}, b'{}')),
                     Mock(return_value=(200, {}, b'not json')),
                     Mock(side_effect=TimeoutError())):
            with self.subTest(post=post):
                result = self.call(http_post=post)
                self.assertEqual(result['llm_status'], 'upstream_error')
                self.assertEqual(result['tools'], [])


class SearchSessionsTests(unittest.TestCase):
    def entries(self):
        def entry(started, duration, activity='ride', distance=1000.0):
            when = datetime.fromisoformat(started.replace('Z', '+00:00'))
            return {'when': when, 'activity': activity,
                    'summary': {'duration_s': duration, 'avg_hr': 130, 'max_hr': 140,
                                'distance_m': distance}}
        return [entry('2026-11-01T08:00:00Z', 3600),
                entry('2026-11-05T08:00:00Z', 2700),
                entry('2026-11-05T09:00:00Z', 600, activity='run')]

    def test_date_matches_local_calendar_day(self):
        # 2026-11-01T08:00:00Z is 2026-11-01 16:00 in Asia/Shanghai (UTC+8).
        found = ride_agent.search_sessions(self.entries(), {'date': '2026-11-01'})
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]['started_at'].startswith('2026-11-01'))
        self.assertIn('+', found[0]['started_at'])  # local offset, not Z

    def test_longest_sorts_by_duration_descending(self):
        found = ride_agent.search_sessions(self.entries(), {'sort': 'longest'})
        self.assertEqual([item['summary']['duration_s'] for item in found], [3600, 2700, 600])

    def test_default_sort_is_latest_first(self):
        found = ride_agent.search_sessions(self.entries(), {'sort': 'latest'})
        self.assertEqual([item['summary']['duration_s'] for item in found], [600, 2700, 3600])

    def test_activity_filter(self):
        found = ride_agent.search_sessions(self.entries(), {'activity': 'run'})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['summary']['duration_s'], 600)

    def test_limit_and_no_match(self):
        many = self.entries() * 3
        self.assertEqual(len(ride_agent.search_sessions(many, {'sort': 'latest'})), 3)
        self.assertEqual(ride_agent.search_sessions(self.entries(), {'date': '2020-01-01'}), [])


class MatchedFactsTests(unittest.TestCase):
    """ride_chat matched_sessions projection: validated, numbers registered."""

    def matched(self):
        return [{'started_at': '2026-11-01T08:00:00+08:00',
                 'summary': {'duration_s': 3600, 'avg_hr': 130, 'max_hr': 140,
                             'distance_m': 25200.0}}]

    def chat(self, matched, question='我11月1日那次骑了多久？', **kwargs):
        kwargs.setdefault('api_key', 'synthetic-key')
        return ride_chat.build_ride_chat(question, session=None,
                                         matched_sessions=matched, **kwargs)

    def facts_of(self, post):
        return json.loads(json.loads(post.call_args.args[0].data)['messages'][1]['content'])

    def test_projection_and_verified_speech(self):
        post = speech_transport('11月1日那次骑了60分钟，平均心率130 bpm，25.2公里。')
        result = self.chat(self.matched(), http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        facts = self.facts_of(post)['matched_sessions']
        self.assertEqual(facts, [{'date': '11月1日', 'duration_min': 60,
                                  'avg_hr': 130, 'distance_km': 25.2}])

    def test_unregistered_matched_number_rejected(self):
        post = speech_transport('11月1日那次骑了61分钟。')
        result = self.chat(self.matched(), http_post=post)
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')

    def test_malformed_matched_rejected_without_upstream(self):
        bad = (None, 'matched', 42, [], [{}] * 4,
               [{'started_at': '2026-11-01T08:00:00+08:00'}],
               [{'started_at': 'not-a-date', 'summary': {}}],
               [{'started_at': '2026-11-01T08:00:00+08:00', 'summary': {}, 'x': 1}])
        for matched in bad:
            if matched is None:
                continue  # None means "not provided", legal
            with self.subTest(matched=repr(matched)[:50]):
                post = Mock(side_effect=AssertionError('no network'))
                result = self.chat(matched, http_post=post)
                self.assertEqual(result['llm_status'], 'rejected_bad_schema')
                post.assert_not_called()

    def test_none_matched_leaves_no_trace(self):
        post = speech_transport('这个我没记到。')
        result = self.chat(None, question='我累计骑了多少？', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertNotIn('matched_sessions', self.facts_of(post))


class PlannerWiringTests(unittest.TestCase):
    """App-level: hint questions pay one planner call; failures degrade honestly."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = coach_server.App(
            store=SimpleNamespace(path=Path(self.temp.name) / 'coach.sqlite3'),
            llm_post=Mock(side_effect=AssertionError('replaced per test')))
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def save(self, identifier, started, duration=3600, avg_hr=130):
        begin = datetime.fromisoformat(started.replace('Z', '+00:00'))
        ended = begin.timestamp() + duration
        end_iso = datetime.fromtimestamp(ended, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        memory = {'schema_version': 'luma-ride-memory/3', 'id': identifier,
                  'started_at': started, 'ended_at': end_iso, 'samples_hz': 1,
                  'series': [{'t': 0, 'hr': avg_hr}, {'t': duration, 'hr': avg_hr}],
                  'summary': {'duration_s': duration, 'avg_hr': avg_hr, 'max_hr': avg_hr,
                              'time_in_zone_s': None, 'zone_target': None, 'gaps': 0,
                              'alerts': 0, 'avg_cadence': None, 'avg_power': None,
                              'max_power': None, 'distance_m': 25200.0,
                              'avg_speed_mps': 7.0, 'max_speed_mps': 9.0},
                  'coach_notes': [], 'persona_version': 'ride-persona/19',
                  'source': 'ble_gatt_0x180d'}
        self.app.ride_dir.mkdir(parents=True, exist_ok=True)
        (self.app.ride_dir / (identifier + '.json')).write_text(
            json.dumps(memory, ensure_ascii=False), encoding='utf-8')

    def two_call_transport(self, plan, speech, calls):
        def http_post(request, timeout):
            body = request.data.decode('utf-8')
            calls.append(body)
            if '\\"task\\": \\"plan\\"' in body or '"task": "plan"' in body:
                content = json.dumps(plan, ensure_ascii=False)
            else:
                content = json.dumps({'speech': speech}, ensure_ascii=False)
            envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
            return 200, {'Content-Type': 'application/json'}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')
        return http_post

    def test_hint_question_plans_then_answers_with_matched_facts(self):
        self.save('alpha', '2026-11-01T08:00:00Z')
        self.save('beta', '2026-11-05T08:00:00Z', duration=2700, avg_hr=125)
        calls = []
        self.app.llm_post = self.two_call_transport(
            {'tools': [{'name': 'search_sessions', 'args': {'date': '2026-11-01'}}]},
            '11月1日那次骑了60分钟，平均心率130 bpm。', calls)
        result = self.app._chat_ride({'question': '我11月1日那次骑了多久？'})
        self.assertEqual((result['lane'], result['speech_status']), ('personal', 'ok'))
        self.assertEqual(result['speech'], '11月1日那次骑了60分钟，平均心率130 bpm。')
        self.assertEqual(len(calls), 2)  # one plan call + one answer call
        answer_facts = json.loads(json.loads(calls[1])['messages'][1]['content'])
        self.assertEqual(answer_facts['matched_sessions'],
                         [{'date': '11月1日', 'duration_min': 60, 'avg_hr': 130,
                           'distance_km': 25.2}])

    def test_planner_failure_degrades_to_plain_bundle(self):
        self.save('alpha', '2026-11-01T08:00:00Z')
        calls = []
        # Planner returns malformed JSON; the answer must still go out, from the
        # base bundle only, with no matched_sessions.
        self.app.llm_post = self.two_call_transport('not json', '这个我没记到。', calls)
        result = self.app._chat_ride({'question': '我11月1日那次骑了多久？'})
        self.assertEqual(result['speech_status'], 'ok')
        answer_facts = json.loads(json.loads(calls[-1])['messages'][1]['content'])
        self.assertNotIn('matched_sessions', answer_facts)

    def test_planner_disabled_by_env(self):
        self.save('alpha', '2026-11-01T08:00:00Z')
        calls = []
        self.app.llm_post = self.two_call_transport(
            {'tools': []}, '这个我没记到。', calls)
        with patch.dict(os.environ, {'AGENT_PLANNER': 'off'}):
            result = self.app._chat_ride({'question': '我11月1日那次骑了多久？'})
        self.assertEqual(result['speech_status'], 'ok')
        self.assertEqual(len(calls), 1)  # no planner call at all

    def test_non_hint_question_never_plans(self):
        self.save('alpha', '2026-11-01T08:00:00Z')
        calls = []
        self.app.llm_post = self.two_call_transport(
            {'tools': []}, '累计记录了60分钟。', calls)
        result = self.app._chat_ride({'question': '我累计骑了多久？'})
        self.assertEqual(result['speech_status'], 'ok')
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
