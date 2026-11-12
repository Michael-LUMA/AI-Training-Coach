"""Offline configuration tests: no sockets, real credentials or external requests."""
import os
import sys
import tempfile
import unittest
import warnings
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server as server
from coach_keystore import KeyStore
from action_cards import ActionError

class CoachConfigTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_cloud_database_is_opt_in_and_initialized_before_socket(self):
        with patch.object(server.cloud_db, 'init_db') as initialize, \
                patch.object(server, 'ThreadingHTTPServer') as constructor:
            server.create_server()
            initialize.assert_not_called()
            os.environ['DATABASE_URL'] = 'postgresql://user:secret@db/mark'
            events = []
            initialize.side_effect = lambda: events.append('db')
            constructor.side_effect = lambda *args: (events.append('socket'), Mock())[1]
            server.create_server()
        self.assertEqual(events, ['db', 'socket'])

    def test_cloud_initialization_failure_is_sanitized_and_never_binds(self):
        os.environ['DATABASE_URL'] = 'postgresql://user:secret@db/mark'
        with patch.object(server.cloud_db, 'init_db', side_effect=server.DatabaseError('password=secret')), \
                patch.object(server.cloud_db, 'close') as close, \
                patch.object(server, 'ThreadingHTTPServer') as constructor:
            with self.assertRaises(ValueError) as caught:
                server.create_server()
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('postgresql://', str(caught.exception))
        constructor.assert_not_called()
        close.assert_called_once_with()

    def test_defaults(self):
        self.assertEqual(server._bind_config(), ('127.0.0.1', 8794))
        self.assertEqual(server.CoachStore().path, server.DEFAULT_DB)

    def test_environment_and_explicit_precedence(self):
        os.environ.update(LUMA_COACH_HOST='127.0.0.2', LUMA_COACH_PORT='10000')
        self.assertEqual(server._bind_config(), ('127.0.0.2', 10000))
        self.assertEqual(server._bind_config('127.0.0.1', 9000), ('127.0.0.1', 9000))

    def test_invalid_ports(self):
        for port in ('', 'abc', '1.5', '-1', '1023', '65536', '8765', '8791', '8793', ' 9000', True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                server._bind_config(port=port)
        os.environ['LUMA_COACH_PORT'] = 'bad'
        with self.assertRaises(ValueError):
            server._bind_config()

    def test_invalid_hosts(self):
        for host in ('', 'localhost', '::1', 'example.com', '127.0.0.1:9000', ' 127.0.0.1'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                server._bind_config(host=host)

    def test_remote_requires_exact_opt_in_before_binding(self):
        for allow in (None, '', 'true', '01', '1 '):
            if allow is not None:
                os.environ['LUMA_COACH_ALLOW_REMOTE'] = allow
            with self.subTest(allow=allow), patch.object(server, 'ThreadingHTTPServer') as http:
                with self.assertRaises(ValueError):
                    server.create_server(host='0.0.0.0')
                http.assert_not_called()

    def test_remote_explicit_warns_and_binds(self):
        os.environ.update(LUMA_COACH_ALLOW_REMOTE='1',
                          LUMA_COACH_PUBLIC_ORIGIN='https://private-test.example',
                          LUMA_COACH_TEST_PASSWORD='synthetic-mobile-test-password-12345')
        with patch.object(server, 'ThreadingHTTPServer') as http:
            with self.assertWarnsRegex(RuntimeWarning, 'REMOTE BINDING'):
                server.create_server(host='0.0.0.0', port=10000, app=Mock())
            self.assertEqual(http.call_args.args[0], ('0.0.0.0', 10000))

    def test_loopback_does_not_warn(self):
        with warnings.catch_warnings(record=True) as found:
            warnings.simplefilter('always')
            server._bind_config()
        self.assertFalse(found)

    def test_database_env_and_override(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'private' / 'coach.sqlite3'
            os.environ['LUMA_COACH_DB'] = str(target)
            self.assertEqual(server.CoachStore().path, target)
            self.assertEqual(server.CoachStore(Path(temp) / 'other.db').path.name, 'other.db')
            self.assertFalse(target.exists())
        for path in ('', '  '):
            os.environ['LUMA_COACH_DB'] = path
            with self.assertRaises(ValueError):
                server.CoachStore()
        with self.assertRaises(ValueError):
            server.CoachStore('bad\x00path')

    def test_master_key_env_encrypted_store(self):
        os.environ['LUMA_COACH_MASTER_KEY'] = 'synthetic-environment-master-key-123456789'
        with patch('coach_keystore.os.name', 'posix'):
            keystore = KeyStore()
        self.assertEqual(keystore.status['key_storage_backend'], 'masterkey')
        with tempfile.TemporaryDirectory() as temp:
            os.environ['LUMA_COACH_DB'] = str(Path(temp) / 'coach.db')
            store = server.CoachStore(keystore=keystore)
            store.save_key('synthetic-api-key')
            self.assertEqual(store.get_key(), 'synthetic-api-key')
            self.assertNotIn(b'synthetic-api-key', store.path.read_bytes())

    def test_missing_master_key_fails_closed(self):
        with patch('coach_keystore.os.name', 'posix'):
            keystore = KeyStore()
        self.assertFalse(keystore.status['encrypted_at_rest'])
        with self.assertRaises(ActionError) as caught:
            keystore.encrypt('synthetic-api-key')
        self.assertEqual(caught.exception.code, 'key_storage_unavailable')

    def test_health_liveness_no_secrets_or_dependencies(self):
        store = Mock()
        app = server.App(store, fetch=Mock(side_effect=AssertionError('No network')))
        self.assertEqual(app.dispatch('GET', '/health', {'Host': 'probe.onrender.com'}), {'status': 'ok'})
        store.get_key.assert_not_called()
        app.fetch.assert_not_called()
        with self.assertRaises(ActionError):
            app.preflight('GET', '/api/coach/config', {'Host': 'probe.onrender.com'}, 10000)

    def test_health_guards(self):
        app = server.App(Mock())
        for method, path, headers in (
            ('POST', '/health', {'Host': 'probe'}),
            ('GET', '/health?x=1', {'Host': 'probe'}),
            ('GET', '/health', {}),
            ('GET', '/health', {'Host': 'probe', 'Origin': 'https://evil.invalid'}),
            ('GET', '/health', {'Host': 'probe', 'Sec-Fetch-Site': 'cross-site'}),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(ActionError):
                app.preflight(method, path, headers, 10000)
        headers = Message()
        headers.add_header('Host', 'probe')
        headers.add_header('Host', 'other')
        with self.assertRaises(ActionError):
            app.preflight('GET', '/health', headers, 10000)

    def test_cli_override_environment(self):
        os.environ.update(LUMA_COACH_HOST='0.0.0.0', LUMA_COACH_PORT='bad')
        with patch.object(sys, 'argv', ['coach_server.py', '--host', '127.0.0.1', '--port', '9000']), patch.object(server, 'ThreadingHTTPServer') as http:
            server.main()
            self.assertEqual(http.call_args.args[0], ('127.0.0.1', 9000))
            http.return_value.serve_forever.assert_called_once()
            http.return_value.server_close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
