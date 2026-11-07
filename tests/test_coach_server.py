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


if __name__ == '__main__':
    unittest.main()
