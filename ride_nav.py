"""AMap (Gaode) geocoding + bicycling/walking directions; the key never leaves the server.

中文摘要：只做地址解析与骑行/步行路径规划的归一化转发；key 由调用方显式提供，
永不写入任何返回、日志或持久化；上游形状全部防御性解析，失败只给状态码。
Transport contract: http_get(request, *, timeout) -> (status, headers, bytes).
No environment key lookup: the caller supplies api_key explicitly. Never retry.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

GEOCODE_ENDPOINT = "https://restapi.amap.com/v3/geocode/geo"
DIRECTION_ENDPOINT = "https://restapi.amap.com/v4/direction/bicycling"
WALKING_ENDPOINT = "https://restapi.amap.com/v3/direction/walking"
TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 256 * 1024
MAX_STEPS = 500
MAX_STEP_POINTS = 500
MAX_TOTAL_POINTS = 5000
MAX_TEXT_CHARS = 300
_ORIGIN = re.compile(r"^(-?[0-9]{1,3}(?:\.[0-9]{1,7})?),(-?[0-9]{1,2}(?:\.[0-9]{1,7})?)$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Provider numeric codes only; never the provider's free-text messages.
_KEY_REJECTED = {"10001", "10009", "10032", "10038"}
_QUOTA = {"10003", "10004", "10021", "10044"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a keyed URL, including to the same host.


def _http_get(request: Request, *, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response too large")
            return response.status, dict(response.headers.items()), body
    except HTTPError as error:
        try:
            return error.code, {}, b""  # Do not read or disclose provider error text.
        finally:
            error.close()


def _json(text: str | bytes) -> Any:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def constant(_):
        raise ValueError("nonfinite JSON number")

    data = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return data


def _result(status: str, **fields) -> dict:
    return {"route_status": status, **fields}


def _status_for(code: Any) -> str:
    text = str(code)
    if text in _KEY_REJECTED:
        return "key_rejected"
    if text in _QUOTA:
        return "quota_exceeded"
    return "upstream_error"


def _text(value: Any, *, required: bool) -> str | None:
    if value is None and not required:
        return ""
    if type(value) is not str:
        return None
    cleaned = _CONTROL.sub("", value).strip()
    if not 1 <= len(cleaned) <= MAX_TEXT_CHARS and (required or cleaned):
        return None
    if required and not cleaned:
        return None
    return cleaned


def _coordinate(pair: Any) -> tuple[float, float] | None:
    if type(pair) is not str:
        return None
    match = _ORIGIN.match(pair.strip())
    if not match:
        return None
    try:
        lng, lat = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return lng, lat


def _meters(value: Any) -> int | None:
    if type(value) is bool or type(value) not in (int, float):
        return None
    if not (value == value and abs(value) != float("inf")) or not 0 <= value <= 100_000_000:
        return None
    return int(round(value))


def _fetch_json(url: str, http_get) -> tuple[int, Any] | None:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        response = http_get(request, timeout=TIMEOUT_S)
    except HTTPError as error:
        error.close()
        return None
    except (TimeoutError, OSError, URLError, ValueError):
        return None
    if (type(response) is not tuple or len(response) != 3 or type(response[0]) is not int
            or not isinstance(response[1], Mapping) or type(response[2]) is not bytes
            or len(response[2]) > MAX_RESPONSE_BYTES):
        return None
    try:
        envelope = _json(response[2])
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return None
    return response[0], envelope


def _geocode(address: str, api_key: str, http_get) -> tuple[str, tuple[float, float] | None, str]:
    """Resolve an address to GCJ-02 coordinates; returns (status, point, label)."""
    url = GEOCODE_ENDPOINT + "?" + urlencode({"key": api_key, "address": address})
    fetched = _fetch_json(url, http_get)
    if fetched is None:
        return "upstream_error", None, ""
    status, envelope = fetched
    if status != 200 or type(envelope) is not dict:
        return "upstream_error", None, ""
    if envelope.get("status") != "1":
        return _status_for(envelope.get("infocode")), None, ""
    geocodes = envelope.get("geocodes")
    if type(geocodes) is not list:
        return "upstream_error", None, ""
    if not geocodes:
        return "address_not_found", None, ""
    first = geocodes[0]
    if type(first) is not dict:
        return "upstream_error", None, ""
    point = _coordinate(first.get("location"))
    label = _text(first.get("formatted_address"), required=False)
    if point is None or label is None:
        return "upstream_error", None, ""
    return "ok", point, label


def _walking_meters(value: Any) -> int | None:
    # v3 returns decimal strings; retain the same finite/range checks as v4.
    if type(value) is str:
        if not re.fullmatch(r"[0-9]{1,9}", value):
            return None
        value = int(value)
    return _meters(value)


def _direction(origin: str, destination: str, api_key: str, http_get, *, mode: str) -> dict:
    walking = mode == "walking"
    endpoint = WALKING_ENDPOINT if walking else DIRECTION_ENDPOINT
    params = {"key": api_key, "origin": origin, "destination": destination}
    if walking:
        params["output"] = "json"
    url = endpoint + "?" + urlencode(params)
    fetched = _fetch_json(url, http_get)
    if fetched is None:
        return _result("upstream_error")
    status, envelope = fetched
    if status != 200 or type(envelope) is not dict:
        return _result("upstream_error")
    if walking:
        if envelope.get("status") != "1":
            return _result(_status_for(envelope.get("infocode")))
    elif envelope.get("errcode") != 0:
        return _result(_status_for(envelope.get("errcode")))
    data = envelope.get("route" if walking else "data")
    paths = data.get("paths") if type(data) is dict else None
    if type(paths) is not list:
        return _result("upstream_error")
    if not paths:
        return _result("no_route")
    path = paths[0]
    if type(path) is not dict:
        return _result("upstream_error")
    number = _walking_meters if walking else _meters
    distance_m = number(path.get("distance"))
    duration_s = number(path.get("duration"))
    raw_steps = path.get("steps")
    if distance_m is None or duration_s is None or type(raw_steps) is not list or not raw_steps:
        return _result("upstream_error")
    if len(raw_steps) > MAX_STEPS:
        return _result("route_too_complex")
    steps = []
    total_points = 0
    for raw in raw_steps:
        if type(raw) is not dict:
            return _result("upstream_error")
        instruction = _text(raw.get("instruction"), required=True)
        road = _text(raw.get("road"), required=False)
        step_m = number(raw.get("distance"))
        polyline = raw.get("polyline")
        if instruction is None or road is None or step_m is None or type(polyline) is not str:
            return _result("upstream_error")
        if len(polyline) > MAX_RESPONSE_BYTES // 4:
            return _result("upstream_error")
        pairs = polyline.strip().split(";") if polyline.strip() else []
        if not 1 <= len(pairs) <= MAX_STEP_POINTS:
            return _result("upstream_error")
        total_points += len(pairs)
        if total_points > MAX_TOTAL_POINTS:
            return _result("route_too_complex")
        path = []
        for pair in pairs:
            point = _coordinate(pair)
            if point is None:
                return _result("upstream_error")
            path.append([point[0], point[1]])
        end = path[-1]
        steps.append({"instruction": instruction, "road": road, "distance_m": step_m,
                      "end_lng": end[0], "end_lat": end[1], "path": path})
    return _result("ok", distance_m=distance_m, duration_s=duration_s, steps=steps)


def plan_route(origin: Any, destination_address: Any, *, api_key: str | None,
               mode: str = "bicycling", http_get: Callable[..., tuple[int, Mapping[str, str], bytes]] | None = None) -> dict:
    """Plan one bicycling or walking route from a GCJ-02 origin to a free-text address.

    Args:
        origin: "lng,lat" string in GCJ-02 (the client converts browser GPS).
        destination_address: 1..100 visible characters; control characters rejected.
        api_key: Explicit printable ASCII AMap Web-service key; None/blank means
            not configured. This module never reads environment variables.
        mode: "bicycling" (default, v4) or "walking" (v3).
        http_get: Optional offline transport receiving a urllib Request and keyword
            timeout=10, returning (HTTP status, headers mapping, body bytes).
    Returns:
        route_status plus, on "ok", distance_m/duration_s/destination/steps.
        Steps carry instruction/road/distance_m/end_lng/end_lat (the step END
        coordinate) plus path: the decoded GCJ-02 polyline as [lng, lat] pairs
        for corridor (off-route) checks. No key or upstream message text is
        ever included in any result.
    Raises:
        No expected input, transport or JSON errors escape. Unexpected programming
        errors in an injected transport propagate, rather than being hidden.
    Example:
        >>> plan_route('116.397,39.908', '天安门', api_key=None)['route_status']
        'not_configured'
    """
    if type(mode) is not str or mode not in ("bicycling", "walking"):
        return _result("invalid_input")
    if type(origin) is not str or _coordinate(origin) is None:
        return _result("invalid_input")
    if (type(destination_address) is not str or _CONTROL.search(destination_address)
            or not 1 <= len(destination_address.strip()) <= 100):
        return _result("invalid_input")
    address = destination_address.strip()
    if api_key is None or (type(api_key) is str and not api_key.strip()):
        return _result("not_configured")
    if (type(api_key) is not str or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in api_key)):
        return _result("upstream_error")
    transport = _http_get if http_get is None else http_get
    status, point, label = _geocode(address, api_key, transport)
    if status != "ok":
        return _result(status)
    destination = f"{point[0]:.6f},{point[1]:.6f}"
    route = _direction(origin.strip(), destination, api_key, transport, mode=mode)
    if route["route_status"] == "ok":
        route["destination"] = label
    return route
