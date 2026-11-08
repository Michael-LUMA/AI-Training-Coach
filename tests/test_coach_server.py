"""Synthetic end-to-end dispatcher/handler tests; temporary DB, no bound sockets."""
import base64
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server as server
from action_cards import ActionError

KEY = 'synthetic-credential-1234'


class CoachServerTests(unittest.TestCase):
    def setUp(self):
        # API tests exercise real encryption without depending on a Windows
        # profile/DPAPI permission. Native DPAPI has its own keystore tests.
        from coach_keystore import MasterKeyBackend
        master = 'synthetic-server-test-master-key-32-bytes-minimum'
        key_env = patch.dict('os.environ', {'LUMA_COACH_MASTER_KEY': master})
        key_env.start(); self.addCleanup(key_env.stop)
        backend = patch('coach_keystore.DPAPIBackend',
                        side_effect=lambda: MasterKeyBackend(master))
        backend.start(); self.addCleanup(backend.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'coach/coach.sqlite3'
        self.calls = []
        self.replies = {}
        self.app = server.App(server.CoachStore(self.path), fetch=self.fetch, today=lambda: date(2026, 11, 6))
        self.guard = patch('coach_icu_client._fetch', side_effect=AssertionError('Real transport forbidden'))
        self.guard.start(); self.addCleanup(self.guard.stop)

    def fetch(self, req, *, timeout):
        self.calls.append(req)
        self.assertEqual(base64.b64decode(req.get_header('Authorization').split()[1]), ('API_KEY:' + KEY).encode())
        path = urlsplit(req.full_url).path
        default = {
            '/api/v1/athlete/0/activities': [],
            '/api/v1/activity/i1': {'id':'i1', 'type':'Ride', 'start_date':'2026-11-06T00:00:00Z', 'start_date_local':'2026-11-06T08:00:00'},
            '/api/v1/activity/i1/streams': [{'type':'time','data':[0,10,20]}, {'type':'watts','data':[100,200,100]}],
            '/api/v1/athlete/0/sport-settings/Ride': {'ftp':250, 'power_zones':[0,150,250]},
            '/api/v1/athlete/0/wellness/2026-11-06': {'resting_hr':52, 'sleep_secs':27000},
        }
        value = self.replies.get(path, default.get(path))
        if isinstance(value, tuple):
            return value
        return 200, {}, json.dumps(value).encode()

    def headers(self, method='GET', body=b''):
        result = {'Host':'127.0.0.1:8794', 'X-Coach-Token':self.app.token}
        if method == 'POST':
            result.update({'Origin':'http://127.0.0.1:8794','Content-Type':'application/json','Content-Length':str(len(body))})
        return result

    def request(self, path, payload=None):
        method = 'GET' if payload is None else 'POST'
        body = json.dumps(payload).encode() if payload is not None else b''
        return self.app.dispatch(method, path, self.headers(method, body), body)

    def connect(self):
        return self.request('/api/coach/connect', {'api_key':KEY})

    def error(self, code, call):
        with self.assertRaises(ActionError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_page_config_single_brand_and_no_store_creation(self):
        page, csp = self.request('/')
        config = self.request('/api/coach/config')
        self.assertEqual(config['brand_name'], server.BRAND_NAME)
        self.assertIn(b'<html lang="en">', page)
        self.assertNotIn(server.BRAND_NAME.encode(), page)
        self.assertNotIn(b'innerHTML', page)
        self.assertIn("script-src 'sha256-", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertFalse(config['connected'])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.calls)

    def test_page_routes_tolerate_query_strings(self):
        # A shared/bookmarked link with query params must still serve the page
        # publicly; raw-path routing used to token-deny or 404 these URLs.
        for path in ('/?autodemo=1', '/privacy?utm_source=x', '/terms?a=1&b=2'):
            with self.subTest(path=path):
                page, csp = self.request(path)
                self.assertIn(b'<html lang="en">', page)
                self.assertIn("script-src 'sha256-", csp)

    def test_save_private_key_no_network_and_persists_masked_only(self):
        result = self.connect()
        self.assertFalse(result['verified'])
        self.assertEqual(result['masked_key'], '****1234')
        self.assertNotIn(KEY, json.dumps(result))
        self.assertFalse(self.calls)
        self.assertEqual(server.CoachStore(self.path).get_key(), KEY)
        config = self.request('/api/coach/config')
        self.assertNotIn(KEY, json.dumps(config))
        # encrypted_at_rest must honestly report backend availability; when a
        # backend exists the stored bytes must not contain the plaintext key.
        from coach_keystore import KeyStore
        self.assertEqual(config['encrypted_at_rest'], KeyStore().status['encrypted_at_rest'])
        if config['encrypted_at_rest']:
            self.assertNotIn(KEY, self.path.read_bytes().decode('utf-8', 'ignore'))
        for key in ('a','abcd'):
            self.assertEqual(self.request('/api/coach/connect', {'api_key':key})['masked_key'], '****')

    def test_no_key_is_401(self):
        for path, payload in (('/api/coach/activities?days=14',None),('/api/coach/review',{'activity_id':'i1'})):
            error = self.error('auth_required', lambda: self.request(path, payload))
            self.assertEqual(error.status, 401)
        self.assertFalse(self.calls)

    def test_empty_activity_list_and_date_range(self):
        self.connect()
        self.assertEqual(self.request('/api/coach/activities?days=14'), {'activities':[]})
        query = parse_qs(urlsplit(self.calls[-1].full_url).query)
        self.assertEqual(query['oldest'], ['2026-10-24'])
        self.assertEqual(query['newest'], ['2026-11-06'])

    def test_activity_fields_whitelist(self):
        self.connect()
        self.replies['/api/v1/athlete/0/activities'] = [{'id':'i1','name':'<script>not executable</script>','private':'not returned'}]
        result = self.request('/api/coach/activities?days=1')
        self.assertNotIn('private', result['activities'][0])

    def test_full_review_pipeline_and_contract(self):
        self.connect()
        result = self.request('/api/coach/review', {'activity_id':'i1'})
        review = result['review']
        self.assertEqual(review['facts']['metrics']['average_power_w']['value'], 150)
        self.assertEqual(review['source']['kind'], 'intervals_icu')
        self.assertTrue(review['facts']['zones']['power'])
        self.assertIn('rule_inferences',review)
        self.assertIn('training_suggestion',review)
        self.assertEqual(review['ai_inferences'],[])
        self.assertEqual(review['status'],'draft')
        self.assertIsNone(review['confirmation'])
        self.assertNotIn(KEY,json.dumps(result))
        self.assertEqual(len(self.calls),4)

    def test_empty_or_missing_streams_degrade_optional_404(self):
        self.connect()
        for value in ([],(404,{},b'')):
            self.replies['/api/v1/activity/i1/streams'] = value
            self.replies['/api/v1/athlete/0/sport-settings/Ride'] = (404,{},b'')
            self.replies['/api/v1/athlete/0/wellness/2026-11-06'] = (404,{},b'')
            result = self.request('/api/coach/review',{'activity_id':'i1'})
            self.assertIsNone(result['review']['facts']['metrics']['average_power_w']['value'])
            self.assertIsNone(result['review']['facts']['zones']['power'])
            self.assertIn('insufficient data',' '.join(result['review']['facts']['messages']))
            self.assertTrue(result['warnings'])

    def test_no_activity_date_does_not_guess_wellness(self):
        self.connect()
        self.replies['/api/v1/activity/i1'] = {'type':'Ride'}
        result = self.request('/api/coach/review',{'activity_id':'i1'})
        self.assertTrue(any(w['field']=='wellness_date' for w in result['warnings']))
        self.assertFalse(any('/wellness/' in req.full_url for req in self.calls))

    def handler(self, method, path, payload=None, overrides=None):
        with patch.object(server,'ThreadingHTTPServer') as ctor:
            server.create_server(app=self.app)
            cls = ctor.call_args.args[1]
        handler = cls.__new__(cls)
        handler.command, handler.path = method, path
        body = json.dumps(payload).encode() if payload is not None else b''
        handler.headers = self.headers(method,body)
        handler.headers.update(overrides or {})
        handler.server = SimpleNamespace(server_port=8794)
        handler.connection = Mock()
        handler.rfile = Mock(); handler.rfile.read.return_value = body
        handler.wfile = io.BytesIO()
        handler.reply = Mock()
        handler.handle_request()
        return handler

    def test_boundary_blocks_before_read(self):
        for override, code in (({'Host':'evil.test'},'host_denied'),({'Origin':'https://evil.test'},'origin_denied'),
                               ({'Origin':None},'origin_denied'),({'Sec-Fetch-Site':'cross-site'},'origin_denied'),
                               ({'X-Coach-Token':'bad'},'token_denied'),({'X-Coach-Token':'密钥'},'token_denied'),
                               ({'Content-Length':'9000'},'body_too_large'),({'Transfer-Encoding':'chunked'},'invalid_body')):
            handler = self.handler('POST','/api/coach/connect',{'api_key':KEY},override)
            self.assertEqual(handler.reply.call_args.args[1]['error_code'],code)
            handler.rfile.read.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_sensitive_get_requires_token(self):
        self.connect()
        handler = self.handler('GET','/api/coach/activities?days=14',overrides={'X-Coach-Token':''})
        self.assertEqual(handler.reply.call_args.args[0],403)
        self.assertFalse(self.calls)

    def test_http_endpoints_and_sanitized_upstream_error(self):
        handler = self.handler('POST','/api/coach/connect',{'api_key':KEY})
        self.assertEqual(handler.reply.call_args.args[0],200)
        self.replies['/api/v1/activity/i1'] = (429,{'Retry-After':'60'},KEY.encode())
        handler = self.handler('POST','/api/coach/review',{'activity_id':'i1'})
        status, body = handler.reply.call_args.args
        self.assertEqual((status,body['error_code'],body['retry_after']),(429,'rate_limited','60'))
        self.assertNotIn(KEY,json.dumps(body))

    def test_invalid_json_body_and_queries_no_network(self):
        body = b'{"api_key":"x","api_key":"y"}'
        self.error('invalid_json',lambda:self.app.dispatch('POST','/api/coach/connect',self.headers('POST',body),body))
        for path in ('/api/coach/activities?days=0','/api/coach/activities?days=91',
                     '/api/coach/activities?days=1&days=2','/api/coach/activities?days=abc'):
            self.error('invalid_input',lambda:self.request(path))
        self.error('invalid_input',lambda:self.request('/api/coach/connect',{'api_key':KEY,'extra':True}))
        self.assertFalse(self.calls)

    def test_storage_failure_sanitized(self):
        with patch.object(self.app.store,'save_key',side_effect=sqlite3.OperationalError(KEY)):
            handler = self.handler('POST','/api/coach/connect',{'api_key':KEY})
        status,body = handler.reply.call_args.args
        self.assertEqual(status,503)
        self.assertNotIn(KEY,json.dumps(body))

    def test_factory_loopback_only_never_serves(self):
        with patch.object(server,'ThreadingHTTPServer') as ctor:
            result = server.create_server(app=self.app)
            self.assertEqual(ctor.call_args.args[0],('127.0.0.1',8794))
            result.serve_forever.assert_not_called()


    def test_legal_pages_public_offline_and_csp_hashes(self):
        import hashlib
        import re
        for path, title in (('/privacy', 'Privacy notice'), ('/terms', 'Terms of use')):
            with self.subTest(path=path):
                with patch.object(self.app.store, 'get_key', side_effect=AssertionError('Page must not read key')):
                    raw, csp = self.app.dispatch('GET', path, {'Host':'127.0.0.1:8794'})
                text = raw.decode('utf-8')
                self.assertIn('<html lang="en">', text)
                self.assertIn(title, text)
                self.assertIn("fetch('/api/coach/config'", text)
                self.assertIn('config.brand_name', text)
                self.assertIn('config.support_email', text)
                self.assertNotIn(server.BRAND_NAME, text)
                self.assertNotIn(server.SUPPORT_EMAIL, text)
                self.assertNotIn('innerHTML', text)
                self.assertNotRegex(text, r'<(?:script|link|img)[^>]+(?:src|href)=')
                for tag in ('script', 'style'):
                    for part in re.findall('<' + tag + '>(.*?)</' + tag + '>', text, re.S):
                        digest = base64.b64encode(hashlib.sha256(part.encode()).digest()).decode()
                        self.assertIn("'sha256-" + digest + "'", csp)
                self.assertIn("connect-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
        self.assertFalse(self.calls)
        self.assertFalse(self.path.exists())

    def test_legal_pages_keep_host_origin_fetch_and_method_guards(self):
        for path in ('/privacy', '/terms'):
            for override, code in (({'Host':'evil.test'}, 'host_denied'),
                                   ({'Origin':'https://evil.test'}, 'origin_denied'),
                                   ({'Sec-Fetch-Site':'cross-site'}, 'origin_denied')):
                headers = {'Host':'127.0.0.1:8794'}
                headers.update(override)
                self.error(code, lambda:self.app.dispatch('GET', path, headers))
            self.error('not_found', lambda:self.request(path, {}))
            handler = self.handler('GET', path, overrides={'X-Coach-Token':''})
            self.assertEqual(handler.reply.call_args.args[0], 200)
            self.assertIsInstance(handler.reply.call_args.args[1][0], bytes)
        self.assertFalse(self.calls)
        self.assertFalse(self.path.exists())

    def test_legal_config_brand_and_support_single_source(self):
        with patch.object(server, 'BRAND_NAME', 'Synthetic test brand'), patch.object(server, 'SUPPORT_EMAIL', 'pending@example.invalid'):
            result = self.request('/api/coach/config')
        self.assertEqual(result['brand_name'], 'Synthetic test brand')
        self.assertEqual(result['support_email'], 'pending@example.invalid')
        self.assertNotIn(KEY, json.dumps(result))
        self.assertFalse(self.calls)
        self.assertFalse(self.path.exists())

    def test_legal_content_matches_current_hosted_pilot(self):
        privacy = self.request('/privacy')[0].decode()
        terms = self.request('/terms')[0].decode()
        for text in ('no persistent activity-data cache', 'no in-app delete button', '.runtime/coach/',
                     'do not sell', 'train models', 'Intervals.icu API', 'backups', 'placeholder',
                     'shared demo slot', 'ephemeral', 'Railway'):
            self.assertIn(text, privacy)
        for text in ('not medical advice', 'risks of exercise', 'pending payment provider',
                     'refund', 'mandatory consumer rights', 'placeholder',
                     'shared demo slot', 'ephemeral'):
            self.assertIn(text, terms)

    def test_public_origin_retargets_host_and_origin_checks(self):
        env = patch.dict('os.environ', {'LUMA_COACH_PUBLIC_ORIGIN': 'https://coach.example.com'})
        with env:
            # Loopback Host is no longer accepted once deployed origin is set.
            with self.assertRaises(ActionError) as denied:
                self.request('/api/coach/config')
            self.assertEqual(denied.exception.code, 'host_denied')
            # The deployed hostname passes, GET without Origin, POST with matching Origin.
            page = self.app.dispatch('GET', '/api/coach/config',
                                     {'Host': 'coach.example.com'}, b'')
            self.assertIn('token', page)
            headers = {'Host': 'coach.example.com', 'Origin': 'https://coach.example.com',
                       'X-Coach-Token': self.app.token,
                       'Content-Type': 'application/json', 'Content-Length': '2'}
            with self.assertRaises(ActionError) as cross:
                self.app.dispatch('POST', '/api/coach/connect',
                                  {**headers, 'Origin': 'https://other.example.com'}, b'{}')
            self.assertEqual(cross.exception.code, 'origin_denied')

    def test_public_origin_misconfiguration_fails_fast(self):
        for bad in ('https://coach.example.com/path', 'https://user:pw@coach.example.com',
                    'ftp://coach.example.com', 'not-a-url', 'https://coach.example.com?q=1'):
            with patch.dict('os.environ', {'LUMA_COACH_PUBLIC_ORIGIN': bad}):
                with self.assertRaises(ValueError):
                    server.create_server(port=8794, app=self.app)

    def test_weekly_requires_monday_and_connection(self):
        with self.assertRaises(ActionError) as denied:
            self.request('/api/coach/weekly', {'week_start': '2026-11-02'})
        self.assertEqual(denied.exception.code, 'auth_required')
        self.connect()
        # Tuesday, garbage, and a future Monday (stub today is 2026-11-06).
        for bad in ('2026-11-03', 'not-a-date', '2026-11-09'):
            with self.subTest(bad=bad), self.assertRaises(ActionError) as rejected:
                self.request('/api/coach/weekly', {'week_start': bad})
            self.assertEqual(rejected.exception.code, 'invalid_input')
        result = self.request('/api/coach/weekly', {'week_start': '2026-11-02'})
        self.assertEqual(result['wellness'], 'not_fetched')
        self.assertEqual(result['weekly']['facts']['current']['activity_count'], 0)

    def test_weekly_bounds_previous_week_and_latest_reported_ctl(self):
        self.connect()
        self.replies['/api/v1/athlete/0/activities'] = [
            {'id': 'a1', 'start_date_local': '2026-10-28T07:00:00', 'moving_time': 1800, 'icu_training_load': 40},
            {'id': 'a2', 'start_date_local': '2026-11-04T07:00:00', 'moving_time': 3600, 'distance': 30000, 'icu_training_load': 60, 'icu_ctl': 45, 'icu_atl': 55},
            {'id': 'a3', 'start_date_local': '2026-11-05T07:00:00', 'moving_time': 2700, 'icu_training_load': 50, 'icu_ctl': 46, 'icu_atl': 54},
        ]
        result = self.request('/api/coach/weekly', {'week_start': '2026-11-02'})
        facts = result['weekly']['facts']
        self.assertEqual(facts['current']['activity_count'], 2)
        self.assertEqual(facts['current']['total_training_load'], 110.0)
        self.assertEqual(facts['previous']['activity_count'], 1)
        # Latest reported CTL/ATL inside the selected week, not the first.
        self.assertEqual(facts['reported_context'], {'ctl': 46.0, 'atl': 54.0})


    def test_page_includes_weekly_summary_controls_and_endpoint(self):
        page = self.request('/')[0].decode('utf-8')
        for text in ('id="week-start"', 'id="weekly"', 'id="weekly-result"',
                     '/api/coach/weekly'):
            with self.subTest(text=text):
                self.assertIn(text, page)


    def l2_reply(self, draft=None, *, status=200):
        """Synthetic provider JSON; no real key, socket or paid API."""
        if draft is None:
            draft = {"summary": "Average power was 150W.",
                     "observations": [{"text": "Inspect the recorded power.",
                                       "fact_refs": ["facts.metrics.average_power_w"]}],
                     "next_ride_note": "Record missing data if available."}
        return status, {}, json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(draft)}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45}}).encode()

    def test_l2_not_configured_returns_200_with_rule_draft_no_cloud(self):
        self.app.llm_post = Mock(side_effect=AssertionError('Cloud must not be called'))
        with patch.object(self.app.store, 'get_key', return_value=KEY), patch.dict(
                'os.environ', {'DASHSCOPE_API_KEY': ''}):
            handler = self.handler('POST', '/api/coach/review-l2', {'activity_id': 'i1'})
        status, result = handler.reply.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result['llm'], {'llm_status': 'not_configured', 'draft': None,
                                        'usage': None, 'fallback': 'rule_draft'})
        self.assertEqual(result['review']['facts']['metrics']['average_power_w']['value'], 150)
        self.assertIn('warnings', result)
        self.assertFalse(result['verified'])
        self.assertEqual(len(self.calls), 4)
        self.app.llm_post.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_l2_env_key_injected_transport_usage_and_no_secret_persistence(self):
        cloud_key = 'synthetic-dashscope-test-key'
        self.app.llm_post = Mock(return_value=self.l2_reply())
        with patch.object(self.app.store, 'get_key', return_value=KEY), patch.dict(
                'os.environ', {'DASHSCOPE_API_KEY': cloud_key}):
            handler = self.handler('POST', '/api/coach/review-l2', {'activity_id': 'i1'})
        status, result = handler.reply.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result['llm']['llm_status'], 'ok')
        self.assertEqual(result['llm']['usage'], {'prompt_tokens': 123, 'completion_tokens': 45})
        self.assertTrue(result['llm']['draft']['disclaimer'].startswith('AI-assisted draft. '))
        self.app.llm_post.assert_called_once()
        request = self.app.llm_post.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + cloud_key)
        self.assertEqual(self.app.llm_post.call_args.kwargs, {'timeout': 20})
        for secret in (KEY, cloud_key):
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, request.data.decode())
        self.assertFalse(self.path.exists())

    def test_l2_reuses_identical_rule_pipeline_and_does_not_mutate_rule_result(self):
        self.app.llm_post = Mock(return_value=self.l2_reply())
        with patch.object(self.app.store, 'get_key', return_value=KEY), patch.dict(
                'os.environ', {'DASHSCOPE_API_KEY': 'synthetic-dashscope-test-key'}):
            rule = self.request('/api/coach/review', {'activity_id': 'i1'})
            self.app.llm_post.assert_not_called()
            enhanced = self.request('/api/coach/review-l2', {'activity_id': 'i1'})
            self.request('/api/coach/demo-review', {})
        self.assertEqual({key: enhanced[key] for key in rule}, rule)
        # Byte-for-byte comparison retains IDs and timestamps; do not strip them.
        self.assertEqual(server.dumps(enhanced['review']).encode('utf-8'),
                         server.dumps(rule['review']).encode('utf-8'))
        self.assertIsNone(rule['review']['created_at'])
        self.assertIsNone(rule['review']['updated_at'])
        self.app.llm_post.assert_called_once()

    def test_l2_isolates_mutation_and_same_input_snapshot_across_app_instances(self):
        with patch.object(self.app.store, 'get_key', return_value=KEY):
            before = self.request('/api/coach/review', {'activity_id': 'i1'})
            original_bytes = server.dumps(before['review']).encode('utf-8')
            def mutating_engine(review, **kwargs):
                review['facts']['metrics']['average_power_w']['value'] = 99999
                review['facts']['messages'].append('synthetic injected mutation')
                return {'llm_status': 'upstream_error', 'draft': None,
                        'usage': None, 'fallback': 'rule_draft'}
            with patch.object(server, 'build_llm_review', side_effect=mutating_engine):
                enhanced = self.request('/api/coach/review-l2', {'activity_id': 'i1'})
            after = self.request('/api/coach/review', {'activity_id': 'i1'})
        self.assertEqual(server.dumps(enhanced['review']).encode('utf-8'), original_bytes)
        self.assertEqual(server.dumps(after['review']).encode('utf-8'), original_bytes)
        self.assertEqual(server.dumps(before['review']).encode('utf-8'), original_bytes)
        # A new App builds the same snapshot without a cache or reused object.
        self.app = server.App(server.CoachStore(self.path), fetch=self.fetch,
                              today=lambda: date(2026, 11, 6))
        with patch.object(self.app.store, 'get_key', return_value=KEY):
            rebuilt = self.request('/api/coach/review', {'activity_id': 'i1'})
        self.assertEqual(server.dumps(rebuilt['review']).encode('utf-8'), original_bytes)

    def test_rule_snapshot_identity_changes_with_facts_and_never_reuses_stale_data(self):
        with patch.object(self.app.store, 'get_key', return_value=KEY):
            before = self.request('/api/coach/review', {'activity_id': 'i1'})['review']
            self.replies['/api/v1/activity/i1/streams'] = [
                {'type': 'time', 'data': [0, 10, 20]},
                {'type': 'watts', 'data': [200, 200, 200]}]
            changed = self.request('/api/coach/review', {'activity_id': 'i1'})['review']
            self.replies.pop('/api/v1/activity/i1/streams')
            restored = self.request('/api/coach/review', {'activity_id': 'i1'})['review']
        self.assertNotEqual(before['id'], changed['id'])
        self.assertEqual(changed['facts']['metrics']['average_power_w']['value'], 200)
        self.assertEqual(server.dumps(before), server.dumps(restored))

    def test_l2_cloud_rejection_timeout_and_failure_keep_rule_200(self):
        draft = {'summary': 'Power was 99999W.', 'observations': [],
                 'next_ride_note': 'Inspect recorded data.'}
        cases = [(self.l2_reply(draft), None, 'rejected_unverifiable_number'),
                 (self.l2_reply({'summary': 'Missing fields'}), None, 'rejected_bad_schema'),
                 (self.l2_reply(status=429), None, 'upstream_error'),
                 (None, TimeoutError('synthetic-sensitive-provider-error'), 'upstream_error')]
        for response, error, expected in cases:
            with self.subTest(expected=expected, timeout=error is not None):
                self.app.llm_post = Mock(return_value=response, side_effect=error)
                with patch.object(self.app.store, 'get_key', return_value=KEY), patch.dict(
                        'os.environ', {'DASHSCOPE_API_KEY': 'synthetic-dashscope-test-key'}):
                    handler = self.handler('POST', '/api/coach/review-l2', {'activity_id': 'i1'})
                status, result = handler.reply.call_args.args
                self.assertEqual(status, 200)
                self.assertEqual(result['llm']['llm_status'], expected)
                self.assertEqual(result['llm']['fallback'], 'rule_draft')
                self.assertIsNone(result['llm']['draft'])
                self.assertIn('training_suggestion', result['review'])
                self.assertNotIn('synthetic-sensitive-provider-error', json.dumps(result))
                self.app.llm_post.assert_called_once()

    def test_l2_security_rejects_before_read_or_any_upstream(self):
        self.app.llm_post = Mock(side_effect=AssertionError('Cloud forbidden'))
        for overrides, code in (({'Host': 'evil.test'}, 'host_denied'),
                                ({'Origin': 'https://evil.test'}, 'origin_denied'),
                                ({'Origin': None}, 'origin_denied'),
                                ({'Sec-Fetch-Site': 'cross-site'}, 'origin_denied'),
                                ({'X-Coach-Token': ''}, 'token_denied'),
                                ({'Content-Length': '9000'}, 'body_too_large'),
                                ({'Transfer-Encoding': 'chunked'}, 'invalid_body')):
            with self.subTest(code=code, overrides=overrides):
                handler = self.handler('POST', '/api/coach/review-l2', {'activity_id': 'i1'}, overrides)
                self.assertEqual(handler.reply.call_args.args[1]['error_code'], code)
                handler.rfile.read.assert_not_called()
        self.assertFalse(self.calls)
        self.app.llm_post.assert_not_called()

    def test_l2_requires_connection_and_exact_request_fields(self):
        self.app.llm_post = Mock(side_effect=AssertionError('Cloud forbidden'))
        handler = self.handler('POST', '/api/coach/review-l2', {'activity_id': 'i1'})
        self.assertEqual(handler.reply.call_args.args[0], 401)
        self.assertEqual(handler.reply.call_args.args[1]['error_code'], 'auth_required')
        for payload in ({}, {'activity_id': 'i1', 'api_key': 'client-supplied'},
                        {'activity_id': True}, {'activity_id': '../secret'}):
            with self.subTest(payload=payload), patch.object(self.app.store, 'get_key', return_value=KEY):
                handler = self.handler('POST', '/api/coach/review-l2', payload)
                self.assertEqual(handler.reply.call_args.args[0], 400)
        self.assertFalse(self.calls)
        self.app.llm_post.assert_not_called()

    def test_l2_preserves_intervals_rate_limit_and_never_calls_cloud(self):
        self.app.llm_post = Mock(side_effect=AssertionError('Cloud forbidden'))
        self.replies['/api/v1/activity/i1'] = (429, {'Retry-After': '60'}, KEY.encode())
        with patch.object(self.app.store, 'get_key', return_value=KEY):
            handler = self.handler('POST', '/api/coach/review-l2', {'activity_id': 'i1'})
        status, result = handler.reply.call_args.args
        self.assertEqual((status, result['error_code'], result['retry_after']), (429, 'rate_limited', '60'))
        self.assertNotIn(KEY, json.dumps(result))
        self.app.llm_post.assert_not_called()
        self.assertEqual(len(self.calls), 1)

    def test_l2_page_contract_blind_labels_reveal_and_safe_dom(self):
        page, csp = self.request('/')
        text = page.decode('utf-8')
        for item in ('id="l2-compare"', 'id="l2-result"', 'Draft A', 'Draft B', 'Reveal',
                     '/api/coach/review-l2', 'rejected_unverifiable_number',
                     'rejected_bad_schema', 'not_configured', 'upstream_error',
                     'Alibaba Cloud DashScope', 'crypto.getRandomValues',
                     "setL2Activity(activity.id)", "setL2Activity(null)"):
            with self.subTest(item=item):
                self.assertIn(item, text)
        for identifier in ('brand', 'key', 'connect', 'demo', 'days', 'list', 'activities',
                           'review', 'status', 'connection', 'week-start', 'weekly',
                           'weekly-result', 'waitlist-email', 'waitlist-submit',
                           'waitlist-feedback', 'waitlist-form', 'l2-compare', 'l2-result'):
            self.assertEqual(text.count('id="' + identifier + '"'), 1)
        self.assertIn("params.get('autodemo') === '1'", text)
        self.assertIn("params.get('autoflow') === '1'", text)
        self.assertNotIn('innerHTML', text)
        self.assertNotIn('<script src=', text)
        self.assertIn("script-src 'sha256-", csp)
        self.assertIn("connect-src 'self'", csp)


    @staticmethod
    def ride_payload(identifier='synthetic-ride-01', source='ble_gatt_0x180d'):
        return {'schema_version': 'luma-ride-memory/1', 'id': identifier,
                'started_at': '2026-11-07T08:00:00Z', 'ended_at': '2026-11-07T08:01:00Z',
                'samples_hz': 1, 'series': [{'t': 0, 'hr': 120}, {'t': 1, 'gap': True}, {'t': 30, 'hr': 140}],
                'summary': {'duration_s': 60, 'avg_hr': 130, 'max_hr': 140,
                            'time_in_zone_s': None, 'zone_target': None, 'gaps': 1, 'alerts': 0},
                'coach_notes': ['Signal missing; no interpolation.'],
                'persona_version': 'ride-persona/1', 'source': source}

    def test_ride_store_is_lazy_for_mock_store_and_page(self):
        store = Mock()
        app = server.App(store)
        self.assertIsNone(app._ride_dir)
        page, _ = app.dispatch('GET', '/ride?simulate=1', {'Host': '127.0.0.1:8794'})
        self.assertIn(b'SIMULATED', page)
        self.assertIsNone(app._ride_dir)
        store.get_key.assert_not_called()

    def test_ride_post_persists_exact_contract_and_metadata_list(self):
        payload = self.ride_payload()
        result = self.request('/api/coach/ride/session', payload)
        self.assertEqual(result, {'status': 'saved', 'id': payload['id'], 'storage': 'ephemeral'})
        path = self.app.ride_dir / (payload['id'] + '.json')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), payload)
        result = self.request('/api/coach/ride/sessions?limit=1')
        self.assertEqual(len(result['sessions']), 1)
        meta = result['sessions'][0]
        self.assertEqual(meta['summary'], payload['summary'])
        self.assertEqual(meta['source'], payload['source'])
        self.assertNotIn('series', meta)
        self.assertNotIn('coach_notes', meta)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.calls)

    def test_ride_simulated_and_gap_are_not_rewritten(self):
        payload = self.ride_payload(source='simulated')
        self.request('/api/coach/ride/session', payload)
        saved = json.loads((self.app.ride_dir / (payload['id']+'.json')).read_text(encoding='utf-8'))
        self.assertEqual(saved['source'], 'simulated')
        self.assertEqual(saved['series'], payload['series'])
        self.assertEqual(saved['summary']['time_in_zone_s'], None)
        self.assertEqual(len(saved['series']), 3)

    def test_ride_idempotent_retry_and_conflict_never_overwrite(self):
        payload = self.ride_payload()
        self.request('/api/coach/ride/session', payload)
        path = self.app.ride_dir / (payload['id']+'.json')
        raw = path.read_bytes()
        self.assertEqual(self.request('/api/coach/ride/session', payload)['status'], 'saved')
        payload['coach_notes'] = ['Different memory']
        self.error('session_conflict', lambda: self.request('/api/coach/ride/session', payload))
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(list(self.app.ride_dir.glob('.pending-*')))

    def test_ride_rejects_invalid_fields_before_disk_write(self):
        for change in ({'id': '../escape'}, {'source': 'unknown'}, {'api_key': 'not-allowed'},
                       {'samples_hz': -1}, {'series': [{}]}, {'series': [None]*20001}):
            with self.subTest(change=list(change)):
                payload = self.ride_payload(); payload.update(change)
                handler = self.handler('POST', '/api/coach/ride/session', payload)
                self.assertEqual(handler.reply.call_args.args[0], 400)
        payload = self.ride_payload(); del payload['source']
        self.assertEqual(self.handler('POST', '/api/coach/ride/session', payload).reply.call_args.args[0], 400)
        self.assertIsNone(self.app._ride_dir)
        self.assertFalse(self.calls)

    def test_ride_route_requires_token_and_exact_shape(self):
        headers = self.headers('POST', b'{}'); del headers['X-Coach-Token']
        self.error('token_denied', lambda: self.app.dispatch('POST', '/api/coach/ride/route', headers, b'{}'))
        for payload in ({}, {'origin': '116.397,39.908'}, {'destination_address': '天安门'},
                        {'origin': '116.397,39.908', 'destination_address': '天安门', 'extra': 1},
                        {'origin': 'bad', 'destination_address': '天安门'},
                        {'origin': '181,39', 'destination_address': '天安门'},
                        {'origin': '116.397,39.908', 'destination_address': ''},
                        {'origin': '116.397,39.908', 'destination_address': 'x' * 101}):
            with self.subTest(payload=payload):
                self.error('invalid_input', lambda: self.request('/api/coach/ride/route', payload))

    def test_ride_route_proxies_and_never_leaks_key(self):
        seen = []

        def nav_get(request, *, timeout):
            seen.append(request.full_url)
            if 'geocode' in request.full_url:
                return 200, {}, json.dumps({'status': '1', 'geocodes': [
                    {'formatted_address': '北京市东城区天安门', 'location': '116.397477,39.908692'}]}).encode()
            return 200, {}, json.dumps({'errcode': 0, 'data': {'paths': [
                {'distance': 5231, 'duration': 1800, 'steps': [
                    {'instruction': '向东骑行200米右转', 'road': '长安街', 'distance': 200,
                     'polyline': '116.390,39.908;116.392,39.908'}]}]}}).encode()

        self.app.nav_get = nav_get
        key_env = patch.dict('os.environ', {'AMAP_API_KEY': 'synthetic-secret-amap-key'})
        key_env.start(); self.addCleanup(key_env.stop)
        result = self.request('/api/coach/ride/route', {'origin': '116.397,39.908', 'destination_address': '天安门'})
        self.assertEqual(result['route_status'], 'ok')
        self.assertEqual(result['distance_m'], 5231)
        self.assertEqual(result['steps'][0]['end_lng'], 116.392)
        self.assertEqual(len(seen), 2)
        self.assertNotIn('synthetic-secret-amap-key', json.dumps(result))
        self.assertFalse(self.calls)

    def test_ride_route_walking_mode_hits_walking_endpoint(self):
        seen = []

        def nav_get(request, *, timeout):
            seen.append(request.full_url)
            if 'geocode' in request.full_url:
                return 200, {}, json.dumps({'status': '1', 'geocodes': [
                    {'formatted_address': '北京市东城区天安门', 'location': '116.397477,39.908692'}]}).encode()
            return 200, {}, json.dumps({'status': '1', 'info': 'OK', 'route': {'paths': [
                {'distance': '5231', 'duration': '3600', 'steps': [
                    {'instruction': '向东步行200米右转', 'road': '长安街', 'distance': '200',
                     'polyline': '116.390,39.908;116.392,39.908'}]}]}}).encode()

        self.app.nav_get = nav_get
        key_env = patch.dict('os.environ', {'AMAP_API_KEY': 'synthetic-secret-amap-key'})
        key_env.start(); self.addCleanup(key_env.stop)
        result = self.request('/api/coach/ride/route', {'origin': '116.397,39.908',
                                                        'destination_address': '天安门', 'mode': 'walking'})
        self.assertEqual(result['route_status'], 'ok')
        self.assertEqual(result['distance_m'], 5231)
        self.assertIs(type(result['duration_s']), int)
        self.assertIn('/v3/direction/walking', seen[1])
        self.assertNotIn('synthetic-secret-amap-key', json.dumps(result))

    def test_ride_route_default_mode_stays_bicycling(self):
        seen = []

        def nav_get(request, *, timeout):
            seen.append(request.full_url)
            if 'geocode' in request.full_url:
                return 200, {}, json.dumps({'status': '1', 'geocodes': [
                    {'formatted_address': '北京市东城区天安门', 'location': '116.397477,39.908692'}]}).encode()
            return 200, {}, json.dumps({'errcode': 0, 'data': {'paths': [
                {'distance': 5231, 'duration': 1800, 'steps': [
                    {'instruction': '向东骑行200米右转', 'road': '长安街', 'distance': 200,
                     'polyline': '116.390,39.908;116.392,39.908'}]}]}}).encode()

        self.app.nav_get = nav_get
        key_env = patch.dict('os.environ', {'AMAP_API_KEY': 'synthetic-secret-amap-key'})
        key_env.start(); self.addCleanup(key_env.stop)
        result = self.request('/api/coach/ride/route', {'origin': '116.397,39.908', 'destination_address': '天安门'})
        self.assertEqual(result['route_status'], 'ok')
        self.assertIn('/v4/direction/bicycling', seen[1])

    def test_ride_route_invalid_mode_rejected_without_upstream(self):
        self.app.nav_get = Mock(side_effect=AssertionError('No upstream for invalid mode'))
        key_env = patch.dict('os.environ', {'AMAP_API_KEY': 'synthetic-secret-amap-key'})
        key_env.start(); self.addCleanup(key_env.stop)
        for bad in ('flying', '', 123, None, True):
            with self.subTest(mode=bad):
                self.error('invalid_input', lambda: self.request('/api/coach/ride/route',
                           {'origin': '116.397,39.908', 'destination_address': '天安门', 'mode': bad}))
        self.app.nav_get.assert_not_called()

    def test_ride_route_not_configured_without_env_key(self):
        self.app.nav_get = Mock(side_effect=AssertionError('Upstream must not be called without a key'))
        with patch.dict('os.environ') as env:
            env.pop('AMAP_API_KEY', None)
            result = self.request('/api/coach/ride/route', {'origin': '116.397,39.908', 'destination_address': '天安门'})
        self.assertEqual(result, {'route_status': 'not_configured'})
        self.app.nav_get.assert_not_called()

    def test_ride_route_rate_limited_with_ride_bucket(self):
        self.app.nav_get = Mock(side_effect=AssertionError('No upstream without a key'))
        for _ in range(10):
            self.request('/api/coach/ride/route', {'origin': '116.397,39.908', 'destination_address': '天安门'})
        self.error('rate_limited', lambda: self.request('/api/coach/ride/route',
                                                        {'origin': '116.397,39.908', 'destination_address': '天安门'}))

    def test_ride_rejects_nonfinite_and_duplicate_json_fields(self):
        raw_bodies = (b'{"id":"one","id":"two"}', b'{"samples_hz":NaN}', b'{"samples_hz":1e999}')
        for body in raw_bodies:
            with self.subTest(body=body):
                error = self.error('invalid_json', lambda: self.app.dispatch(
                    'POST', '/api/coach/ride/session', self.headers('POST', body), body))
                self.assertEqual(error.status, 400)
        self.assertIsNone(self.app._ride_dir)

    def test_ride_security_denies_before_body_read(self):
        for overrides, code in (({'Host':'evil.test'},'host_denied'),
                                ({'Origin':'https://evil.test'},'origin_denied'),
                                ({'Origin':None},'origin_denied'),
                                ({'Sec-Fetch-Site':'cross-site'},'origin_denied'),
                                ({'X-Coach-Token':''},'token_denied'),
                                ({'Content-Length':str(server.MAX_RIDE_BODY_BYTES+1)},'body_too_large'),
                                ({'Transfer-Encoding':'chunked'},'invalid_body')):
            with self.subTest(code=code):
                h = self.handler('POST', '/api/coach/ride/session', self.ride_payload(), overrides)
                self.assertEqual(h.reply.call_args.args[1]['error_code'], code)
                h.rfile.read.assert_not_called()
        for path in ('/api/coach/ride/sessions', '/api/coach/ride/sessions?limit=20'):
            h = self.handler('GET', path, overrides={'X-Coach-Token':''})
            self.assertEqual(h.reply.call_args.args[0], 403)
        self.assertIsNone(self.app._ride_dir)

    def test_ride_body_limit_accepts_real_series_without_relaxing_other_routes(self):
        payload = self.ride_payload()
        payload['series'] = [{'t':i/100, 'hr':120} for i in range(2000)]
        payload['summary'].update(avg_hr=120, max_hr=120, gaps=0)
        self.assertGreater(len(json.dumps(payload)), server.MAX_BODY_BYTES)
        self.assertEqual(self.handler('POST', '/api/coach/ride/session', payload).reply.call_args.args[0], 200)
        self.assertEqual(self.handler('POST', '/api/coach/demo-review', {},
                         {'Content-Length':'9000'}).reply.call_args.args[0], 413)

    def test_ride_post_rate_limit_once_per_handler_and_expiration(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            for _ in range(10):
                h = self.handler('POST', '/api/coach/ride/session', self.ride_payload())
                self.assertEqual(h.reply.call_args.args[0], 200)
            h = self.handler('POST', '/api/coach/ride/session', self.ride_payload())
            status, result = h.reply.call_args.args
            self.assertEqual((status, result['error_code']), (429, 'rate_limited'))
            self.assertIn('retry_after', result)
        with patch.object(server.time, 'monotonic', return_value=161):
            self.assertEqual(self.request('/api/coach/ride/session', self.ride_payload())['status'], 'saved')

    def test_ride_get_rate_limit_and_independent_post_budget(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            for _ in range(60):
                self.assertEqual(self.request('/api/coach/ride/sessions')['sessions'], [])
            error = self.error('rate_limited', lambda: self.request('/api/coach/ride/sessions'))
            self.assertEqual(error.status, 429)
            self.assertEqual(self.request('/api/coach/ride/session', self.ride_payload())['status'], 'saved')

    def test_ride_list_strict_limit_and_empty_storage_is_not_created(self):
        self.assertEqual(self.request('/api/coach/ride/sessions')['sessions'], [])
        self.assertFalse(self.app.ride_dir.exists())
        for query in ('limit=0','limit=21','limit=-1','limit=1&limit=2','limit=',
                      'limit=1&other=2','other=1','limit=1.0','limit','limit=999'):
            self.error('invalid_input', lambda q=query: self.request('/api/coach/ride/sessions?'+q))

    def test_ride_list_newest_first_and_skips_corrupt_files(self):
        import os
        for i in range(3):
            payload = self.ride_payload('ride-'+str(i))
            self.request('/api/coach/ride/session', payload)
            os.utime(self.app.ride_dir/(payload['id']+'.json'), (100+i,100+i))
        (self.app.ride_dir/'corrupt.json').write_text('{', encoding='utf-8')
        result = self.request('/api/coach/ride/sessions?limit=2')
        self.assertEqual([row['id'] for row in result['sessions']], ['ride-2','ride-1'])
        self.assertEqual(result['skipped_invalid'], 1)

    def test_ride_atomic_write_failure_returns_storage_error_no_partial_file(self):
        with patch.object(server.os, 'replace', side_effect=OSError('synthetic disk failure')):
            h = self.handler('POST', '/api/coach/ride/session', self.ride_payload())
        status, result = h.reply.call_args.args
        self.assertEqual((status,result['error_code']), (503,'storage_unavailable'))
        self.assertNotIn('synthetic disk failure', json.dumps(result))
        self.assertFalse(list(self.app.ride_dir.iterdir()))

    def test_ride_page_public_query_csp_and_required_contracts(self):
        for path in ('/ride', '/ride?simulate=1'):
            page, csp = self.app.dispatch('GET', path, {'Host':'127.0.0.1:8794'})
            text = page.decode('utf-8')
            for item in ('0x180D','0x2A37','getUint16(1, true)','getUint8(1)', 'PERSONA',
                         'OUTSIDE_DWELL_MS = 10000', 'ALERT_COOLDOWN_MS = 60000',
                         'simulate=1','SIMULATED','id="simulated-banner"','gap:true',
                         "source:SIMULATED ? 'simulated' : 'ble_gatt_0x180d'",
                         'speechSynthesis','zh-CN','utterance.rate = 1.0',
                         'gattserverdisconnected','localStorage.setItem','retry-upload',
                         'download-session','/api/coach/ride/session','X-Coach-Token',
                         'Record only. Not medical advice.', 'Android Chrome','iOS Safari'):
                with self.subTest(path=path, item=item): self.assertIn(item, text)
            self.assertNotIn('innerHTML', text)
            self.assertNotIn('<script src=', text)
            self.assertIn("script-src 'sha256-", csp)
            self.assertIn("style-src 'sha256-", csp)
            self.assertIn("connect-src 'self'", csp)
        self.assertIsNone(self.app._ride_dir)


if __name__ == '__main__':
    unittest.main()
