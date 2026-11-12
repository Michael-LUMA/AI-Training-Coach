"""Opt-in real PostgreSQL acceptance; only MARK_TEST_DATABASE_URL is used.

Never point this at production. Creates isolated random devices and removes only
those users and rides. No DSN, bearer tokens or driver errors are printed.
"""
import os
import unittest
import uuid
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import auth
import cloud_rides
import db
import profile_service
from tests.test_cloud_rides import summary


@unittest.skipUnless(os.environ.get("MARK_TEST_DATABASE_URL", "").strip(),
                     "Explicit disposable PostgreSQL test database is not configured")
class PostgreSQLAcceptanceTests(unittest.TestCase):
    @patch("cloud_rides._refresh_profile")
    def test_identity_isolation_replay_concurrency_and_reconnect(self, _refresh):
        dsn = os.environ["MARK_TEST_DATABASE_URL"]
        identities = []
        db.close()
        try:
            db.init_db(dsn)
            device = "mark-acceptance-" + uuid.uuid4().hex
            def register_once(_):
                try:
                    return auth.register_device(device, dsn)
                except auth.DeviceAlreadyRegistered:
                    return None
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(register_once, range(4)))
            registered = [identity for identity in results if identity is not None]
            self.assertEqual(len(registered), 1)
            identities.append(registered[0])
            other = auth.register_device("mark-acceptance-" + uuid.uuid4().hex, dsn)
            identities.append(other)
            user = registered[0]["user_id"]
            self.assertEqual(auth.whoami(registered[0]["token"], dsn), user)
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: cloud_rides.sync(user, [summary()], dsn), range(4)))
            self.assertEqual(sum(result["saved"] for result in results), 1)
            self.assertEqual(sum(result["duplicates"] for result in results), 3)
            self.assertEqual(cloud_rides.list_rides(other["user_id"], database_url=dsn)["rides"], [])
            cloud_rides.sync(other["user_id"], [summary()], dsn)
            db.close()
            self.assertEqual(cloud_rides.get_ride(user, "ride-1", dsn)["ride"], summary())
            changed = summary(); changed["coach_notes"] = ["conflict"]
            with self.assertRaises(cloud_rides.CloudRideConflict):
                cloud_rides.sync(user, [summary("new"), changed], dsn)
            with self.assertRaises(cloud_rides.CloudRideNotFound):
                cloud_rides.get_ride(user, "new", dsn)
            cloud_rides.delete_ride(user, "ride-1", dsn)
            self.assertEqual(cloud_rides.get_ride(other["user_id"], "ride-1", dsn)["ride"], summary())
        except db.DatabaseError:
            raise AssertionError("PostgreSQL acceptance failed; check the disposable database configuration") from None
        finally:
            try:
                if identities:
                    conn = db.get_conn(dsn)
                    try:
                        with conn.cursor() as cursor:
                            for identity in identities:
                                cursor.execute("DELETE FROM rides WHERE user_id = %s", (identity["user_id"],))
                                cursor.execute("DELETE FROM users WHERE id = %s", (identity["user_id"],))
                        conn.commit()
                    finally:
                        db.release_conn(conn)
            except Exception:
                raise AssertionError("PostgreSQL acceptance cleanup failed; inspect the disposable database privately") from None
            finally:
                db.close()


    @patch("cloud_rides._refresh_profile")
    def test_profile_cache_isolation_invalidation_concurrency_and_reconnect(self, _refresh):
        from tests.test_profile_service import NOW, record
        dsn = os.environ["MARK_TEST_DATABASE_URL"]
        identities = []
        db.close()
        try:
            db.init_db(dsn)
            for _ in range(2):
                identities.append(auth.register_device("mark-profile-" + uuid.uuid4().hex, dsn))
            user, other = [identity["user_id"] for identity in identities]
            cloud_rides.sync(user, [record(i) for i in range(1, 4)], dsn)
            expected = profile_service.compute_profile(user, dsn, now=NOW)
            self.assertEqual(expected["total_sessions"], 3)
            self.assertIsNone(profile_service.get_profile(other, dsn, now=NOW))
            db.close()
            self.assertEqual(profile_service.get_profile(user, dsn, now=NOW), expected)
            with ThreadPoolExecutor(max_workers=3) as executor:
                write = executor.submit(cloud_rides.sync, user, [record(4)], dsn)
                refresh = executor.submit(profile_service.compute_profile, user, dsn, now=NOW)
                write.result(); refresh.result()
            self.assertEqual(profile_service.get_profile(user, dsn, now=NOW)["total_sessions"], 4)
            with ThreadPoolExecutor(max_workers=3) as executor:
                delete = executor.submit(cloud_rides.delete_ride, user, "profile-1", dsn)
                refresh = executor.submit(profile_service.compute_profile, user, dsn, now=NOW)
                delete.result(); refresh.result()
            self.assertEqual(profile_service.get_profile(user, dsn, now=NOW)["total_sessions"], 3)
            cloud_rides.delete_ride(user, "profile-2", dsn)
            self.assertIsNone(profile_service.get_profile(user, dsn, now=NOW))
            conn = db.get_conn(dsn)
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM profiles WHERE user_id = %s", (user,))
                    self.assertEqual(cursor.fetchone()[0], 0)
            finally:
                db.release_conn(conn)
        except db.DatabaseError:
            raise AssertionError("Profile PostgreSQL acceptance failed; check the disposable database privately") from None
        finally:
            try:
                if identities:
                    conn = db.get_conn(dsn)
                    try:
                        with conn.cursor() as cursor:
                            for identity in identities:
                                for table in ("profiles", "rides"):
                                    cursor.execute("DELETE FROM " + table + " WHERE user_id = %s", (identity["user_id"],))
                                cursor.execute("DELETE FROM users WHERE id = %s", (identity["user_id"],))
                        conn.commit()
                    finally:
                        db.release_conn(conn)
            except Exception:
                raise AssertionError("Profile acceptance cleanup failed; inspect the disposable database privately") from None
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
