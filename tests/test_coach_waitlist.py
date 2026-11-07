"""Offline waitlist dispatcher tests; temporary files, no sockets or real keys."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_cards import ActionError
import coach_server as server
from coach_waitlist import WaitlistStore, normalize_email


class WaitlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.credentials = Path(self.temp.name) / 'coach.sqlite3'
        self.store = Mock(path=self.credentials)
        self.app = server.App(self.store, fetch=Mock(side_effect=AssertionError('No transport')))
        guard = patch('coach_icu_client._fetch', side_effect=AssertionError('No real network'))
        guard.start()
        self.addCleanup(guard.stop)

    def request(self, payload, **overrides):
        raw = json.dumps(payload).encode()
        headers = {'Host':'127.0.0.1:8794', 'Origin':'http://127.0.0.1:8794',
                   'X-Coach-Token':self.app.token, 'Content-Type':'application/json', 'Content-Length':str(len(raw))}
        headers.update(overrides)
        return self.app.dispatch('POST', '/api/coach/waitlist', headers, raw)

    def test_valid_email_stored_separately_without_key_or_transport(self):
        result = self.request({'email':'Rider+test@Example.com'})
        self.assertEqual(result['status'], 'already_registered')
        self.assertEqual(self.app.waitlist.path.name, 'waitlist.db')
        self.assertNotEqual(self.app.waitlist.path.resolve(), self.credentials.resolve())
        self.assertFalse(self.credentials.exists())
        self.store.get_key.assert_not_called()
        self.store.save_key.assert_not_called()
        self.app.fetch.assert_not_called()
        with closing(sqlite3.connect(self.app.waitlist.path)) as db:
            self.assertEqual(db.execute('SELECT email FROM waitlist').fetchall(), [('rider+test@example.com',)])
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('waitlist',)])

    def test_repeat_is_idempotent_and_indistinguishable(self):
        first = self.request({'email':'Rider@example.com'})
        second = self.request({'email':'rider@EXAMPLE.COM'})
        other = self.request({'email':'new@example.com'})
        self.assertEqual(first, second)
        self.assertEqual(first, other)
        self.assertNotIn('email', first)
        with closing(sqlite3.connect(self.app.waitlist.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM waitlist').fetchone()[0], 2)

    def test_invalid_emails_do_not_create_database(self):
        invalid = [None, 1, True, [], {}, '', ' x@example.com', 'x@example.com\n', 'a@@example.com',
                   '.a@example.com', 'a.@example.com', 'a..b@example.com', 'a b@example.com',
                   'x@localhost', 'x@-example.com', 'x@example-.com', 'x@exa_mple.com',
                   'x@例子.com', 'x@example.c', 'a'*65+'@example.com', 'x@'+'a'*64+'.com',
                   '"x"@example.com', '<x@example.com>', 'x@example.com\r\nBcc:y@example.com', 'x'*255]
        for email in invalid:
            with self.subTest(email=email), self.assertRaises(ActionError) as caught:
                self.request({'email':email})
            self.assertEqual(caught.exception.code, 'invalid_email')
        self.assertFalse(self.app.waitlist.path.exists())

    def test_payload_shape_rejected(self):
        for payload in ({}, {'email':'x@example.com','extra':True}, [], 'x@example.com'):
            with self.subTest(payload=payload), self.assertRaises(ActionError):
                self.request(payload)
        self.assertFalse(self.app.waitlist.path.exists())

    def test_security_guards_remain_required(self):
        for headers, code in [({'X-Coach-Token':''},'token_denied'),
                              ({'Origin':'https://evil.example'},'origin_denied'),
                              ({'Host':'evil.example'},'host_denied'),
                              ({'Content-Type':'text/plain'},'invalid_body'),
                              ({'Content-Length':'99999'},'body_too_large')]:
            with self.subTest(code=code), self.assertRaises(ActionError) as caught:
                self.request({'email':'x@example.com'}, **headers)
            self.assertEqual(caught.exception.code,code)
        self.assertFalse(self.app.waitlist.path.exists())

    def test_same_database_rejected(self):
        with self.assertRaises(ValueError):
            WaitlistStore(self.credentials, credential_path=self.credentials)
        with self.assertRaises(ValueError):
            server.App(Mock(path=self.credentials.with_name('waitlist.db'))).waitlist

    def test_existing_credentials_untouched_and_waitlist_persists(self):
        with closing(sqlite3.connect(self.credentials)) as db:
            db.execute('CREATE TABLE credential (value TEXT)')
            db.execute("INSERT INTO credential VALUES ('synthetic')")
            db.commit()
        before = self.credentials.read_bytes()
        self.request({'email':'x@example.com'})
        self.assertEqual(self.credentials.read_bytes(),before)
        other = WaitlistStore(self.app.waitlist.path, credential_path=self.credentials)
        self.assertEqual(other.register('X@example.com'),'already_registered')
        with closing(sqlite3.connect(other.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM waitlist').fetchone()[0],1)

    def test_page_feedback_config_brand_and_privacy(self):
        page, csp = server._page()
        text = page.decode()
        self.assertIn('id="waitlist-form"',text)
        self.assertIn('type="email"',text)
        self.assertIn("api('/api/coach/waitlist', {email:",text)
        self.assertIn("el('waitlist-feedback').textContent = result.message",text)
        self.assertIn("el('brand').textContent = data.brand_name",text)
        self.assertNotIn(server.BRAND_NAME,text)
        self.assertNotIn('innerHTML',text)
        self.assertIn("script-src 'sha256-",csp)
        policy = server._page('privacy.html')[0].decode()
        self.assertIn('waitlist.db',policy)
        self.assertIn('not encrypted',policy)

    def test_conservative_valid_aliases(self):
        for value in ('rider@example.com', 'a.b+tag@sub.example.co.uk', 'a-b@example.travel'):
            self.assertEqual(normalize_email(value),value)


if __name__ == '__main__':
    unittest.main()
