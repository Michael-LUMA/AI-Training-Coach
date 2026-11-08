"""Offline contracts for Mark's Chinese weekly memories, route and browser card.

中文摘要：合成会话、注入 HTTP、Node 假浏览器；不调用云端、BLE 或付费服务。
"""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import coach_server as server
import ride_weekly as weekly
from action_cards import ActionError
from ride_memory import validate_session
from tests import test_ride_ui as ui


def memory(identifier='ride-current', started='2026-11-04T12:00:00Z', *, duration=3600, hr=120, source='ble_gatt_0x180d'):
    """Return a complete validated v1 memory with synthetic, consistent samples."""
    end = datetime.fromisoformat(started.replace('Z', '+00:00')) + timedelta(seconds=duration)
    points = [{'t': 0, 'hr': hr}] if hr is not None else []
    if hr is not None and duration > 0:
        points.append({'t': duration, 'hr': hr})
    return validate_session({'schema_version': 'luma-ride-memory/1', 'id': identifier,
        'started_at': started, 'ended_at': end.isoformat(), 'samples_hz': 1,
        'series': points, 'summary': {'duration_s': duration, 'avg_hr': hr, 'max_hr': hr,
            'time_in_zone_s': None, 'zone_target': None, 'gaps': 0, 'alerts': 0},
        'coach_notes': ['private note never for cloud'], 'persona_version': 'ride-persona/14', 'source': source})


def transport(speech='这周骑了1次，记录60分钟。', *, draft=None, status=200, finish='stop'):
    envelope = {'choices': [{'finish_reason': finish, 'message': {
        'content': json.dumps({'speech': speech} if draft is None else draft, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 12}}
    return Mock(return_value=(status, {}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')))


class RideWeeklyModuleTests(unittest.TestCase):
    def review(self, text, current=None, previous=None):
        return weekly.build_ride_weekly([memory()] if current is None else current,
                                       [] if previous is None else previous,
                                       api_key='synthetic-key', http_post=transport(text))

    def test_exact_aggregation_differences_and_hr_list_not_mean(self):
        current = [memory(hr=100, duration=3600), memory('ride-second', hr=140, duration=3840),
                   memory('ride-missing', hr=None, duration=0)]
        previous = [memory('ride-previous', duration=3000)]
        before = copy.deepcopy((current, previous))
        facts = weekly.aggregate_ride_weeks(current, previous)
        self.assertEqual(facts['week'], {'count': 3, 'total_duration_s': 7440,
            'total_duration_min': 124, 'avg_hr': [100, 140, None],
            'avg_hr_range': {'low': 100, 'high': 140}})
        self.assertEqual(facts['previous_week']['count'], 1)
        self.assertEqual(facts['differences'], {'count_diff': 2, 'total_duration_diff_s': 4440, 'duration_diff_min': 74})
        self.assertEqual((current, previous), before)
        self.assertNotIn('average_hr', facts['week'])

    def test_fractional_seconds_sum_once_before_whole_second_rounding(self):
        facts = weekly.aggregate_ride_weeks([memory(duration=60.4), memory('ride-b', duration=60.4)],
                                             [memory('ride-p', duration=120.1)])
        self.assertEqual(facts['week']['total_duration_s'], 121)
        self.assertEqual(facts['differences']['total_duration_diff_s'], 1)
        self.assertIs(type(facts['differences']['total_duration_diff_s']), int)
        self.assertEqual(facts['week']['total_duration_min'], 2)

    def test_no_previous_comparison_is_absent(self):
        facts = weekly.aggregate_ride_weeks([memory()], [])
        self.assertIsNone(facts['previous_week'])
        self.assertIsNone(facts['differences'])

    def test_monday_midnight_sunday_end_and_next_monday_excluded(self):
        stamps = ['2026-11-01T23:59:59Z', '2026-11-02T00:00:00Z',
                  '2026-11-08T23:59:59Z', '2026-11-09T00:00:00Z', '2026-10-25T23:59:59Z']
        items = [memory('ride-' + str(i), value) for i, value in enumerate(stamps)]
        current, previous = weekly.split_week_sessions(items, date(2026, 11, 2), tz=timezone.utc)
        self.assertEqual([m['id'] for m in current], ['ride-1', 'ride-2'])
        self.assertEqual([m['id'] for m in previous], ['ride-0'])

    def test_offset_timestamps_compare_instants_not_iso_prefixes(self):
        zone = timezone(timedelta(hours=8))
        items = [memory('ride-start', '2026-11-01T16:00:00Z'),
                 memory('ride-before', '2026-11-02T06:59:59+15:00'),
                 memory('ride-end', '2026-11-08T15:59:59Z'),
                 memory('ride-next', '2026-11-09T08:00:00+08:00')]
        current, previous = weekly.split_week_sessions(items, date(2026, 11, 2), tz=zone)
        self.assertEqual([m['id'] for m in current], ['ride-start', 'ride-end'])
        self.assertEqual([m['id'] for m in previous], ['ride-before'])
        equivalent = [memory('ride-a', '2026-11-02T00:00:00+08:00'),
                      memory('ride-b', '2026-11-01T16:00:00Z')]
        self.assertEqual(len(weekly.split_week_sessions(equivalent, date(2026, 11, 2), tz=zone)[0]), 2)

    def test_no_data_never_calls_llm_even_with_key_and_previous(self):
        post = Mock(side_effect=AssertionError('No cloud for zero rides'))
        for previous in ([], [memory()]):
            result = weekly.build_ride_weekly([], previous, api_key='synthetic-key', http_post=post)
            self.assertEqual(result, {'llm_status': 'no_data', 'draft': None, 'usage': None, 'fallback': 'template'})
        post.assert_not_called()

    def test_not_configured_is_explicit_and_does_not_read_env(self):
        post = Mock(side_effect=AssertionError('No cloud without explicit key'))
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'not-to-be-read'}):
            for key in (None, '', '  '):
                result = weekly.build_ride_weekly([memory()], [], api_key=key, http_post=post)
                self.assertEqual(result['llm_status'], 'not_configured')
        post.assert_not_called()

    def test_transport_projection_registered_minutes_usage_and_input_isolation(self):
        current = [memory(duration=7440, hr=131)]
        before = copy.deepcopy(current)
        post = transport('这周骑了1次，记录124分钟，平均心率131 bpm。')
        result = weekly.build_ride_weekly(current, [], api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual(result['usage'], {'prompt_tokens': 11, 'completion_tokens': 12})
        self.assertIsNone(result['fallback'])
        self.assertEqual(current, before)
        request = post.call_args.args[0]
        self.assertEqual(request.full_url, weekly.ENDPOINT)
        self.assertEqual(post.call_args.kwargs, {'timeout': weekly.TIMEOUT_S})
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], weekly.MODEL)
        self.assertLessEqual(payload['max_tokens'], 600)
        facts = json.loads(payload['messages'][1]['content'])
        self.assertEqual(facts['week']['total_duration_min'], 124)
        self.assertIsNone(facts['previous_week'])
        for forbidden in ('private note', 'ride-current', 'series', 'started_at', 'persona_version'):
            self.assertNotIn(forbidden, payload['messages'][1]['content'])

    def test_unknown_arabic_chinese_and_wrong_dimension_numbers_reject_whole_draft(self):
        for text in ('这周骑了999次。', '这周骑了九百九十九次。', '我看到平均心率3600 bpm。', '这周记录120分钟。'):
            with self.subTest(text=text):
                result = self.review(text)
                self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')
                self.assertIsNone(result['draft'])
                self.assertEqual(result['fallback'], 'template')

    def test_registered_chinese_minutes_and_differences_pass(self):
        self.assertEqual(self.review('这周骑了一次，记录六十分钟。')['llm_status'], 'ok')
        result = self.review('这周骑了2次，比上周多1次，时长多60分钟。',
                             current=[memory(), memory('ride-b')], previous=[memory('ride-p')])
        self.assertEqual(result['llm_status'], 'ok')

    def test_mixed_sources_exclude_simulation_in_both_windows(self):
        current = [memory(), memory('ride-fake', duration=9990, hr=199, source='simulated')]
        previous = [memory('ride-real-p', duration=1800), memory('ride-fake-p', duration=8880, source='simulated')]
        facts = weekly.aggregate_ride_weeks(current, previous)
        self.assertEqual(facts['excluded_simulated'], 2)
        self.assertEqual(facts['week']['count'], 1)
        self.assertEqual(facts['week']['total_duration_s'], 3600)
        self.assertEqual(facts['previous_week']['total_duration_s'], 1800)
        self.assertEqual(self.review('模拟剔除了2次。', current, previous)['llm_status'], 'ok')
        self.assertEqual(self.review('这周记录9990秒。', current, previous)['llm_status'], 'rejected_unverifiable_number')

    def test_mixed_current_simulated_only_and_previous_real_becomes_no_data(self):
        post = Mock(side_effect=AssertionError('No cloud after filtering'))
        result = weekly.build_ride_weekly([memory(source='simulated')], [memory('ride-real')], api_key='key', http_post=post)
        self.assertEqual(result['llm_status'], 'no_data')
        post.assert_not_called()

    def test_all_simulated_requires_exact_prefix_and_registers_rule(self):
        current = [memory(source='simulated')]
        post = transport('模拟记录：这周骑了1次，记录60分钟。')
        result = weekly.build_ride_weekly(current, [], api_key='synthetic-key', http_post=post)
        self.assertEqual(result['llm_status'], 'ok')
        facts = json.loads(json.loads(post.call_args.args[0].data)['messages'][1]['content'])
        self.assertEqual(facts['speech_rule'], '必须以「模拟记录：」开头')
        self.assertEqual(facts['excluded_simulated'], 0)
        for text in ('这周骑了1次。', '这周是模拟记录，骑了1次。', '合成记录：骑了1次。'):
            self.assertEqual(self.review(text, current)['llm_status'], 'rejected_unsafe_speech')

    def test_schema_errors_and_unsafe_effect_claim_discard_draft(self):
        for draft in ({'speech': ''}, {'speech': 'text', 'extra': 1}, {'speech': ['text']}, {'speech': 'a' * 301}):
            result = weekly.build_ride_weekly([memory()], [], api_key='key', http_post=transport(draft=draft))
            self.assertEqual(result['llm_status'], 'rejected_bad_schema')
        self.assertEqual(self.review('我看到你变强了。')['llm_status'], 'rejected_unsafe_speech')
        item = memory();item['summary']['avg_hr'] = 999
        post = Mock()
        self.assertEqual(weekly.build_ride_weekly([item], [], api_key='key', http_post=post)['llm_status'], 'rejected_bad_schema')
        post.assert_not_called()

    def test_weekly_average_hr_claim_rejected_even_when_value_is_registered(self):
        # With several rides, any weekly "平均" claim is never a fact; rejected
        # even when the value matches a registered ride HR, whatever the wording.
        pair = [memory(hr=131), memory('ride-second', hr=140)]
        for text in ('这周骑了2次，总共120分钟，平均心率131，挺稳的。',
                     '心率平均在131左右，这周骑了2次，总共120分钟。',
                     '这周骑了2次，总共120分钟，心率波动挺大，平均在131左右。',
                     '这周骑了2次，总共120分钟，平均每次60分钟。'):
            self.assertEqual(weekly.build_ride_weekly(pair, [], api_key='key',
                             http_post=transport(text))['llm_status'], 'rejected_unverifiable_number')
        # A single-ride week may cite that ride's registered avg_hr.
        self.assertEqual(weekly.build_ride_weekly([memory(hr=131)], [], api_key='key',
                         http_post=transport('这周骑了1次，一共60分钟，平均心率131。'))['llm_status'], 'ok')

    def test_hr_range_wording_registered_and_accepted_for_multi_ride_weeks(self):
        pair = [memory(hr=120), memory('ride-second', hr=148)]
        result = weekly.build_ride_weekly(pair, [], api_key='key',
                  http_post=transport('这周骑了2次，总共120分钟，心率在120到148之间。'))
        self.assertEqual(result['llm_status'], 'ok')
        # A range bound that is not the observed min/max is still rejected.
        self.assertEqual(weekly.build_ride_weekly(pair, [], api_key='key',
                         http_post=transport('这周骑了2次，总共120分钟，心率在118到148之间。'))['llm_status'],
                         'rejected_unverifiable_number')

    def test_timeout_bad_envelope_non_200_and_oversize_have_no_retry(self):
        for exception in (TimeoutError(), OSError(), ValueError()):
            post = Mock(side_effect=exception)
            self.assertEqual(weekly.build_ride_weekly([memory()], [], api_key='key', http_post=post)['llm_status'], 'upstream_error')
            post.assert_called_once()
        for response in ((503, {}, b'{}'), (200, {}, b'not json'), (200, {}, b'x' * (weekly.MAX_RESPONSE_BYTES + 1))):
            self.assertEqual(weekly.build_ride_weekly([memory()], [], api_key='key', http_post=Mock(return_value=response))['llm_status'], 'upstream_error')

    def test_weekly_template_snapshots_positive_negative_equal_zero_and_simulated(self):
        agg = lambda count, seconds: {'count': count, 'total_duration_s': seconds}
        snapshots = [
            (agg(3, 7440), None, '这周骑了3次，一共124分钟。'),
            (agg(3, 7440), agg(1, 3600), '这周骑了3次，一共124分钟。比上周多2次，时长多64分钟。'),
            (agg(1, 3600), agg(3, 7440), '这周骑了1次，一共60分钟。比上周少2次，时长少64分钟。'),
            (agg(1, 3600), agg(1, 3600), '这周骑了1次，一共60分钟。比上周多0次，时长多0分钟。'),
            (agg(0, 0), agg(3, 7440), '这周还没骑车，随时开骑我都在。'),
            ({**agg(1, 61), 'source': 'simulated'}, None, '模拟记录：这周骑了1次，一共1分钟。'),
        ]
        for week, previous, expected in snapshots:
            self.assertEqual(weekly.weekly_template(week, previous), expected)


class RideWeeklyRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        store = SimpleNamespace(path=Path(self.temp.name) / 'coach.db')
        self.post = transport()
        self.app = server.App(store=store, llm_post=self.post, today=lambda: date(2026, 11, 7))
        self.addCleanup(patch.stopall)
        patch('ride_weekly._http_post', side_effect=AssertionError('No real cloud')).start()
        patch.dict(os.environ, {'DASHSCOPE_API_KEY': ''}).start()

    def request(self, payload=None, *, path=None, headers=None, method='POST'):
        path = path or server.RIDE_WEEKLY
        body = json.dumps({'week_start': '2026-11-02'} if payload is None else payload).encode()
        head = {'Host': '127.0.0.1:8794', 'Origin': 'http://127.0.0.1:8794',
                'X-Coach-Token': self.app.token, 'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        head.update(headers or {})
        return self.app.dispatch(method, path, head, body)

    def save(self, item):
        self.app.ride_dir.mkdir(parents=True, exist_ok=True)
        (self.app.ride_dir / (item['id'] + '.json')).write_text(json.dumps(item), encoding='utf-8')

    def test_post_whitelist_no_data_shape_template_no_directory_creation(self):
        self.assertEqual(server.RIDE_WEEKLY, '/api/coach/ride/weekly')
        result = self.request()
        self.assertEqual(result, {'speech_status': 'no_data', 'speech': '这周还没骑车，随时开骑我都在。',
            'fallback': 'template', 'usage': None, 'week': {'count': 0, 'total_duration_s': 0}, 'previous_week': None})
        self.assertFalse(self.app.ride_dir.exists());self.post.assert_not_called()

    def test_date_and_body_validation_reject_before_cloud(self):
        invalid = [{}, [], {'week_start': None}, {'week_start': '2026-11-02', 'extra': 1},
                   {'week_start': '2026-11-03'}, {'week_start': '2026-11-09'},
                   {'week_start': '2026-02-30'}, {'week_start': '20261102'},
                   {'week_start': '0001-01-01'}, {'week_start': '9999-12-27'}]
        for payload in invalid:
            self.app._ride_requests['POST'].clear()
            with self.subTest(payload=payload), self.assertRaises(ActionError) as error:
                self.request(payload)
            self.assertEqual((error.exception.code, error.exception.status), ('invalid_input', 400))
        self.post.assert_not_called()
        self.assertFalse(self.app.ride_dir.exists())

    def test_host_origin_and_token_are_not_exempt(self):
        for headers, code in (({'Host': 'evil.invalid'}, 'host_denied'),
                              ({'Origin': 'https://evil.invalid'}, 'origin_denied'),
                              ({'Origin': None}, 'origin_denied'),
                              ({'X-Coach-Token': ''}, 'token_denied'),
                              ({'Sec-Fetch-Site': 'cross-site'}, 'origin_denied')):
            with self.subTest(headers=headers), self.assertRaises(ActionError) as error:
                self.request(headers=headers)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(error.exception.status, 403)
        self.post.assert_not_called()

    def test_get_not_whitelisted_and_fixed_length_json_required(self):
        for kwargs, code in (({'method': 'GET'}, 'not_found'),
                             ({'headers': {'Content-Type': 'text/plain'}}, 'invalid_body'),
                             ({'headers': {'Transfer-Encoding': 'chunked'}}, 'invalid_body'),
                             ({'headers': {'Content-Length': str(server.MAX_BODY_BYTES + 1)}}, 'body_too_large')):
            with self.subTest(kwargs=kwargs), self.assertRaises(ActionError) as error:
                self.request(**kwargs)
            self.assertEqual(error.exception.code, code)
        self.post.assert_not_called()

    def test_shares_post_budget_with_upload_and_review_get_independent(self):
        self.request(memory(), path=server.RIDE_POST)
        for _ in range(9):
            self.request()
        with self.assertRaises(ActionError) as error:
            self.request({'session_id': 'ride-current'}, path=server.RIDE_REVIEW)
        self.assertEqual((error.exception.code, error.exception.status), ('rate_limited', 429))
        result = self.request(path=server.RIDE_LIST, method='GET')
        self.assertEqual(len(result['sessions']), 1)

    def test_disk_snapshot_tolerates_invalid_files_and_only_returns_local_numbers(self):
        self.save(memory())
        self.save(memory('ride-previous', '2026-10-28T12:00:00Z', duration=1800))
        self.save(memory('ride-outside', '2026-10-01T12:00:00Z'))
        directory = self.app.ride_dir
        (directory / 'broken.json').write_text('{broken', encoding='utf-8')
        (directory / 'unknown.json').write_text(json.dumps(memory('wrong-id')), encoding='utf-8')
        (directory / 'oversize.json').write_bytes(b' ' * (server.MAX_RIDE_BODY_BYTES + 1))
        (directory / 'folder.json').mkdir()
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        result = self.request()
        self.assertEqual(result['week'], {'count': 1, 'total_duration_s': 3600})
        self.assertEqual(result['previous_week'], {'count': 1, 'total_duration_s': 1800})
        self.assertEqual(result['speech_status'], 'not_configured')
        self.assertEqual(result['speech'], '这周骑了1次，一共60分钟。比上周多0次，时长多30分钟。')
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}, before)
        self.post.assert_not_called()

    def test_server_boundary_uses_aware_instants_in_local_calendar(self):
        local = datetime.combine(date(2026, 11, 2), datetime.min.time()).astimezone()
        for name, instant in (('at', local), ('before', local - timedelta(seconds=1)),
                              ('last', local + timedelta(days=7) - timedelta(seconds=1)),
                              ('next', local + timedelta(days=7))):
            stamp = instant.astimezone(timezone(timedelta(hours=-5))).isoformat()
            self.save(memory('ride-' + name, stamp))
        result = self.request()
        self.assertEqual(result['week']['count'], 2)
        self.assertEqual(result['previous_week']['count'], 1)

    def test_provider_call_is_outside_lock_env_is_injected_and_usage_preserved(self):
        self.save(memory())
        original = self.post
        def post(request, *, timeout):
            self.assertFalse(self.app._ride_lock.locked())
            self.assertTrue(self.app._ride_lock.acquire(blocking=False))
            self.app._ride_lock.release()
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-key')
            return original(request, timeout=timeout)
        self.app.llm_post = post
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.request()
        self.assertEqual(result['speech_status'], 'ok')
        self.assertEqual(result['speech'], '这周骑了1次，记录60分钟。')
        self.assertIsNone(result['fallback'])
        self.assertEqual(result['usage'], {'prompt_tokens': 11, 'completion_tokens': 12})
        self.assertEqual(set(result), {'speech_status', 'speech', 'fallback', 'usage', 'week', 'previous_week'})

    def test_rejected_cloud_draft_has_template_but_never_fabricates_week_numbers(self):
        self.save(memory())
        self.app.llm_post = transport('这周骑了999次。')
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'synthetic-key'}):
            result = self.request()
        self.assertEqual(result['speech_status'], 'rejected_unverifiable_number')
        self.assertEqual(result['speech'], '这周骑了1次，一共60分钟。')
        self.assertEqual(result['week'], {'count': 1, 'total_duration_s': 3600})
        self.assertEqual(result['fallback'], 'template')

    def test_route_mixed_sources_and_all_simulated_template_labelling(self):
        self.save(memory(source='simulated'))
        result = self.request()
        self.assertTrue(result['speech'].startswith('模拟记录：'))
        self.save(memory('ride-real', duration=1800))
        result = self.request()
        self.assertEqual(result['week'], {'count': 1, 'total_duration_s': 1800})
        self.assertEqual(result['speech'], '这周骑了1次，一共30分钟。')


class RideWeeklyBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ui.ROOT / 'static/ride.html').read_text(encoding='utf-8')

    def run_js(self, scenario, **options):
        if not ui.NODE:
            self.skipTest('Node runtime required for offline JS tests')
        harness = ui.HARNESS.replace('  // TEST_REVIEW_TRANSPORT', r"""
  if(url==='/api/coach/ride/weekly'){
    if(options.weeklyFail)throw new Error('offline');
    return {ok:true,status:200,json:async()=>options.weekly || {speech_status:'not_configured',speech:null,fallback:'template',usage:null}};
  }
  // TEST_REVIEW_TRANSPORT""")
        result = subprocess.run([ui.NODE, '-e', harness], input=json.dumps({'html': self.html, 'scenario': scenario, 'options': options}),
                                capture_output=True, text=True, encoding='utf-8', cwd=ui.ROOT, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_empty_history_disables_button_with_no_autoplay_or_weekly_request(self):
        result = self.run_js("return {disabled:$('ride-weekly-listen').disabled,text:$('ride-weekly-summary').textContent,spoken:_spoken,calls:_calls};")
        self.assertTrue(result['disabled']);self.assertEqual(result['text'], '还没有记录')
        self.assertEqual(result['spoken'], [])
        self.assertFalse(any(call['url'].endswith('/weekly') for call in result['calls']))

    def test_local_calendar_boundaries_totals_and_button_state(self):
        result = self.run_js("""
          const item=(id,date,duration)=>({id,started_at:date.toISOString(),source:'ble_gatt_0x180d',summary:{duration_s:duration}});
          const history=[item('start',new Date(2026,10,2,0,0,0),3600),item('last',new Date(2026,10,8,23,59,59),3840),
            item('previous',new Date(2026,10,1,23,59,59),1800),item('next',new Date(2026,10,9,0,0,0),9999)];
          const facts=renderWeekly(history,new Date(2026,10,8,12));
          return {facts,start:weeklyWeekStart,disabled:$('ride-weekly-listen').disabled,text:$('ride-weekly-summary').textContent};
        """)
        self.assertEqual(result['start'], '2026-11-02')
        self.assertEqual(result['facts']['week'], {'count': 2, 'total_duration_s': 7440})
        self.assertEqual(result['facts']['previous_week'], {'count': 1, 'total_duration_s': 1800})
        self.assertFalse(result['disabled'])
        self.assertEqual(result['text'], '这周骑了2次，一共124分钟。比上周多1次，时长多94分钟。')

    def test_weekly_template_js_matches_python_snapshots(self):
        cases = [({'count': 0, 'total_duration_s': 0}, None),
                 ({'count': 3, 'total_duration_s': 7440}, {'count': 1, 'total_duration_s': 3600}),
                 ({'count': 1, 'total_duration_s': 61}, {'count': 3, 'total_duration_s': 7440})]
        for week, previous in cases:
            for source in ('ble_gatt_0x180d', 'simulated'):
                result = self.run_js('return weeklyTemplate(_options.week,_options.previous,_options.source);', week=week, previous=previous, source=source)
                self.assertEqual(result, weekly.weekly_template({**week, 'source': source}, previous))

    def test_click_posts_token_local_monday_renders_and_speaks_verified_speech(self):
        result = self.run_js("""
          renderWeekly([{id:'current',started_at:new Date(2026,10,4,12).toISOString(),source:'ble_gatt_0x180d',summary:{duration_s:3600}}],new Date(2026,10,7));
          await $('ride-weekly-listen').click();
          return {text:$('ride-weekly-speech').textContent,spoken:_spoken,calls:_calls,disabled:$('ride-weekly-listen').disabled};
        """, weekly={'speech_status': 'ok', 'speech': '这周骑了1次，记录60分钟。', 'fallback': None})
        self.assertEqual(result['text'], '这周骑了1次，记录60分钟。')
        self.assertEqual(result['spoken'], [result['text']]);self.assertFalse(result['disabled'])
        request = next(call for call in result['calls'] if call['url'].endswith('/weekly'))
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(json.loads(request['body']), {'week_start': '2026-11-02'})
        self.assertEqual(request['headers']['X-Coach-Token'], 'synthetic-ui-token')

    def test_cloud_fallback_or_network_failure_renders_local_template(self):
        for options in ({}, {'weeklyFail': True}, {'weekly': {'speech_status': 'rejected_unverifiable_number', 'speech': '999次', 'fallback': 'template'}}):
            result = self.run_js("""
              renderWeekly([{id:'ride',started_at:new Date(2026,10,4).toISOString(),source:'ble_gatt_0x180d',summary:{duration_s:7440}}],new Date(2026,10,7));
              await listenWeekly();return {text:$('ride-weekly-speech').textContent,spoken:_spoken};
            """, **options)
            self.assertEqual(result['text'], '这周骑了1次，一共124分钟。')
            self.assertEqual(result['spoken'], [result['text']])

    def test_simulated_filtering_and_old_history_only_zero_week(self):
        result = self.run_js("""
          const item=(id,source,day,duration)=>({id,source,started_at:new Date(2026,10,day,12).toISOString(),summary:{duration_s:duration}});
          const real=item('real','ble_gatt_0x180d',4,3600),fake=item('fake','simulated',5,9990);
          const mixed=renderWeekly([real,fake],new Date(2026,10,7));
          const simulated=renderWeekly([fake],new Date(2026,10,7));const label=$('ride-weekly-summary').textContent;
          renderWeekly([item('old','ble_gatt_0x180d',1,3600)],new Date(2026,10,7));
          return {mixed,simulated,label,zero:$('ride-weekly-summary').textContent,disabled:$('ride-weekly-listen').disabled};
        """)
        self.assertEqual(result['mixed']['week']['count'], 1)
        self.assertEqual(result['mixed']['week']['total_duration_s'], 3600)
        self.assertEqual(result['simulated']['week']['count'], 1)
        self.assertTrue(result['label'].startswith('模拟记录：'))
        self.assertEqual(result['zero'], '这周还没骑车，随时开骑我都在。')
        self.assertFalse(result['disabled'])

    def test_weekly_button_does_not_interrupt_active_ride_speech(self):
        result = self.run_js("""
          renderWeekly([{id:'ride',started_at:new Date(2026,10,4).toISOString(),source:'ble_gatt_0x180d',summary:{duration_s:3600}}],new Date(2026,10,7));
          begin();await listenWeekly();return {spoken:_spoken,text:$('ride-weekly-speech').textContent};
        """)
        self.assertEqual(result['spoken'], [])
        self.assertEqual(result['text'], '这周骑了1次，一共60分钟。')

    def test_new_weekly_card_strings_version_and_existing_ids_retained(self):
        for text in ('ride-persona/17', "name:'Mark'", '本周', '听 Mark 说这星期', '还没有记录',
                     'id="ride-weekly"', 'id="memory-card"', 'id="demo-start"', 'id="end-ride"',
                     '/api/coach/ride/weekly', 'weekly_no_rides:', 'weekly_current:', 'weekly_comparison:'):
            self.assertIn(text, self.html)
        self.assertNotIn('innerHTML', self.html)


if __name__ == '__main__':
    unittest.main()
