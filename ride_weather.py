"""AMap (Gaode) weather lookup for the pre-ride brief; the key never leaves the server.

中文摘要：坐标 → 逆地理编码 → 当日天气，归一化转发，只回城市与当日昼夜天气/气温。
key 由调用方显式提供，永不写入返回、日志或持久化；上游形状全部防御性解析，
失败只给状态码；坐标来自用户已授权的定位，由本服务转发给高德（与导航同一声明）。
Transport contract: http_get(request, *, timeout) -> (status, headers, bytes).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ride_nav import _fetch_json, _status_for

REGEO_ENDPOINT = "https://restapi.amap.com/v3/geocode/regeo"
WEATHER_ENDPOINT = "https://restapi.amap.com/v3/weather/weatherInfo"
_MAX_TEXT = 60


def _result(status: str, **fields) -> dict:
    return {"weather_status": status, **fields}


def _clean_text(value: Any) -> str | None:
    if type(value) is not str:
        return None
    cleaned = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    return cleaned if 1 <= len(cleaned) <= _MAX_TEXT else None


def _temperature(value: Any) -> float | None:
    if type(value) is bool or type(value) not in (int, float, str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (-100.0 <= number <= 100.0) or number != number:
        return None
    return int(number) if number == int(number) else number


def _envelope_error(envelope: Any) -> str | None:
    """AMap status field: '1' ok, anything else maps to a sanitized status."""
    if type(envelope) is not dict:
        return "upstream_error"
    if envelope.get("status") == "1":
        return None
    return _status_for(envelope.get("infocode"))


def fetch_weather(lat: float, lng: float, *, api_key: Any, http_get) -> dict:
    """One reverse-geocode + one forecast call; never retries, never guesses."""
    if type(lat) is bool or type(lng) is bool \
            or type(lat) not in (int, float) or type(lng) not in (int, float) \
            or not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) \
            or lat != lat or lng != lng:
        return _result("invalid_input")
    if type(api_key) is not str or not api_key.strip():
        return _result("not_configured")

    regeo = _fetch_json(
        REGEO_ENDPOINT + "?" + urlencode({"location": f"{lng:.7f},{lat:.7f}", "key": api_key}),
        http_get)
    if regeo is None or regeo[0] != 200:
        return _result("upstream_error")
    error = _envelope_error(regeo[1])
    if error is not None:
        return _result(error)
    component = regeo[1].get("regeocode")
    component = component.get("addressComponent") if type(component) is dict else None
    if type(component) is not dict:
        return _result("upstream_error")
    adcode = _clean_text(component.get("adcode"))
    # AMap quirk: empty fields come back as [] rather than "".
    city = _clean_text(component.get("city")) or _clean_text(component.get("province"))
    if adcode is None or city is None:
        return _result("upstream_error")

    weather = _fetch_json(
        WEATHER_ENDPOINT + "?" + urlencode({"city": adcode, "extensions": "all", "key": api_key}),
        http_get)
    if weather is None or weather[0] != 200:
        return _result("upstream_error")
    error = _envelope_error(weather[1])
    if error is not None:
        return _result(error)
    forecasts = weather[1].get("forecasts")
    casts = None
    if type(forecasts) is list and forecasts and type(forecasts[0]) is dict:
        casts = forecasts[0].get("casts")
    today = casts[0] if type(casts) is list and casts and type(casts[0]) is dict else None
    if today is None:
        return _result("upstream_error")
    day_weather = _clean_text(today.get("dayweather"))
    night_weather = _clean_text(today.get("nightweather"))
    day_temp = _temperature(today.get("daytemp"))
    night_temp = _temperature(today.get("nighttemp"))
    if day_weather is None or night_weather is None or day_temp is None or night_temp is None:
        return _result("upstream_error")
    return _result("ok", city=city, day_weather=day_weather, day_temp=day_temp,
                   night_weather=night_weather, night_temp=night_temp)
