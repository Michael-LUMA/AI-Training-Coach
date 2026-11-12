"""Actual loopback HTTP/private-gate smoke test, with synthetic DB and identity."""
import base64
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

import coach_server as server
from tests import test_cloud_rides as fixtures


class CloudHTTPTests(unittest.TestCase):
    def test_private_login_registration_sync_replay_read_and_raw_rejection_over_http(self):
        password = "synthetic-private-http-smoke-password-1234"
        basic = "Basic " + base64.b64encode(("boss:" + password).encode()).decode()
        conn = fixtures.FakeConnection()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"LUMA_COACH_TEST_PASSWORD": password}, clear=True), \
                patch.object(server, "_bind_config", return_value=("127.0.0.1", 0)), \
                patch.object(server.auth, "register_device", return_value={"user_id": "user-a", "token": "synthetic-token"}), \
                patch.object(server.auth, "whoami", return_value="user-a"), \
                patch.object(server.cloud_db, "get_conn", return_value=conn), \
                patch.object(server.cloud_db, "release_conn"), \
                patch.object(server.cloud_rides, "_refresh_profile"):
            app = server.App(server.CoachStore(Path(directory) / "coach.sqlite3"))
            httpd = server.create_server(app=app)
            worker = Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                def request(path, payload, login=True):
                    raw = json.dumps(payload).encode()
                    port = httpd.server_port
                    headers = {"Origin": "http://127.0.0.1:" + str(port), "Content-Type": "application/json"}
                    if login:
                        headers["Authorization"] = basic
                    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as client:
                        client.request("POST", path, raw, headers)
                        response = client.getresponse()
                        return response.status, dict(response.getheaders()), json.loads(response.read())

                status, headers, result = request(server.AUTH_REGISTER, {}, login=False)
                self.assertEqual((status, result["error_code"]), (401, "test_access_required"))
                self.assertIn("WWW-Authenticate", headers)
                self.assertNotIn("synthetic-token", json.dumps(result))
                status, headers, result = request(server.AUTH_REGISTER, {"device_id": "synthetic-device"})
                self.assertEqual((status, result["user_id"]), (200, "user-a"))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(request(server.AUTH_WHOAMI, {"token": result["token"]})[2], {"user_id": "user-a"})
                payload = {"token": result["token"], "rides": [fixtures.summary()]}
                self.assertEqual(request("/api/rides/sync", payload)[2]["saved"], 1)
                self.assertEqual(request("/api/rides/sync", payload)[2]["duplicates"], 1)
                self.assertEqual(request("/api/rides/list", {"token": result["token"]})[2]["rides"],
                                 [fixtures.summary()])
                payload["rides"][0]["series"] = []
                self.assertEqual(request("/api/rides/sync", payload)[0], 400)
            finally:
                httpd.shutdown()
                httpd.server_close()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
