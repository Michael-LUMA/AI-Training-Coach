"""Opt-in real PostgreSQL M7 tests; use a disposable database/schema only."""
import os
import unittest
import uuid
from unittest.mock import patch

import auth
import cloud_rides
import data_ownership as ownership
import db
import dialog_store
import profile_service
from tests.test_profile_service import NOW, record


@unittest.skipUnless(os.environ.get('MARK_TEST_DATABASE_URL', '').strip(),
                     'Explicit disposable PostgreSQL test database is not configured')
class OwnershipPostgresTests(unittest.TestCase):
    def test_full_export_rollback_hard_delete_isolation_and_no_resurrection(self):
        from psycopg2 import sql
        dsn = os.environ['MARK_TEST_DATABASE_URL']
        identities = []
        db.close()
        trigger_created = False
        try:
            db.init_db(dsn)
            for _ in range(2):
                identities.append(auth.register_device('m7-pg-' + uuid.uuid4().hex, dsn))
            a, b = [account['user_id'] for account in identities]
            with patch.object(cloud_rides, '_refresh_profile'):
                cloud_rides.sync(a, [record(i) for i in (1, 2, 3)], dsn)
                cloud_rides.sync(b, [record(4)], dsn)
            profile_service.compute_profile(a, dsn, now=NOW)
            for user in (a, b):
                dialog_store.save_exchange(user, 'M7 synthetic question', 'M7 synthetic answer', database_url=dsn)
                ownership.add_note(user, 'M7 synthetic note', dsn)
            exported = ownership.export_data(a, dsn)
            other = ownership.export_data(b, dsn)
            self.assertEqual(len(exported['rides']), 3)
            self.assertEqual(len(exported['dialog']), 2)
            self.assertEqual(len(exported['notes']), 1)
            self.assertEqual(exported['profile']['sample_count'], 3)
            self.assertNotIn(identities[0]['token'], str(exported))
            # A real PostgreSQL trigger fails late in the delete transaction.
            conn = db.get_conn(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("CREATE FUNCTION m7_reject_note_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.user_id = {} THEN RAISE EXCEPTION 'synthetic M7 delete failure'; END IF; RETURN OLD; END $$").format(sql.Literal(a)))
                    cur.execute('CREATE TRIGGER m7_reject_note_delete BEFORE DELETE ON notes FOR EACH ROW EXECUTE FUNCTION m7_reject_note_delete()')
                conn.commit(); trigger_created = True
            finally:
                db.release_conn(conn)
            with self.assertRaises(db.DatabaseError):
                ownership.delete_data(a, True, dsn)
            self.assertEqual(auth.whoami(identities[0]['token'], dsn), a)
            after = ownership.export_data(a, dsn)
            for key in ('rides', 'profile', 'dialog', 'notes'):
                self.assertEqual(after[key], exported[key])
            self._drop_trigger(dsn); trigger_created = False
            deleted = ownership.delete_data(a, True, dsn)
            self.assertEqual(deleted['deleted'], {'rides': 3, 'profiles': 1, 'dialog_turns': 2, 'notes': 1, 'users': 1})
            with self.assertRaises(auth.InvalidToken):
                auth.whoami(identities[0]['token'], dsn)
            for operation in (lambda: ownership.add_note(a, 'must not return', dsn),
                              lambda: dialog_store.save_exchange(a, 'q', 'a', database_url=dsn),
                              lambda: cloud_rides.sync(a, [record(5)], dsn)):
                with self.assertRaises(db.DatabaseError): operation()
            conn = db.get_conn(dsn)
            try:
                with conn.cursor() as cur:
                    for table, key in (('rides', 'user_id'), ('profiles', 'user_id'), ('dialog_turns', 'user_id'), ('notes', 'user_id'), ('users', 'id')):
                        cur.execute(sql.SQL('SELECT count(*) FROM {} WHERE {} = %s').format(sql.Identifier(table), sql.Identifier(key)), (a,))
                        self.assertEqual(cur.fetchone()[0], 0)
                conn.commit()
            finally: db.release_conn(conn)
            other_after = ownership.export_data(b, dsn)
            for key in ('rides', 'profile', 'dialog', 'notes'):
                self.assertEqual(other_after[key], other[key])
        finally:
            if trigger_created: self._drop_trigger(dsn)
            for account in identities:
                try: auth.whoami(account['token'], dsn)
                except auth.InvalidToken: continue
                ownership.delete_data(account['user_id'], True, dsn)
            db.close()

    @staticmethod
    def _drop_trigger(dsn):
        conn = db.get_conn(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute('DROP TRIGGER IF EXISTS m7_reject_note_delete ON notes')
                cur.execute('DROP FUNCTION IF EXISTS m7_reject_note_delete()')
            conn.commit()
        finally: db.release_conn(conn)
