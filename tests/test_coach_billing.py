"""Synthetic offline billing tests; no live Creem, network, card or real secrets."""
from contextlib import closing
import copy
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from email.message import Message

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_cards import ActionError
from coach_billing import BillingStore, STATES, verify
import coach_server as server

SECRET = 'synthetic-webhook-secret-only'
ROUTE = '/api/coach/billing/webhook'


def event(kind='subscription.paid', stamp=1730000000, ident='evt-1', alias=False):
    return {'id':ident, 'event_type' if alias else 'eventType':kind, 'created_at':stamp,
            'object':{'id':'sub-1', 'customer':{'email':'Rider@example.com'}}}


class BillingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'billing.db'
        self.credentials = self.path.with_name('coach.sqlite3')
        self.store = BillingStore(self.path, credential_path=self.credentials)
        credentials = Mock(path=self.credentials)
        credentials.get_key.return_value = None
        credentials.encryption_status = {}
        self.app = server.App(credentials, fetch=Mock(side_effect=AssertionError('Network forbidden')))
        env = patch.dict('os.environ', {'CREEM_WEBHOOK_SECRET':SECRET})
        env.start(); self.addCleanup(env.stop)

    def post(self, value=None, raw=None, **extra):
        raw = json.dumps(event() if value is None else value).encode() if raw is None else raw
        headers = {'Host':'callback.example', 'Content-Type':'application/json', 'Content-Length':str(len(raw)),
                   'creem-signature':hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()}
        headers.update(extra)
        return self.app.dispatch('POST',ROUTE,headers,raw)

    def count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    def test_signature_exact_bytes_and_constant_time_comparison(self):
        raw=b'{ "synthetic": true }'
        sig=hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()
        with patch('coach_billing.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
            self.assertTrue(verify(raw,sig,SECRET)); compare.assert_called_once()
        self.assertTrue(verify(raw,sig.upper(),SECRET))
        self.assertFalse(verify(raw+b' ',sig,SECRET))
        self.assertFalse(verify(raw,sig,'wrong'))
        for bad in ('', 'z'*64, 'a'*63, None):
            self.assertFalse(verify(raw,bad,SECRET))
        self.assertFalse(verify(raw,sig,''))

    def test_callback_without_token_external_host_and_config(self):
        self.assertEqual(self.post(),{'received':True})
        config=self.app.dispatch('GET','/api/coach/config',{'Host':'127.0.0.1:8794'})
        self.assertTrue(config['billing_enabled'])
        self.assertEqual(config['subscription_status'],'paid')
        self.assertNotIn('rider@example.com',json.dumps(config))
        self.assertFalse(self.credentials.exists())
        self.app.fetch.assert_not_called()

    def test_missing_secret_fail_closed_no_storage(self):
        with patch.dict('os.environ',{'CREEM_WEBHOOK_SECRET':''}):
            with self.assertRaises(ActionError) as caught: self.post()
            self.assertEqual(caught.exception.code,'billing_not_configured')
            self.assertEqual(caught.exception.status,503)
            config=self.app.dispatch('GET','/api/coach/config',{'Host':'127.0.0.1:8794'})
            self.assertFalse(config['billing_enabled'])
            self.assertEqual(config['subscription_status'],'none')
        self.assertFalse(self.path.exists())

    def test_bad_signature_tampering_rejected_before_json(self):
        for raw,sig in ((b'not-json','0'*64),(json.dumps(event()).encode(),'bad')):
            with self.assertRaises(ActionError) as caught: self.post(raw=raw,**{'creem-signature':sig})
            self.assertEqual(caught.exception.code,'billing_signature_invalid')
        self.assertFalse(self.path.exists())

    def test_replay_id_unique_even_changed_payload(self):
        self.store.handle(event())
        self.store.handle(event('subscription.expired',1730000100))
        self.assertEqual(self.count(),1)
        self.assertEqual(self.store.current_status(),'paid')

    def test_out_of_order_expired_then_older_paid_does_not_rollback(self):
        self.store.handle(event('subscription.expired',1730000200,'new'))
        self.store.handle(event('subscription.paid',1730000000,'old'))
        self.assertEqual(self.store.current_status(),'expired')
        self.assertEqual(self.count(),2)

    def test_newer_paid_can_renew(self):
        self.store.handle(event('subscription.expired',1730000000,'old'))
        self.store.handle(event('subscription.paid',1730000200,'new',True))
        self.assertEqual(self.store.current_status(),'paid')

    def test_ties_are_deterministic_and_terminal_safe(self):
        for kinds in (('subscription.paid','subscription.expired'),('subscription.expired','subscription.paid')):
            with self.subTest(kinds=kinds):
                path=self.path.with_name(kinds[0]+'.db'); store=BillingStore(path)
                for kind in kinds: store.handle(event(kind,ident=kind))
                self.assertEqual(store.current_status(),'expired')

    def test_both_aliases_unknown_types_and_missing_subscription(self):
        unknown=event('new.future.event',alias=True)
        self.store.handle(unknown)
        self.assertEqual(self.store.current_status(),'none')
        self.assertEqual(self.count(),1)
        checkout=event('checkout.completed',ident='checkout')
        self.store.handle(checkout)  # checkout id is NOT a subscription identity
        self.assertEqual(self.store.current_status(),'none')
        checkout['id']='checkout2'; checkout['object']['subscription']={'id':'sub-1'}
        self.store.handle(checkout)
        self.assertEqual(self.store.current_status(),'completed')

    def test_all_key_events_and_email_persist_without_raw_payload(self):
        for index,(kind,state) in enumerate(STATES.items()):
            obj=event(kind,1730000000+index,str(index))
            obj['object']['subscription']='sub-1'
            obj['object']['private_test_marker']='must-not-persist'
            self.store.handle(obj)
            self.assertEqual(self.store.current_status(),state)
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT email,status FROM subscriptions').fetchone()
            self.assertEqual(row,('rider@example.com','refunded'))
        self.assertNotIn(b'must-not-persist',self.path.read_bytes())
        self.assertNotIn(SECRET.encode(),self.path.read_bytes())

    def test_timestamps_iso_seconds_milliseconds(self):
        for value in (1730000000,1730000000000,'2024-10-27T03:33:20Z'):
            e=event(stamp=value,ident=str(value)); self.store.handle(e)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(len(set(r[0] for r in db.execute('SELECT created_at FROM events'))),1)

    def test_invalid_envelopes_and_dates_do_not_create_db(self):
        bad=[None,[],{},event(stamp=True),event(stamp=float('inf')),event(stamp='2026-11-07'),event(stamp=-1)]
        conflicting=event(); conflicting['event_type']='subscription.expired'; bad.append(conflicting)
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ActionError): self.store.handle(value)
        self.assertFalse(self.path.exists())

    def test_callback_body_limits_duplicate_signature_browser_and_json(self):
        for extra in ({'Origin':'http://127.0.0.1:8794'}, {'Sec-Fetch-Site':'cross-site'},
                      {'Content-Length':'999999'}, {'Transfer-Encoding':'chunked'}):
            with self.subTest(extra=extra),self.assertRaises(ActionError): self.post(**extra)
        headers=Message()
        for key,val in {'Host':'callback.example','creem-signature':'a'*64}.items(): headers[key]=val
        headers['creem-signature']='b'*64
        with self.assertRaises(ActionError): self.app.preflight('POST',ROUTE,headers,8794)
        with self.assertRaises(ActionError) as caught: self.post(raw=b'{"id":1,"id":2}')
        self.assertEqual(caught.exception.code,'invalid_json')
        self.assertFalse(self.path.exists())

    def test_token_exemption_does_not_apply_to_other_routes(self):
        with self.assertRaises(ActionError) as caught:
            self.app.dispatch('POST','/api/coach/demo-review',{'Host':'127.0.0.1:8794','Origin':'http://127.0.0.1:8794'},b'{}')
        self.assertEqual(caught.exception.code,'token_denied')

    def test_no_creation_on_read_separate_database_and_no_input_mutation(self):
        self.assertEqual(self.store.current_status(),'none'); self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError): BillingStore(self.credentials,credential_path=self.credentials)
        value=event(); before=copy.deepcopy(value); self.store.handle(value)
        self.assertEqual(value,before)


if __name__ == '__main__': unittest.main()
