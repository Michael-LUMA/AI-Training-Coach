import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

import db


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and self.owner.raise_on_execute:
            return False
        return False

    def execute(self, sql):
        self.executed.append(sql)
        self.owner.executed.append(sql)
        if self.owner.raise_on_execute:
            raise RuntimeError("schema rejected")


class FakeConnection:
    def __init__(self, owner):
        self.owner = owner
        self.commits = 0
        self.rollbacks = 0
        self.executed = []
        self.raise_on_execute = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    instances = []
    raise_on_create = None

    def __init__(self, minconn, maxconn, dsn=None):
        if self.raise_on_create:
            raise self.raise_on_create
        self.minconn = minconn
        self.maxconn = maxconn
        self.dsn = dsn
        self.conn = FakeConnection(self)
        self.closed = False
        self.put = []
        type(self).instances.append(self)

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        self.put.append(conn)

    def closeall(self):
        self.closed = True


class DbTests(unittest.TestCase):
    def setUp(self):
        db.close()
        FakePool.instances.clear()
        FakePool.raise_on_create = None

    def tearDown(self):
        db.close()

    def fake_driver(self):
        driver = types.SimpleNamespace()
        driver.pool = types.SimpleNamespace(SimpleConnectionPool=FakePool)
        return driver

    def test_missing_database_url_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(db.DatabaseNotConfigured):
                db.get_conn()

    def test_driver_missing_is_explicit(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@db/mark"}, clear=True):
            with patch.object(importlib, "import_module", side_effect=ModuleNotFoundError("psycopg2")):
                with self.assertRaises(db.DatabaseDriverUnavailable):
                    db.get_conn()

    def test_pool_bounds_and_connection_release(self):
        driver = self.fake_driver()
        with patch.object(importlib, "import_module", return_value=driver):
            conn = db.get_conn("postgresql://user:secret@db/mark")
            pool = FakePool.instances[-1]
            self.assertEqual((1, 5), (pool.minconn, pool.maxconn))
            self.assertIs(conn, pool.conn)
            db.release_conn(conn)
            self.assertEqual([conn], pool.put)

    def test_init_schema_commits_and_returns_connection(self):
        driver = self.fake_driver()
        with patch.object(importlib, "import_module", return_value=driver):
            db.init_db("postgresql://db/mark")
            pool = FakePool.instances[-1]
            self.assertEqual(1, pool.conn.commits)
            self.assertEqual(0, pool.conn.rollbacks)
            self.assertIn("CREATE TABLE IF NOT EXISTS rides", pool.conn.executed[0])
            self.assertIn("JSONB", pool.conn.executed[0])
            self.assertEqual([pool.conn], pool.put)

    def test_init_schema_rolls_back_and_raises(self):
        driver = self.fake_driver()
        FakePool.raise_on_create = None
        with patch.object(importlib, "import_module", return_value=driver):
            pool_conn = db.get_conn("postgresql://db/mark")
            pool_conn.raise_on_execute = True
            db.release_conn(pool_conn)
            with self.assertRaises(db.DatabaseOperationError):
                db.init_db("postgresql://db/mark")
            self.assertEqual(1, pool_conn.rollbacks)

    def test_connection_failure_is_not_reported_as_success(self):
        driver = self.fake_driver()
        FakePool.raise_on_create = RuntimeError("connection refused")
        with patch.object(importlib, "import_module", return_value=driver):
            with self.assertRaises(db.DatabaseConnectionError) as context:
                db.get_conn("postgresql://db/mark")
        self.assertIn("connection refused", str(context.exception))

    def test_close_is_idempotent(self):
        driver = self.fake_driver()
        with patch.object(importlib, "import_module", return_value=driver):
            db.get_conn("postgresql://db/mark")
            pool = FakePool.instances[-1]
            db.close()
            db.close()
            self.assertTrue(pool.closed)

    def test_cloud_summary_is_copied_and_accepted(self):
        summary = {"duration_s": 1800, "heart_rate": {"avg": 142}, "segments": [{"pace": 5.2}]}
        result = db.validate_cloud_summary(summary)
        self.assertEqual(result, summary)
        self.assertIsNot(result, summary)

    def test_cloud_summary_rejects_raw_fields_recursively(self):
        for payload in (
            {"series": [1, 2]},
            {"metrics": {"gps": {"lat": 1, "lon": 2}}},
            {"sensor_data": [{"value": 1}]},
            {"activity": [{"coordinates": [[1, 2]]}]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(db.CloudDataError):
                    db.validate_cloud_summary(payload)


if __name__ == "__main__":
    unittest.main()


