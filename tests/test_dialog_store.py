"""M6 offline SQL-contract tests. SQLite adapter is NOT a PostgreSQL acceptance.

The adapter executes real SQL after placeholder and row-lock translation. A
separate, explicit-DSN test covers PostgreSQL; neither path calls a model.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import auth
import db
import dialog_store as dialog
import memory_service as memory

NOW = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)


class SqliteCursor:
    def __init__(self, owner):
        self.owner = owner
        self.cursor = owner.conn.cursor()

    def execute(self, sql, params=()):
        self.owner.database.queries.append((" ".join(sql.split()), params))
        if self.owner.database.fail_on and self.owner.database.fail_on in sql:
            raise RuntimeError("private DSN or SQL failure")
        if sql.endswith("FOR UPDATE"):
            self.owner.conn.execute("BEGIN IMMEDIATE")
            sql = sql.removesuffix(" FOR UPDATE")
        self.cursor.execute(sql.replace("%s", "?"), params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        self.owner.database.closed_cursors += 1
        self.cursor.close()


class SqliteConnection:
    def __init__(self, database):
        self.database = database
        self.conn = sqlite3.connect(database.path, timeout=20)
        self.conn.execute("PRAGMA foreign_keys=ON")

    def cursor(self):
        return SqliteCursor(self)

    def commit(self):
        if self.database.fail_commit:
            raise RuntimeError("private commit failure")
        self.conn.commit()

    def rollback(self):
        self.database.rollbacks += 1
        self.conn.rollback()


class SqliteDatabase:
    def __init__(self, path):
        self.path = str(path)
        self.queries = []
        self.connections = self.released = self.closed_cursors = self.rollbacks = 0
        self.fail_on = None
        self.fail_commit = False

    def initialize(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(db.SCHEMA_SQL)
            for user in ("a", "b"):
                conn.execute("INSERT INTO users (id,device_id,token,created_at) VALUES (?,?,?,?)",
                             (user, "device-"+user, "token-"+user, NOW.isoformat()))

    def get_conn(self, database_url=None):
        self.connections += 1
        return SqliteConnection(self)

    def release_conn(self, conn):
        self.released += 1
        conn.conn.close()

    def patches(self):
        return patch.object(db, "get_conn", side_effect=self.get_conn), patch.object(
            db, "release_conn", side_effect=self.release_conn)


class DialogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = SqliteDatabase(Path(self.temp.name)/"dialog.sqlite3")
        self.database.initialize()
        for boundary in self.database.patches():
            boundary.start(); self.addCleanup(boundary.stop)

    def save(self, user="a", question="你好", answer="你好呀", minutes=1, **kw):
        return dialog.save_exchange(user, question, answer, NOW-timedelta(minutes=minutes), **kw)

    def test_atomic_pair_isolated_and_chronological(self):
        self.save(question="第一问", minutes=3)
        self.save("b", "不属于甲", minutes=2)
        result = self.save(question="第二问", minutes=1)
        self.assertEqual(dialog.get_recent("a", now=NOW), [
            {"q":"第一问", "a":"你好呀"}, {"q":"第二问", "a":"你好呀"}])
        listing = dialog.list_turns("a", export=True)
        self.assertEqual(listing["total_rounds"], 2)
        self.assertEqual([m["role"] for m in listing["turns"]], ["user","mark"]*2)
        self.assertEqual(listing["turns"][-1]["id"], result["turns"][-1]["id"])
        self.assertFalse(listing["has_more"])
        self.assertEqual(self.database.connections, self.database.released)
        self.assertEqual(self.database.connections, self.database.closed_cursors)

    def test_recent_five_ROUNDS_not_five_messages(self):
        for i in range(7):
            self.save(question=f"问题{i}", minutes=10-i)
        value = dialog.get_recent("a", now=NOW)
        self.assertEqual(len(value), 5)
        self.assertEqual([m["q"] for m in value], [f"问题{i}" for i in range(2,7)])
        self.assertEqual(len(dialog.get_recent("a", n=2, now=NOW)), 2)

    def test_one_hour_boundary_future_and_offset(self):
        self.save(question="过期", minutes=61)
        self.save(question="边界", minutes=60)
        dialog.save_exchange("a", "未来", "未来回答", NOW+timedelta(seconds=1))
        dialog.save_exchange("a", "最新", "最新回答", NOW.astimezone(timezone(timedelta(hours=8))))
        self.assertEqual([m["q"] for m in dialog.get_recent("a", now=NOW)], ["边界","最新"])
        self.assertNotIn("+08:00", json.dumps(dialog.list_turns("a", export=True)))

    def test_required_single_turn_api_pairs_and_rejects_ambiguity(self):
        user = dialog.save_turn("a", "user", "单条问题", NOW-timedelta(minutes=2))
        self.assertIsNone(dialog.get_recent("a", now=NOW))
        with self.assertRaises(ValueError):
            dialog.save_turn("a", "user", "会错配", NOW)
        with self.assertRaises(ValueError):
            dialog.save_turn("b", "mark", "没有问题", NOW)
        mark = dialog.save_turn("a", "mark", "单条回答", NOW-timedelta(minutes=1))
        self.assertEqual(user["exchange_id"], mark["exchange_id"])
        self.assertEqual(dialog.get_recent("a", now=NOW), [{"q":"单条问题","a":"单条回答"}])

    def test_single_delete_is_hard_idempotent_and_never_crosses_users(self):
        pair = self.save()
        identifier = pair["turns"][0]["id"]
        self.assertEqual(dialog.delete_one("b", identifier), {"deleted":0})
        self.assertEqual(dialog.delete_one("a", identifier), {"deleted":1})
        self.assertEqual(dialog.delete_one("a", identifier), {"deleted":0})
        self.assertIsNone(dialog.get_recent("a", now=NOW))
        remaining = dialog.list_turns("a", export=True)
        self.assertEqual(remaining["total_messages"], 1)
        self.assertNotIn(identifier, json.dumps(remaining))
        with closing(sqlite3.connect(self.database.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dialog_turns WHERE id=?", (identifier,)).fetchone()[0],0)

    def test_delete_all_hard_scoped_and_idempotent(self):
        self.save(); self.save("b")
        self.assertEqual(dialog.delete_all("a"), {"deleted":2})
        self.assertEqual(dialog.delete_all("a"), {"deleted":0})
        self.assertIsNone(dialog.get_recent("a", now=NOW))
        self.assertEqual(dialog.list_turns("b")["total_messages"],2)

    def test_all_writers_and_deleters_take_user_lock(self):
        self.save(); dialog.delete_all("a")
        queries = self.database.queries
        locks = [i for i, (sql,args) in enumerate(queries) if sql.endswith("FOR UPDATE")]
        self.assertEqual(len(locks),2)
        self.assertEqual(queries[locks[0]][1], ("a",))
        self.assertLess(locks[0], next(i for i,(sql,_) in enumerate(queries) if sql.startswith("INSERT")))

    def test_capacity_no_silent_pruning_then_confirmed_whole_round_cleanup(self):
        with patch.object(dialog, "MAX_ROUNDS", 3):
            for i in range(3): self.save(question=f"问题{i}", minutes=10-i)
            with self.assertRaises(dialog.DialogCapacityError): dialog.check_capacity("a")
            with self.assertRaises(dialog.DialogCapacityError): self.save(question="不应保存")
            listing = dialog.list_turns("a", limit=1, export=True)
            self.assertTrue(listing["export_required"])
            self.assertEqual(len(listing["turns"]),6)
            self.assertFalse(listing["has_more"])
            dialog.check_capacity("a", prune_confirmed=True)
            result = self.save(question="确认后新问", minutes=1, prune_confirmed=True)
            self.assertEqual(result["pruned_messages"],2)
            listing = dialog.list_turns("a", export=True)
            self.assertEqual(listing["total_rounds"],3)
            self.assertNotIn("问题0",json.dumps(listing,ensure_ascii=False))
            self.assertIn("问题1",json.dumps(listing,ensure_ascii=False))

    def test_pruning_rolls_back_with_pair_if_second_message_fails(self):
        self.save(question="原先", minutes=5)
        with patch.object(dialog,"MAX_ROUNDS",1), patch.object(dialog,"_insert", wraps=dialog._insert) as insert:
            original = dialog._insert
            def fail_mark(cursor, uid, eid, role, text, stamp):
                if role == "mark": raise RuntimeError("private write failure")
                return original(cursor,uid,eid,role,text,stamp)
            insert.side_effect = fail_mark
            with self.assertRaises(db.DatabaseError) as caught:
                self.save(question="新问", prune_confirmed=True)
        self.assertNotIn("private",str(caught.exception))
        self.assertEqual(dialog.get_recent("a", now=NOW),[{"q":"原先","a":"你好呀"}])
        self.assertGreater(self.database.rollbacks,0)

    def test_commit_failure_rolls_back_and_releases(self):
        self.database.fail_commit = True
        with self.assertRaises(db.DatabaseError): self.save()
        self.database.fail_commit = False
        self.assertEqual(dialog.list_turns("a")["total_messages"],0)
        self.assertEqual(self.database.connections,self.database.released)

    def test_sql_failure_is_sanitized(self):
        self.database.fail_on = "COUNT(*)"
        with self.assertRaises(db.DatabaseError) as caught: self.save()
        self.assertNotIn("private",str(caught.exception))
        self.assertEqual(self.database.connections,self.database.released)

    def test_unknown_identity_does_not_create_rows(self):
        with self.assertRaises(db.DatabaseError): self.save("unknown")
        self.assertEqual(dialog.list_turns("a")["total_messages"],0)

    def test_invalid_fields_rejected_before_storage(self):
        cases = [(None,"user","text"), ("a","system","text"), ("a","mark",b"audio"),
                 ("a","user","x"*201), ("a","mark","x"*481), ("a","user"," "),
                 ("a","user","a\nb"), ("a","mark","a\x7fb"), ("a",[],"x")]
        for user,role,text in cases:
            with self.subTest(role=role), self.assertRaises(ValueError):
                dialog.save_turn(user,role,text,NOW)
        for options in ({"n":True},{"n":0},{"n":6},{"within_hours":0},{"within_hours":True},
                        {"now":datetime(2026,11,12)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                dialog.get_recent("a",**options)
        for options in ({"limit":True},{"limit":2001},{"export":1}):
            with self.assertRaises(ValueError): dialog.list_turns("a",**options)
        with self.assertRaises(ValueError): self.save(prune_confirmed="yes")
        with self.assertRaises(ValueError): dialog.delete_one("a","' OR 1=1")
        with self.assertRaises(ValueError): dialog.save_exchange("a","问题","答",datetime(2026,11,12))
        self.assertEqual(self.database.connections,0)

    def test_malformed_stored_text_fails_closed(self):
        pair = self.save()
        with closing(sqlite3.connect(self.database.path)) as conn, conn:
            conn.execute("UPDATE dialog_turns SET content=? WHERE id=?",("bad\ntext",pair["turns"][0]["id"]))
        with self.assertRaises(db.DatabaseError): dialog.get_recent("a", now=NOW)
        with self.assertRaises(db.DatabaseError): dialog.list_turns("a")

    def test_concurrent_rounds_keep_pairing_and_retention_limit(self):
        def write(i):
            try:
                dialog.save_exchange("a",f"问题{i}",f"回答{i}",NOW)
                return True
            except dialog.DialogCapacityError:
                return False
        with patch.object(dialog,"MAX_ROUNDS",4), ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(write,range(8)))
        self.assertEqual(sum(results),4)
        listing = dialog.list_turns("a",export=True)
        self.assertEqual(listing["total_rounds"],4)
        for turn in dialog.get_recent("a",now=NOW):
            self.assertEqual(turn["q"].removeprefix("问题"),turn["a"].removeprefix("回答"))

    def test_persists_across_new_python_process(self):
        self.save(question="重启后仍在")
        script = """
import json,sys
from unittest.mock import patch
from tests.test_dialog_store import SqliteDatabase, NOW
import db,dialog_store
store=SqliteDatabase(sys.argv[1])
with patch.object(db,'get_conn',side_effect=store.get_conn), patch.object(db,'release_conn',side_effect=store.release_conn):
    print(json.dumps(dialog_store.get_recent('a',now=NOW),ensure_ascii=False))
"""
        result = subprocess.run([sys.executable,"-c",script,self.database.path],
                                cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding="utf-8",env={**os.environ,"PYTHONIOENCODING":"utf-8"},timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[{"q":"重启后仍在","a":"你好呀"}])


class DialogBudgetTests(unittest.TestCase):
    def context(self, turns):
        with patch.object(memory,"_records",return_value=[]), patch.object(dialog,"get_recent",return_value=copy.deepcopy(turns)) as get:
            value = memory.get_context_for_chat("a","继续",now=NOW)
        get.assert_called_once_with("a",n=5,within_hours=1,database_url=None,now=NOW)
        return value

    def test_all_five_short_rounds_fit_budget_and_builder_projection(self):
        turns = [{"q":"你好","a":"你好呀"} for _ in range(5)]
        value = self.context(turns)
        self.assertEqual(value["recent_dialog"],turns)
        self.assertEqual(memory._projection(value,"chat")["recent_dialog"],turns)
        self.assertLessEqual(memory.memory_upper_bound(value,"chat"),800)

    def test_budget_drops_whole_oldest_rounds_never_slices_text(self):
        turns = [{"q":f"问题{i}","a":"回答"*100} for i in range(5)]
        before = copy.deepcopy(turns)
        value = self.context(turns)
        self.assertEqual(turns,before)
        self.assertLessEqual(memory.memory_upper_bound(value,"chat"),800)
        self.assertLess(len(value["recent_dialog"]),5)
        if value["recent_dialog"]:
            self.assertEqual(value["recent_dialog"],turns[-len(value["recent_dialog"]):])

    def test_storage_outage_never_reads_shared_local_dialog(self):
        with patch.object(memory,"_records",return_value=[]), patch.object(dialog,"get_recent",side_effect=db.DatabaseOperationError("offline")):
            with self.assertRaises(db.DatabaseError): memory.get_context_for_chat("a","继续",now=NOW)


@unittest.skipUnless(os.environ.get("MARK_TEST_DATABASE_URL", "").strip(),
                     "Explicit disposable PostgreSQL test database is not configured")
class DialogPostgresTests(unittest.TestCase):
    def test_pair_isolation_concurrency_deletion_and_reconnect(self):
        dsn = os.environ["MARK_TEST_DATABASE_URL"]
        users = []
        db.close()
        try:
            db.init_db(dsn)
            for _ in range(2):
                users.append(auth.register_device("mark-dialog-"+uuid.uuid4().hex,dsn)["user_id"])
            a,b=users
            result = dialog.save_exchange(a,"测试问题","测试回答",database_url=dsn)
            self.assertIsNone(dialog.get_recent(b,database_url=dsn))
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i: dialog.save_exchange(a,f"问题{i}",f"回答{i}",database_url=dsn),range(6)))
            db.close()
            self.assertEqual(len(dialog.get_recent(a,database_url=dsn)),5)
            identifier=result["turns"][0]["id"]
            self.assertEqual(dialog.delete_one(b,identifier,database_url=dsn),{"deleted":0})
            self.assertEqual(dialog.delete_one(a,identifier,database_url=dsn),{"deleted":1})
            self.assertEqual(dialog.delete_all(a,database_url=dsn),{"deleted":13})
            self.assertEqual(dialog.list_turns(a,export=True,database_url=dsn)["turns"],[])
        except db.DatabaseError:
            raise AssertionError("Dialog PostgreSQL acceptance failed; inspect disposable database privately") from None
        finally:
            try:
                if users:
                    conn=db.get_conn(dsn)
                    try:
                        cursor=conn.cursor()
                        for user in users: cursor.execute("DELETE FROM users WHERE id=%s",(user,))
                        conn.commit(); cursor.close()
                    finally: db.release_conn(conn)
            except Exception:
                raise AssertionError("Dialog PostgreSQL cleanup failed; inspect privately") from None
            finally: db.close()


if __name__ == "__main__":
    unittest.main()
