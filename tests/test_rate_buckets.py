"""Keep telemetry/conversation traffic from consuming the save/import budget."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import coach_server as server


class RateBucketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = server.App(store=SimpleNamespace(path=Path(temp.name) / "coach.sqlite3"))

    def limit(self, path, count=1):
        for _ in range(count):
            self.app._ride_rate_limit("POST", path)

    def assert_limited(self, path):
        with self.assertRaises(server.ActionError) as caught:
            self.limit(path)
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "rate_limited"))
        self.assertGreater(int(caught.exception.retry_after), 0)

    def test_conversation_does_not_consume_write_budget(self):
        self.limit(server.RIDE_LIVE, 60)
        self.assert_limited(server.RIDE_LIVE)
        self.limit(server.RIDE_POST, 10)
        self.assert_limited(server.RIDE_POST)

    def test_writes_do_not_consume_conversation_budget(self):
        self.limit(server.RIDE_POST, 10)
        self.assert_limited(server.RIDE_POST)
        self.limit(server.RIDE_CHAT, 60)
        self.assert_limited(server.RIDE_CHAT)

    def test_import_and_save_share_tight_budget(self):
        self.limit(server.RIDE_POST, 5)
        self.limit(server.RIDE_IMPORT, 5)
        self.assert_limited(server.RIDE_POST)
        self.assert_limited(server.RIDE_IMPORT)

    def test_expiration_applies_to_each_bucket(self):
        with patch.object(server.time, "monotonic", return_value=100):
            self.limit(server.RIDE_POST, 10)
            self.limit(server.RIDE_LIVE, 60)
        with patch.object(server.time, "monotonic", return_value=159):
            self.assert_limited(server.RIDE_POST)
            self.assert_limited(server.RIDE_LIVE)
        with patch.object(server.time, "monotonic", return_value=160):
            self.limit(server.RIDE_POST)
            self.limit(server.RIDE_LIVE)

    def test_cloud_sync_and_create_share_save_budget_not_conversation(self):
        body = json.dumps({"token": "fixture"}).encode()
        headers = {"Host": "127.0.0.1:8794", "Origin": "http://127.0.0.1:8794",
                   "Content-Type": "application/json", "Content-Length": str(len(body)),
                   "X-Coach-Token": self.app.token}
        self.limit(server.RIDE_LIVE, 20)
        with patch.object(self.app, "_cloud_ride_request", return_value={"saved": 1}) as cloud:
            for path in ("/api/rides/sync", "/api/rides/create") * 5:
                self.assertEqual(self.app.dispatch("POST", path, headers, body), {"saved": 1})
            with self.assertRaises(server.ActionError) as caught:
                self.app.dispatch("POST", "/api/rides/sync", headers, body)
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(cloud.call_count, 10)
