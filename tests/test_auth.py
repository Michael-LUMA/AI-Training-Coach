import unittest
from unittest.mock import patch

import auth
import db


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False
        self._next_row = None

    def execute(self, sql, params):
        self.owner.executed.append((sql, params))
        if self.owner.raise_on_execute is not None:
            raise self.owner.raise_on_execute
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("SELECT ID, TOKEN FROM USERS"):
            self._next_row = self.owner.existing_row
        elif normalized.startswith("SELECT ID FROM USERS"):
            self._next_row = self.owner.token_row
        elif normalized.startswith("INSERT INTO USERS"):
            self._next_row = self.owner.inserted_row

    def fetchone(self):
        row = self._next_row
        self._next_row = None
        return row

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.executed = []
        self.existing_row = None
        self.inserted_row = None
        self.token_row = None
        self.raise_on_execute = None
        self.cursor_instance = None

    def cursor(self):
        self.cursor_instance = FakeCursor(self)
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.released = []
        self.get_conn_calls = []

        def fake_get_conn(database_url=None):
            self.get_conn_calls.append(database_url)
            return self.conn

        def fake_release_conn(conn):
            self.released.append(conn)

        self.get_conn_patch = patch.object(auth.db, "get_conn", side_effect=fake_get_conn)
        self.release_patch = patch.object(auth.db, "release_conn", side_effect=fake_release_conn)
        self.get_conn_patch.start()
        self.release_patch.start()

    def tearDown(self):
        self.release_patch.stop()
        self.get_conn_patch.stop()

    def test_cursor_close_failure_still_returns_connection_without_driver_details(self):
        self.conn.inserted_row = ("user-1", "token-1")
        with patch.object(FakeCursor, "close", side_effect=RuntimeError("secret driver detail")):
            with self.assertRaises(auth.AuthStorageError) as caught:
                auth.register_device("device-a")
        self.assertEqual([self.conn], self.released)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("driver", str(caught.exception))

    def test_first_registration_creates_uuid_and_token(self):
        token = "generated-token"
        self.conn.inserted_row = ("11111111-1111-4111-8111-111111111111", token)
        with patch.object(auth.secrets, "token_urlsafe", return_value=token):
            result = auth.register_device("device-a", "postgresql://db/mark")

        self.assertEqual(
            {
                "user_id": "11111111-1111-4111-8111-111111111111",
                "token": token,
            },
            result,
        )
        self.assertEqual(1, self.conn.commits)
        self.assertEqual(0, self.conn.rollbacks)
        self.assertEqual([self.conn], self.released)
        self.assertEqual(["postgresql://db/mark"], self.get_conn_calls)
        insert_sql, insert_params = self.conn.executed[0]
        self.assertIn("INSERT INTO users", insert_sql)
        self.assertIn("ON CONFLICT (device_id) DO NOTHING", insert_sql)
        self.assertIn("%s", insert_sql)
        self.assertEqual("device-a", insert_params[1])
        self.assertEqual(token, insert_params[2])
        self.assertTrue(self.conn.cursor_instance.closed)

    def test_generated_identity_values_are_uuid4_and_secure_token_is_used(self):
        self.conn.inserted_row = ("user-from-database", "token-from-database")
        with patch.object(auth.secrets, "token_urlsafe", return_value="secure-token"):
            auth.register_device("device-a")

        generated_user_id = self.conn.executed[0][1][0]
        generated_token = self.conn.executed[0][1][2]
        parsed = auth.uuid.UUID(generated_user_id)
        self.assertEqual(4, parsed.version)
        self.assertEqual("secure-token", generated_token)
    def test_generated_device_id_is_used_when_omitted(self):
        generated_device = "device-generated"
        self.conn.inserted_row = ("user-1", "token-1")
        with patch.object(auth.uuid, "uuid4", return_value=generated_device):
            result = auth.register_device()

        self.assertEqual("user-1", result["user_id"])
        self.assertEqual("device-generated", self.conn.executed[0][1][1])

    def test_known_device_never_recovers_or_rotates_existing_token(self):
        self.conn.existing_row = ("user-existing", "token-existing")
        self.conn.inserted_row = None  # INSERT ... DO NOTHING RETURNING
        with self.assertRaises(auth.DeviceAlreadyRegistered) as caught:
            auth.register_device("device-a")
        self.assertNotIn("token-existing", str(caught.exception))
        self.assertEqual(0, self.conn.commits)
        self.assertEqual(1, self.conn.rollbacks)
        self.assertEqual(1, len(self.conn.executed))
        self.assertNotIn("SELECT", self.conn.executed[0][0])
        self.assertIn("DO NOTHING", self.conn.executed[0][0])
        self.assertEqual([self.conn], self.released)

    def test_empty_device_id_is_rejected_before_database_access(self):
        with self.assertRaises(auth.InvalidDeviceId):
            auth.register_device("   ")
        self.assertEqual([], self.get_conn_calls)
        self.assertEqual([], self.released)

    def test_overlong_device_id_is_rejected(self):
        with self.assertRaises(auth.InvalidDeviceId):
            auth.register_device("x" * (auth.MAX_DEVICE_ID_LENGTH + 1))
        self.assertEqual([], self.get_conn_calls)

    def test_non_string_device_id_is_rejected(self):
        with self.assertRaises(auth.InvalidDeviceId):
            auth.register_device(123)
        self.assertEqual([], self.get_conn_calls)

    def test_whoami_returns_user_id_for_valid_token(self):
        self.conn.token_row = ("user-1",)
        result = auth.whoami("token-1")

        self.assertEqual("user-1", result)
        self.assertEqual("SELECT id FROM users WHERE token = %s", " ".join(self.conn.executed[0][0].split()))
        self.assertEqual(("token-1",), self.conn.executed[0][1])
        self.assertEqual([self.conn], self.released)

    def test_whoami_rejects_unknown_token(self):
        self.conn.token_row = None
        with self.assertRaises(auth.InvalidToken):
            auth.whoami("unknown")
        self.assertEqual([self.conn], self.released)

    def test_invalid_token_is_rejected_before_database_access(self):
        with self.assertRaises(auth.InvalidToken):
            auth.whoami("   ")
        self.assertEqual([], self.get_conn_calls)

    def test_database_error_is_not_masked_and_connection_is_released(self):
        error = db.DatabaseOperationError("database unavailable")
        self.conn.raise_on_execute = error
        with self.assertRaisesRegex(db.DatabaseOperationError, "database unavailable"):
            auth.register_device("device-a")
        self.assertEqual([self.conn], self.released)
        self.assertNotEqual(0, len(self.conn.executed))

    def test_unexpected_storage_error_rolls_back_and_hides_details(self):
        self.conn.raise_on_execute = RuntimeError("private driver detail")
        with self.assertRaises(auth.AuthStorageError) as context:
            auth.register_device("device-a")
        self.assertEqual(1, self.conn.rollbacks)
        self.assertEqual([self.conn], self.released)
        self.assertNotIn("device-a", str(context.exception))

    def test_whoami_storage_error_releases_connection(self):
        self.conn.raise_on_execute = RuntimeError("driver detail")
        with self.assertRaises(auth.AuthStorageError) as context:
            auth.whoami("secret-token")
        self.assertEqual([self.conn], self.released)
        self.assertNotIn("secret-token", str(context.exception))
        self.assertNotIn("driver detail", str(context.exception))


if __name__ == "__main__":
    unittest.main()

