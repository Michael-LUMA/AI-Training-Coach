"""Private mobile test gate: synthetic credentials, local sockets, no cloud calls."""
import base64
import hashlib
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import warnings
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server as server
from action_cards import ActionError

PASSWORD = 'synthetic-private-mobile-password-12345'
BASIC = 'Basic ' + base64.b64encode(('boss:' + PASSWORD).encode()).decode()


class MobileAccessConfigTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_loopback_default_remains_ungated(self):
        self.assertIsNone(server._private_test_credentials('127.0.0.1'))

    def test_remote_without_password_fails_before_binding(self):
        os.environ['LUMA_COACH_ALLOW_REMOTE'] = '1'
        with patch.object(server, 'ThreadingHTTPServer') as http, warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with self.assertRaises(ValueError):
                server.create_server(host='0.0.0.0')
            http.assert_not_called()

    def test_public_origin_alone_cannot_silently_disable_gate(self):
        os.environ['LUMA_COACH_PUBLIC_ORIGIN'] = 'https://mobile.example'
        with self.assertRaises(ValueError):
            server._private_test_credentials('127.0.0.1')

    def test_invalid_secrets_do_not_appear_in_errors(self):
        for password in ('short-private-value', 'x'*129, ' '*32, '密'*32, 'x'*32+'\n'):
            os.environ['LUMA_COACH_TEST_PASSWORD'] = password
            with self.subTest(length=len(password)), self.assertRaises(ValueError) as error:
                server._private_test_credentials('127.0.0.1')
            self.assertNotIn(password, str(error.exception))

    def test_remote_requires_explicit_https_origin(self):
        os.environ['LUMA_COACH_TEST_PASSWORD'] = PASSWORD
        for origin in ('', 'http://mobile.example'):
            os.environ['LUMA_COACH_PUBLIC_ORIGIN'] = origin
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                server._private_test_credentials('0.0.0.0')

    def test_secure_remote_configuration_accepted(self):
        os.environ.update(LUMA_COACH_TEST_PASSWORD=PASSWORD,
                          LUMA_COACH_PUBLIC_ORIGIN='https://mobile.example')
        self.assertEqual(server._private_test_credentials('0.0.0.0'),
                         ('boss:' + PASSWORD).encode())

    def test_duplicate_authorization_is_rejected(self):
        headers = Message()
        headers.add_header('Authorization', BASIC)
        headers.add_header('Authorization', BASIC)
        with self.assertRaises(ActionError) as error:
            server._check_private_test_access('GET', '/mark', headers, ('boss:'+PASSWORD).encode())
        self.assertEqual(error.exception.code, 'invalid_input')

    def test_auth_is_checked_before_body_read_or_dispatch(self):
        os.environ['LUMA_COACH_TEST_PASSWORD'] = PASSWORD
        app = Mock()
        with patch.object(server, 'ThreadingHTTPServer') as ctor:
            server.create_server(app=app)
        cls = ctor.call_args.args[1]
        handler = cls.__new__(cls)
        handler.command, handler.path = 'POST', '/api/coach/ride/chat'
        handler.headers = {'Content-Length': '9999999'}
        handler.connection, handler.rfile, handler.reply = Mock(), Mock(), Mock()
        handler.server = SimpleNamespace(server_port=8794)
        handler.handle_request()
        handler.rfile.read.assert_not_called()
        app.preflight.assert_not_called()
        app.dispatch.assert_not_called()
        self.assertEqual(handler.reply.call_args.args[0], 401)


class MobileAccessHTTPTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'LUMA_COACH_TEST_PASSWORD': PASSWORD}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix='mark-private-test-')
        self.addCleanup(tmp.cleanup)
        self.app = server.App(server.CoachStore(Path(tmp.name)/'coach.sqlite3'),
                              fetch=Mock(side_effect=AssertionError('No network')),
                              llm_post=Mock(side_effect=AssertionError('No paid calls')))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.http = server.create_server(self.port, self.app, host='127.0.0.1')
        worker = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval':0.01}, daemon=True)
        worker.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.http.shutdown)

    def request(self, path='/mark', method='GET', headers=None, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_anonymous_pages_config_records_and_assets_are_blocked(self):
        for path in ('/', '/mark', '/ride', '/api/coach/config', '/api/coach/ride/sessions',
                     '/icon.svg', '/privacy', '/fonts/manrope-latin.woff2'):
            with self.subTest(path=path):
                status, headers, raw = self.request(path)
                self.assertEqual(status, 401)
                self.assertTrue(headers['WWW-Authenticate'].startswith('Basic '))
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(json.loads(raw)['error_code'], 'test_access_required')
                self.assertNotIn(self.app.token.encode(), raw)
                self.assertNotIn(PASSWORD.encode(), raw)

    def test_browser_token_alone_is_not_a_login(self):
        self.assertEqual(self.request('/api/coach/ride/sessions',
                                     headers={'X-Coach-Token':self.app.token})[0], 401)

    def test_password_in_url_is_not_a_login(self):
        self.assertEqual(self.request('/mark?password='+PASSWORD)[0], 401)

    def test_wrong_or_malformed_auth_is_sanitized(self):
        wrong = 'Basic '+base64.b64encode(('other:'+PASSWORD).encode()).decode()
        for auth in ('Bearer '+PASSWORD, 'Basic !!!', 'Basic /////', 'Basic ', wrong, 'x'*600):
            with self.subTest(kind=auth[:6]):
                status, _, raw = self.request(headers={'Authorization':auth})
                self.assertEqual(status,401)
                self.assertNotIn(PASSWORD.encode(),raw)

    def test_authenticated_page_and_config_work(self):
        status, headers, raw = self.request(headers={'Authorization':BASIC})
        self.assertEqual(status,200)
        self.assertIn(b'mark.activeRide',raw)
        self.assertEqual(headers['X-Mark-Build'], hashlib.sha256(raw).hexdigest()[:16])
        status, _, raw = self.request('/api/coach/config',headers={'Authorization':BASIC})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['token'],self.app.token)
        self.app.fetch.assert_not_called()
        self.app.llm_post.assert_not_called()

    def test_basic_auth_does_not_replace_browser_token(self):
        self.assertEqual(self.request('/api/coach/ride/sessions',headers={'Authorization':BASIC})[0],403)
        self.assertEqual(self.request('/api/coach/ride/sessions',headers={
            'Authorization':BASIC,'X-Coach-Token':self.app.token})[0],200)

    def test_authenticated_cross_origin_is_still_blocked(self):
        status, _, raw = self.request(headers={'Authorization':BASIC,'Origin':'https://evil.invalid'})
        self.assertEqual(status,403)
        self.assertEqual(json.loads(raw)['error_code'],'origin_denied')

    def test_only_exact_read_only_health_is_public(self):
        status, headers, raw = self.request('/health')
        self.assertEqual((status,json.loads(raw)),(200,{'status':'ok'}))
        self.assertNotIn('WWW-Authenticate',headers)
        for method,path in (('GET','/health?x=1'),('POST','/health'),('GET','/health/')):
            with self.subTest(method=method,path=path):
                self.assertEqual(self.request(path,method)[0],401)

    def test_unauthenticated_asr_chat_and_webhook_do_not_execute(self):
        for path in ('/api/coach/ride/asr','/api/coach/ride/chat','/api/coach/billing/webhook'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path,'POST',body=b'{}',headers={
                    'Content-Type':'application/json'})[0],401)
        self.app.fetch.assert_not_called()
        self.app.llm_post.assert_not_called()

    def test_credentials_are_fixed_at_startup(self):
        os.environ['LUMA_COACH_TEST_PASSWORD']='another-synthetic-private-password-12345'
        self.assertEqual(self.request(headers={'Authorization':BASIC})[0],200)
        os.environ.pop('LUMA_COACH_TEST_PASSWORD')
        self.assertEqual(self.request()[0],401)


if __name__ == '__main__':
    unittest.main()
