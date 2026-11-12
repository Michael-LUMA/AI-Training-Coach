"""M7 SQL contract, HTTP gates and atomic ownership tests; offline, not PG."""
from contextlib import closing
import json
import base64
import http.client
import os
from threading import Thread
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import auth
import coach_server as server
import data_ownership as owner
import db
import dialog_store
from action_cards import ActionError
from tests.test_dialog_store import SqliteDatabase


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.database = SqliteDatabase(Path(self.temp.name)/"ownership.sqlite3")
        self.database.initialize()
        import sqlite3
        with closing(sqlite3.connect(self.database.path)) as connection, connection:
            for user in ('a','b'):
                connection.execute("INSERT INTO rides (id, user_id, dataset, created_at, schema_version, updated_at) VALUES (?, ?, ?, ?, 'test/1', '2026-11-12')", (user+'-ride',user,json.dumps({'id':user+'-ride','distance_m':10}), '2026-11-12'))
                connection.execute("INSERT INTO profiles VALUES (?, ?, ?, ?)",(user,'{"usual_start_hour":8}',3,'2026-11-12'))
        for boundary in self.database.patches():
            boundary.start(); self.addCleanup(boundary.stop)
        for user in ('a','b'):
            dialog_store.save_exchange(user, user+' question', user+' answer')
            owner.add_note(user,user+' note')
        self.app=server.App(server.CoachStore(Path(self.temp.name)/'coach.sqlite3'))

    def request(self,path,payload,browser=True):
        raw=json.dumps(payload).encode()
        return self.app.dispatch('POST',path,{'Host':'127.0.0.1:8794','Origin':'http://127.0.0.1:8794',
              'X-Coach-Token':self.app.token if browser else '', 'Content-Type':'application/json','Content-Length':str(len(raw))},raw)

    def test_complete_four_categories_no_secrets_or_foreign_data(self):
        exported=self.request('/api/data/export',{'token':'token-a'})
        self.assertEqual(set(exported)&{'rides','profile','dialog','notes'}, {'rides','profile','dialog','notes'})
        self.assertEqual(len(exported['rides']),1); self.assertEqual(len(exported['dialog']),2)
        self.assertEqual(len(exported['notes']),1); self.assertEqual(exported['profile']['sample_count'],3)
        raw=json.dumps(exported)
        self.assertNotIn('token-a',raw); self.assertNotIn('b question',raw); self.assertNotIn('b note',raw)
        self.assertFalse(exported['local_data_included'])

    def test_hard_delete_counts_revokes_identity_and_preserves_other_user(self):
        before=owner.export_data('b')
        result=self.request('/api/data/delete',{'token':'token-a','confirm':True})
        self.assertEqual(result['deleted'],{'rides':1,'profiles':1,'dialog_turns':2,'notes':1,'users':1})
        self.assertTrue(result['token_revoked'])
        with self.assertRaises(auth.InvalidToken): auth.whoami('token-a')
        self.assertEqual(before['notes'],owner.export_data('b')['notes'])
        with closing(self.database.get_conn().conn) as connection:
            for table,key in [('rides','user_id'),('profiles','user_id'),('dialog_turns','user_id'),('notes','user_id'),('users','id')]:
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table} WHERE {key}=?',('a',)).fetchone()[0],0)
        for write in (lambda:owner.add_note('a','resurrection'),lambda:dialog_store.save_exchange('a','q','a')):
            with self.assertRaises(db.DatabaseError): write()

    def test_confirmation_requires_literal_true_and_rejects_extra_identity(self):
        for payload in ({'token':'token-a'}, {'token':'token-a','confirm':False}, {'token':'token-a','confirm':1},
                        {'token':'token-a','confirm':'true'}, {'token':'token-a','confirm':True,'user_id':'b'}):
            with self.subTest(payload=payload),self.assertRaises(ActionError) as caught:
                self.request('/api/data/delete',payload)
            self.assertEqual(caught.exception.status,400)
        self.assertEqual(auth.whoami('token-a'),'a')

    def test_invalid_tokens_browser_gate_and_input_fields(self):
        for path in server.DATA_ROUTES:
            for payload in ({},{'token':'bad'}):
                with self.subTest(path=path),self.assertRaises(ActionError) as caught: self.request(path,payload)
                self.assertEqual(caught.exception.status,401)
            with self.assertRaises(ActionError) as caught: self.request(path,{'token':'token-a'},False)
            self.assertEqual(caught.exception.status,403)
        with self.assertRaises(ActionError): self.request('/api/data/export',{'token':'token-a','user_id':'b'})

    def test_delete_transaction_rolls_back_every_table_on_failure(self):
        self.database.fail_on='DELETE FROM notes'
        with self.assertRaises(db.DatabaseError): owner.delete_data('a',True)
        self.database.fail_on=None
        exported=owner.export_data('a')
        self.assertEqual(len(exported['rides']),1);self.assertEqual(len(exported['dialog']),2)
        self.assertEqual(auth.whoami('token-a'),'a')

    def test_commit_failure_does_not_claim_deletion(self):
        self.database.fail_commit=True
        with self.assertRaises(db.DatabaseError): owner.delete_data('a',True)
        self.database.fail_commit=False
        self.assertEqual(len(owner.export_data('a')['dialog']),2)

    def test_empty_categories_and_full_dialog_not_default_page(self):
        for i in range(55): dialog_store.save_exchange('a',f'q{i}',f'a{i}')
        self.assertEqual(len(owner.export_data('a')['dialog']),112)
        owner.delete_data('b',True)
        with self.assertRaises(db.DatabaseError): owner.export_data('b')

    def test_cloud_notes_validation_limit_and_isolation(self):
        note=self.request('/api/notes/create',{'token':'token-a','text':'  记住我喜欢晨跑  '})
        self.assertEqual(len(self.request('/api/notes/list',{'token':'token-a'})['notes']),2)
        self.assertEqual(self.request('/api/notes/delete',{'token':'token-b','id':note['id']}),{'deleted':0})
        self.assertEqual(self.request('/api/notes/delete',{'token':'token-a','id':note['id']}),{'deleted':1})
        for text in ('', 'x'*141, None):
            with self.assertRaises(ActionError): self.request('/api/notes/create',{'token':'token-a','text':text})
        with patch.object(owner,'MAX_NOTES',1),self.assertRaises(ValueError): owner.add_note('a','overflow')

    def test_local_shared_notes_are_neither_exported_nor_deleted(self):
        path=self.app.notes_path;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('shared local data',encoding='utf-8')
        self.assertNotIn('shared local data',json.dumps(owner.export_data('a')))
        owner.delete_data('a',True)
        self.assertEqual(path.read_text(encoding='utf-8'),'shared local data')

    def test_cloud_live_cache_cleared_only_for_deleted_user(self):
        self.app._live={('a','ride'):object(),('b','ride'):object(),'anonymous':object()}
        self.request('/api/data/delete',{'token':'token-a','confirm':True})
        self.assertNotIn(('a','ride'),self.app._live);self.assertIn(('b','ride'),self.app._live)


    def test_real_http_private_origin_browser_and_cloud_gates(self):
        password='synthetic-m7-private-password-1234'
        basic='Basic '+base64.b64encode(('boss:'+password).encode()).decode()
        with patch.dict(os.environ, {'LUMA_COACH_TEST_PASSWORD':password}, clear=True), \
                patch.object(server, '_bind_config', return_value=('127.0.0.1',0)):
            httpd=server.create_server(app=self.app)
            worker=Thread(target=httpd.serve_forever,kwargs={'poll_interval':0.02},daemon=True)
            worker.start()
            try:
                def request(path, payload, *, login=True, browser=True, origin=None):
                    headers={'Content-Type':'application/json', 'Origin':origin or 'http://127.0.0.1:'+str(httpd.server_port)}
                    if login: headers['Authorization']=basic
                    if browser: headers['X-Coach-Token']=self.app.token
                    with closing(http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)) as client:
                        client.request('POST',path,json.dumps(payload).encode(),headers)
                        response=client.getresponse()
                        return response.status, dict(response.getheaders()), json.loads(response.read())
                for path in server.DATA_ROUTES:
                    with self.subTest(path=path):
                        status, headers, _=request(path,{'token':'token-a'},login=False)
                        self.assertEqual(status,401);self.assertIn('WWW-Authenticate',headers)
                        self.assertEqual(headers['Cache-Control'],'no-store')
                        self.assertEqual(request(path,{'token':'token-a'},browser=False)[0],403)
                        self.assertEqual(request(path,{'token':'token-a'},origin='https://evil.invalid')[0],403)
                        self.assertEqual(request(path,{'token':'bad'})[0],401)
                self.assertEqual(request('/api/data/export',{'token':'token-a'})[0],200)
                self.assertEqual(request('/api/data/delete',{'token':'token-a','confirm':False})[0],400)
                self.assertEqual(auth.whoami('token-a'),'a')
                status, _, result=request('/api/data/delete',{'token':'token-a','confirm':True})
                self.assertEqual(status,200);self.assertTrue(result['token_revoked'])
                self.assertEqual(request('/api/data/export',{'token':'token-a'})[0],401)
                self.assertEqual(request('/api/data/export',{'token':'token-b'})[0],200)
            finally:
                httpd.shutdown();httpd.server_close();worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__=='__main__': unittest.main()
