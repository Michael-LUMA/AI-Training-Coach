"""Synthetic fixtures only; injected transports, no real network access."""
import base64
import io
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_icu_client as client

KEY = "synthetic-private-key"
REAL_FETCH = client._fetch


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        # Any accidental use of the real default transport fails, never opens a socket.
        self.guard = patch.object(client, "_fetch", side_effect=AssertionError("Real transport forbidden"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def make(self, status=200, headers=None, body=b'{}'):
        def fetch(request, *, timeout):
            self.calls.append((request, timeout))
            return status, headers or {}, body
        return client.IntervalsClient(KEY, fetch=fetch)

    def test_list_url_auth_and_timeout(self):
        self.assertEqual(self.make(body=b'[]').list_activities("2026-11-01", "2026-11-06"), [])
        req, timeout = self.calls[0]
        parts = urlsplit(req.full_url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "intervals.icu")
        self.assertEqual(parts.path, "/api/v1/athlete/0/activities")
        query = parse_qs(parts.query)
        self.assertEqual(query["oldest"], ["2026-11-01"])
        self.assertEqual(query["newest"], ["2026-11-06"])
        self.assertIn("start_date_local", query["fields"][0])
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(timeout, 15)
        auth = req.get_header("Authorization")
        self.assertEqual(base64.b64decode(auth.split()[1]), ("API_KEY:" + KEY).encode())
        self.assertNotIn(KEY, req.full_url)
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("Accept"), "application/json")
        # Cloudflare rejects the default Python-urllib agent with a bare 403.
        self.assertTrue(req.get_header("User-agent", "").startswith("luma-ai-ride-coach/"))

    def test_all_endpoint_paths(self):
        api = self.make()
        api.get_activity("i123")
        api.get_sport_settings()
        api.get_wellness("2026-11-06")
        self.assertEqual([urlsplit(req.full_url).path for req, _ in self.calls],
            ["/api/v1/activity/i123", "/api/v1/athlete/0/sport-settings/Ride", "/api/v1/athlete/0/wellness/2026-11-06"])

    def test_stream_query_and_synthetic_payload(self):
        fixture = b'[{"type":"watts","data":[100,null,"200"]}]'
        self.assertEqual(self.make(body=fixture).get_streams("i123", ["time", "watts"])[0]["data"], [100, None, "200"])
        req = self.calls[0][0]
        self.assertEqual(parse_qs(urlsplit(req.full_url).query), {"types": ["time,watts"]})
        self.assertEqual(urlsplit(req.full_url).path, "/api/v1/activity/i123/streams")

    def test_http_error_mapping_and_secret_redaction(self):
        for status, code, local in ((401, "auth_invalid", 401), (404, "not_found", 404),
                                    (429, "rate_limited", 429), (500, "upstream_error", 502),
                                    (302, "upstream_error", 502), (403, "upstream_error", 502)):
            with self.subTest(status=status), self.assertRaises(client.ICUClientError) as caught:
                self.make(status=status, body=KEY.encode()).get_activity("i123")
            error = caught.exception
            self.assertEqual((error.code, error.status), (code, local))
            self.assertNotIn(KEY, str(error) + repr(error))
            self.assertEqual(error.retryable, status in (429, 500))

    def test_retry_after_seconds_date_and_invalid(self):
        for raw, expected in (("00120", "120"), ("Fri, 06 Nov 2026 12:00:00 GMT", "Fri, 06 Nov 2026 12:00:00 GMT"),
                              ("-1", None), (KEY, None), ("12\r\nx-secret: yes", None)):
            with self.assertRaises(client.ICUClientError) as caught:
                self.make(429, {"rEtRy-AfTeR": raw}).get_activity("i123")
            self.assertEqual(caught.exception.retry_after, expected)

    def test_timeout_and_network_error_are_safe_no_retry(self):
        for failure in (TimeoutError(KEY), socket.timeout(KEY), URLError(KEY), OSError(KEY)):
            calls = []
            def fetch(req, *, timeout):
                calls.append(req)
                raise failure
            with self.assertRaises(client.ICUClientError) as caught:
                client.IntervalsClient(KEY, fetch=fetch).get_activity("i123")
            self.assertEqual(caught.exception.code, "network_error")
            self.assertEqual(caught.exception.status, 503)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_strict_json_failure(self):
        for raw in (b'not json', b'\xff', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}'):
            with self.assertRaises(client.ICUClientError) as caught:
                self.make(body=raw).get_activity("i123")
            self.assertEqual(caught.exception.code, "invalid_json")

    def test_unexpected_shapes_and_response_size(self):
        for raw in (b'[]', b'null', b'42'):
            with self.assertRaises(client.ICUClientError) as caught:
                self.make(body=raw).get_activity("i123")
            self.assertEqual(caught.exception.code, "invalid_response")
        with self.assertRaises(client.ICUClientError):
            self.make(body=b'[1]').get_streams("i123")
        with patch.object(client, "MAX_RESPONSE_BYTES", 5):
            with self.assertRaises(client.ICUClientError) as caught:
                self.make(body=b'{"x":1}').get_activity("i123")
            self.assertEqual(caught.exception.code, "response_too_large")

    def test_invalid_input_fails_before_transport(self):
        api = self.make()
        for identifier in ("../secret", "https://evil.example", "i1?key=abc", "i1#x", "", None, "x" * 129):
            with self.assertRaises(client.ICUClientError):
                api.get_activity(identifier)
        for day in ("2026-02-30", "20261106", "2026-11-06/../../", None):
            with self.assertRaises(client.ICUClientError):
                api.get_wellness(day)
        with self.assertRaises(client.ICUClientError):
            api.list_activities("2026-11-07", "2026-11-06")
        for types in ([], "time,watts", ["time", "time"], ["evil"], [None]):
            with self.assertRaises(client.ICUClientError):
                api.get_streams("i123", types)
        self.assertEqual(self.calls, [])

    def test_invalid_config_and_repr(self):
        for key in (None, "", "with space", "x\r\ny", "密钥", "x" * 4097):
            with self.assertRaises(client.ICUClientError):
                client.IntervalsClient(key, fetch=lambda *a, **kw: None)
        for timeout in (True, -1, 0, float("inf"), float("nan"), 121, 10 ** 1000):
            with self.assertRaises(client.ICUClientError):
                client.IntervalsClient(KEY, timeout=timeout)
        self.assertNotIn(KEY, repr(self.make()))

    def test_malformed_injected_response(self):
        for response in (None, (200, {}, "text"), ("200", {}, b'{}')):
            api = client.IntervalsClient(KEY, fetch=lambda *a, **kw: response)
            with self.assertRaises(client.ICUClientError) as caught:
                api.get_activity("i123")
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_stdlib_transport_is_bounded_and_normalizes_headers_offline(self):
        headers = Message(); headers["Content-Type"] = "application/json"
        reply = MagicMock()
        reply.status, reply.headers = 200, headers
        reply.read.return_value = b'{}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = reply
        with patch.object(client, "build_opener", return_value=opener) as build:
            api = client.IntervalsClient(KEY, fetch=REAL_FETCH)
            self.assertEqual(api.get_activity("i123"), {})
            reply.read.assert_called_once_with(client.MAX_RESPONSE_BYTES + 1)
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], client._NoRedirect)
            reply.read.return_value = b'xxxx'
            with patch.object(client, "MAX_RESPONSE_BYTES", 3):
                with self.assertRaises(client.ICUClientError) as caught:
                    api.get_activity("i123")
                self.assertEqual(caught.exception.code, "response_too_large")

    def test_urllib_http_error_maps_without_reading_body_offline(self):
        for use_default in (True, False):
            headers = Message(); headers["Retry-After"] = "60"
            body = io.BytesIO(KEY.encode())
            error = HTTPError(client.BASE_URL, 429, KEY, headers, body)
            opener = MagicMock(); opener.open.side_effect = error
            def fake_fetch(req, *, timeout):
                raise error
            with patch.object(client, "build_opener", return_value=opener):
                api = client.IntervalsClient(KEY, fetch=REAL_FETCH if use_default else fake_fetch)
                with self.assertRaises(client.ICUClientError) as caught:
                    api.get_activity("i123")
                self.assertEqual(caught.exception.code, "rate_limited")
                self.assertEqual(caught.exception.retry_after, "60")
                self.assertNotIn(KEY, str(caught.exception))
                self.assertTrue(body.closed)

    def test_default_transport_redirect_policy_offline(self):
        req = client.Request(client.BASE_URL + "/activity/i123")
        self.assertIsNone(client._NoRedirect().redirect_request(req, None, 302, "Moved", {}, "https://evil.example"))


if __name__ == "__main__":
    unittest.main()
