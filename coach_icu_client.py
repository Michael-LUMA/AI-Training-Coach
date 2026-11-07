"""Bounded Intervals.icu GET client with an injectable offline transport.

中文摘要：仅负责固定 HTTPS API 的鉴权与错误映射；不存储密钥、不记录日志、不自动重试。
Transport contract: fetch(request, *, timeout) -> (status, headers, body_bytes).
Injected transports must respect timeout and must never log Authorization.
"""
from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from action_cards import ActionError

BASE_URL = "https://intervals.icu/api/v1"
# Intervals.icu sits behind Cloudflare, which rejects the default
# Python-urllib User-Agent with a bare 403. Identify the client honestly.
USER_AGENT = "luma-ai-ride-coach/0.1 (+https://intervals.icu)"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
STREAM_TYPES = ("time", "watts", "heartrate", "cadence", "distance", "altitude",
                "latlng", "velocity_smooth")
ACTIVITY_FIELDS = ("id,name,start_date_local,type,distance,moving_time,icu_training_load,"
                   "icu_atl,icu_ctl,icu_weighted_avg_watts,average_heartrate,trainer")
Fetch = Callable[..., tuple[int, Mapping[str, str], bytes]]


class ICUClientError(ActionError):
    """Sanitized error with project-style code/status and optional Retry-After."""

    def __init__(self, code: str, message: str, status: int = 502, *,
                 retryable: bool = False, retry_after: str | None = None):
        super().__init__(code, message, status)
        self.retryable = retryable
        self.retry_after = retry_after


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward API credentials to a redirect target (even same-origin).
        return None


def _fetch(request: Request, *, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    # Disable environment proxies; TLS certificate validation remains enabled.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ICUClientError("response_too_large", "Upstream response exceeds size limit")
            return response.status, dict(response.headers.items()), body
    except HTTPError as error:
        try:
            return error.code, dict(error.headers.items()) if error.headers is not None else {}, b""  # Never echo/read upstream error text.
        finally:
            error.close()


def _date(value: Any) -> str:
    if type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
            return value
        except ValueError:
            pass
    raise ICUClientError("invalid_input", "Date must be a valid YYYY-MM-DD", 400)


def _activity_id(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ICUClientError("invalid_input", "Invalid activity identifier", 400)
    return value


def _retry_after(headers: Mapping[str, str]) -> str | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if type(value) is not str or len(value) > 128:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", value):
        return str(int(value))
    try:
        instant = parsedate_to_datetime(value)
        if instant.tzinfo is not None:
            return format_datetime(instant.astimezone(timezone.utc), usegmt=True)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _json(body: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite number")

    try:
        result = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
        return result
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ICUClientError("invalid_json", "Upstream response is not strict UTF-8 JSON") from None


class IntervalsClient:
    """Read-only client; key stays in memory as an Authorization value only.

    Args:
        api_key: Nonempty printable ASCII API key, at most 4096 characters.
        fetch: Optional fake transport; contract documented at module level.
        timeout: Socket timeout in seconds, finite and positive, at most 120.
    Raises:
        ICUClientError: Invalid configuration (invalid_input).
    Example:
        >>> client = IntervalsClient('synthetic-key', fetch=lambda req, **kw: (200, {}, b'[]'))
        >>> client.list_activities('2026-11-01', '2026-11-06')
        []
    """

    def __init__(self, api_key: str, fetch: Fetch | None = None, *, timeout: float = 15):
        if (type(api_key) is not str or not 0 < len(api_key) <= 4096 or
                any(not 33 <= ord(c) <= 126 for c in api_key)):
            raise ICUClientError("invalid_input", "Invalid API key format", 400)
        if (type(timeout) not in (int, float) or not 0 < timeout <= 120 or
                not math.isfinite(timeout) or (fetch is not None and not callable(fetch))):
            raise ICUClientError("invalid_input", "Invalid transport or timeout", 400)
        self._authorization = "Basic " + base64.b64encode(("API_KEY:" + api_key).encode("ascii")).decode("ascii")
        self._fetch = _fetch if fetch is None else fetch
        self._timeout = timeout

    def _get(self, path, expected, query=None):
        url = BASE_URL + path + ("?" + urlencode(query) if query else "")
        request = Request(url, headers={"Authorization": self._authorization,
                                       "Accept": "application/json",
                                       "User-Agent": USER_AGENT}, method="GET")
        try:
            response = self._fetch(request, timeout=self._timeout)
        except HTTPError as error:
            try:
                response = (error.code, dict(error.headers.items()) if error.headers is not None else {}, b"")
            finally:
                error.close()
        except (TimeoutError, OSError, URLError):
            raise ICUClientError("network_error", "Upstream connection unavailable", 503, retryable=True) from None
        if (type(response) is not tuple or len(response) != 3 or
                type(response[0]) is not int or not isinstance(response[1], Mapping) or
                not all(type(k) is str and type(v) is str for k, v in response[1].items()) or
                type(response[2]) is not bytes):
            raise ICUClientError("invalid_response", "Invalid upstream transport response")
        status, headers, body = response
        if status != 200:
            code, message, local_status = {
                401: ("auth_invalid", "API credentials were rejected", 401),
                404: ("not_found", "Upstream resource not found", 404),
                429: ("rate_limited", "Upstream rate limit reached", 429),
            }.get(status, ("upstream_error", "Upstream request failed", 502))
            raise ICUClientError(code, message, local_status,
                                 retryable=status == 429 or 500 <= status <= 599,
                                 retry_after=_retry_after(headers) if status == 429 else None)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ICUClientError("response_too_large", "Upstream response exceeds size limit")
        result = _json(body)
        if type(result) is not expected or (expected is list and any(type(v) is not dict for v in result)):
            raise ICUClientError("invalid_response", "Unexpected upstream JSON shape")
        return result

    def list_activities(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        """List athlete 0 activity objects within inclusive ISO date bounds.

        Raises ICUClientError for invalid bounds, network/HTTP/JSON failures.
        """
        oldest, newest = _date(oldest), _date(newest)
        if oldest > newest:
            raise ICUClientError("invalid_input", "Oldest date must not exceed newest date", 400)
        return self._get("/athlete/0/activities", list,
                         {"oldest": oldest, "newest": newest, "fields": ACTIVITY_FIELDS})

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        """Return a detail object; invalid IDs and request failures raise ICUClientError."""
        return self._get("/activity/" + _activity_id(activity_id), dict)

    def get_streams(self, activity_id: str, types: Sequence[str] = STREAM_TYPES) -> list[dict[str, Any]]:
        """Return stream objects; types must be nonempty unique supported names.

        Empty returned arrays are valid. ICUClientError reports invalid input or requests.
        """
        activity_id = _activity_id(activity_id)
        if (type(types) not in (list, tuple) or not types or
                any(type(t) is not str or t not in STREAM_TYPES for t in types) or
                len(set(types)) != len(types)):
            raise ICUClientError("invalid_input", "Invalid stream types", 400)
        return self._get("/activity/" + activity_id + "/streams", list, {"types": ",".join(types)})

    def get_sport_settings(self) -> dict[str, Any]:
        """Return athlete 0 Ride settings; request failures raise ICUClientError."""
        return self._get("/athlete/0/sport-settings/Ride", dict)

    def get_wellness(self, day: str) -> dict[str, Any]:
        """Return wellness for an ISO date; invalid dates/requests raise ICUClientError."""
        return self._get("/athlete/0/wellness/" + _date(day), dict)
