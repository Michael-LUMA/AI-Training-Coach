"""Offline encrypted storage tests: synthetic secrets and temporary SQLite only."""
import base64
import ctypes
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_cards import ActionError
import coach_keystore as ks
from coach_server import App, CoachStore

SECRET = "synthetic-api-secret-never-real-1234"
MASTER = "synthetic-master-key-32-bytes-minimum"


class KeyStoreTests(unittest.TestCase):
    def store(self, key=MASTER):
        return ks.KeyStore(ks.MasterKeyBackend(key))

    def test_master_roundtrip_randomized_and_no_plaintext(self):
        store = self.store()
        for value in (SECRET, "", "中文密钥" * 100):
            first, second = store.encrypt(value), store.encrypt(value)
            self.assertNotEqual(first, second)
            self.assertEqual(store.decrypt(first), value)
        self.assertNotIn(SECRET, first)
        self.assertNotIn(SECRET.encode(), base64.b64decode(store.encrypt(SECRET).split(":")[-1]))

    def test_tamper_and_wrong_master_rejected(self):
        store = self.store()
        value = store.encrypt(SECRET)
        raw = base64.b64decode(value.split(":")[-1])
        prefix = ks.PREFIX + "masterkey:"
        for index in (0, 32, 64, len(raw)-1):
            changed = bytearray(raw); changed[index] ^= 1
            with self.assertRaises(ActionError):
                store.decrypt(prefix + base64.b64encode(changed).decode())
        with self.assertRaises(ActionError):
            self.store("a-different-master-key-at-least-32-bytes").decrypt(value)

    def test_invalid_envelopes_and_plaintext_rejected(self):
        for value in (SECRET, None, ks.PREFIX + "dpapi:AAAA", ks.PREFIX + "masterkey:!!!", ks.PREFIX + "masterkey:AA=="):
            with self.assertRaises(ActionError):
                self.store().decrypt(value)

    def test_no_master_and_short_master_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ActionError):
                ks.MasterKeyBackend()
        for value in ("", "short", " " * 32):
            with self.assertRaises(ActionError):
                ks.MasterKeyBackend(value)

    def test_non_windows_auto_master_or_fail_closed(self):
        with patch.object(ks.os, "name", "posix"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ActionError):
                ks.DPAPIBackend()
            store = ks.KeyStore()
            self.assertEqual(store.status, {"encrypted_at_rest": False, "key_storage_backend": None})
            for action in (lambda: store.encrypt(SECRET), lambda: store.decrypt(SECRET)):
                with self.assertRaises(ActionError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "key_storage_unavailable")
            with patch.dict(os.environ, {"LUMA_COACH_MASTER_KEY": MASTER}):
                self.assertEqual(ks.KeyStore().status["key_storage_backend"], "masterkey")

    def test_windows_selects_dpapi_no_fallback(self):
        with patch.object(ks.os, "name", "nt"), patch.object(ks, "DPAPIBackend") as backend:
            backend.return_value.name = "dpapi"
            self.assertEqual(ks.KeyStore().status["key_storage_backend"], "dpapi")
            backend.side_effect = ActionError("key_storage_unavailable", "unavailable", 503)
            with patch.object(ks, "MasterKeyBackend") as fallback:
                self.assertFalse(ks.KeyStore().status["encrypted_at_rest"])
                fallback.assert_not_called()

    def test_dpapi_mock_flags_and_free(self):
        backend = ks.DPAPIBackend.__new__(ks.DPAPIBackend)
        backend._crypt32, backend._kernel32 = Mock(), Mock()
        buffer = ctypes.create_string_buffer(b"synthetic-protected")
        def call(source, description, entropy, reserved, prompt, flags, output):
            self.assertEqual(flags, ks.CRYPTPROTECT_UI_FORBIDDEN)
            self.assertIsNone(description)
            result = ctypes.cast(output, ctypes.POINTER(ks._Blob)).contents
            result.cbData = len(buffer.value)
            result.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            return 1
        backend._crypt32.CryptProtectData.side_effect = call
        self.assertEqual(backend.encrypt(b"test"), b"synthetic-protected")
        backend._kernel32.LocalFree.assert_called_once()
        backend._crypt32.CryptUnprotectData.return_value = 0
        with self.assertRaises(ActionError):
            backend.decrypt(b"invalid")

    @unittest.skipUnless(os.name == "nt" and os.environ.get("LUMA_TEST_DPAPI") == "1", "Real DPAPI needs Windows user profile; opt in with LUMA_TEST_DPAPI=1")
    def test_dpapi_real_local_roundtrip(self):
        backend = ks.DPAPIBackend()
        try:
            ciphertext = backend.encrypt(SECRET.encode())
        except ActionError:
            code = ctypes.get_last_error()
            if code in (5, 1312):
                self.skipTest("DPAPI blocked by current sandbox/account, WinError " + str(code))
            raise
        self.assertNotIn(SECRET.encode(), ciphertext)
        self.assertEqual(backend.decrypt(ciphertext), SECRET.encode())
        changed = bytearray(ciphertext); changed[-1] ^= 1
        with self.assertRaises(ActionError):
            backend.decrypt(bytes(changed))

    def test_coach_store_encrypted_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"coach.sqlite3"
            store = CoachStore(path, keystore=self.store())
            app = App(store)
            headers = {"Host":"127.0.0.1:8794", "Origin":"http://127.0.0.1:8794", "X-Coach-Token":app.token}
            body = json.dumps({"api_key":SECRET}).encode()
            headers.update({"Content-Type":"application/json", "Content-Length":str(len(body))})
            result = app.dispatch("POST", "/api/coach/connect", headers, body)
            self.assertTrue(result["encrypted_at_rest"])
            self.assertEqual(store.get_key(), SECRET)
            self.assertNotIn(SECRET.encode(), path.read_bytes())
            self.assertEqual(CoachStore(path, keystore=self.store()).get_key(), SECRET)
            result = app.dispatch("GET", "/api/coach/config", headers)
            self.assertTrue(result["encrypted_at_rest"])
            self.assertEqual(result["key_storage_backend"], "masterkey")
            self.assertNotIn(SECRET, json.dumps(result))

    def test_unavailable_refuses_before_db_created_and_config_truthful(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = self.store(); backend.backend = None
            path = Path(folder)/"new"/"coach.sqlite3"
            store = CoachStore(path, keystore=backend)
            with self.assertRaises(ActionError):
                store.save_key(SECRET)
            self.assertFalse(path.exists())
            app = App(store)
            result = app.dispatch("GET", "/api/coach/config", {"Host":"127.0.0.1:8794"})
            self.assertFalse(result["encrypted_at_rest"])
            self.assertIsNone(result["key_storage_backend"])

    def test_legacy_plaintext_not_returned_or_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE credential (id INTEGER PRIMARY KEY, api_key TEXT)")
                db.execute("INSERT INTO credential VALUES (1, ?)", (SECRET,))
                db.commit()
            store = CoachStore(path, keystore=self.store())
            with self.assertRaises(ActionError):
                store.get_key()


if __name__ == "__main__":
    unittest.main()
