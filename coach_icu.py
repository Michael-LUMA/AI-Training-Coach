"""Offline Intervals.icu JSON adapter; no HTTP, credentials or persistence.

中文摘要：仅归一化已取得的JSON，复用GPX指标与缺值契约，不猜测时区或GPS。
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from ride_gpx import MAX_POINTS, _number, _timestamp, parse_gpx

_STREAM_FIELDS = {"watts": "power_w", "heartrate": "heart_rate_bpm",
                  "cadence": "cadence_rpm", "altitude": "ele"}
_KNOWN_STREAMS = {"time", "latlng", "distance", "velocity_smooth", *_STREAM_FIELDS}


def _numeric(value: Any, minimum: float | None = None,
             maximum: float | None = None) -> float | None:
    # bool is not a numeric sample even though float(True) would accept it.
    return None if isinstance(value, bool) else _number(value, minimum, maximum)


def _zones(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    result = [_numeric(item, 0) for item in value]
    if any(item is None for item in result):
        return None
    if any(a >= b for a, b in zip(result, result[1:])):
        return None
    return result


def _power_zones_watts(value: Any, ftp: float | None) -> tuple[list[float] | None, str | None]:
    """Normalize Intervals.icu power zones to absolute-watt lower bounds.

    Real API returns either absolute-watt lower bounds (first element 0, e.g.
    [0, 138, 188, ...]) or percentage-of-FTP upper bounds (e.g. the default
    [55, 75, 90, 105, 120, 150, 999] — Coggan percentages with a sentinel top).
    Detection key: a 0 first element means watts; anything else is treated as
    percent-of-FTP uppers and converted ([0] + pct/100*ftp for all but the
    sentinel). Returns (watts or None, conversion note or None).
    """
    zones = _zones(value)
    if zones is None:
        return None, None
    if zones[0] == 0:
        return zones, None
    if ftp is None or ftp <= 0 or len(zones) < 2:
        return None, "power_zones_percent_without_ftp"
    converted = [0.0] + [z * ftp / 100 for z in zones[:-1]]
    if any(a >= b for a, b in zip(converted, converted[1:])):
        return None, "power_zones_conversion_nonmonotonic"
    return converted, "percent_of_ftp_upper_bounds_converted_to_watts"


def normalize_activity(activity: dict[str, Any], streams: list[dict[str, Any]],
                       sport_settings: dict[str, Any] | None = None,
                       wellness: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize activity/stream JSON into a ride record plus coach_context.

    Args:
        activity: Detail JSON. Absolute times require offset-aware start_date
            (or offset-aware start_date_local); naive local times are not UTC.
        streams: Arrays aligned by index, never interpolated or truncated to
            the shortest stream. Missing/invalid elements remain nullable.
        sport_settings: Optional ftp/power_zones/lthr/hr_zones JSON.
        wellness: Optional resting_hr/sleep_secs JSON.
    Returns:
        A luma-ride-gpx/1 record with source.kind='intervals_icu' and an added
        coach_context object. Provenance hashes canonical supplied JSON, NOT
        original HTTP bytes. Absent context values are None, not estimated.
    Raises:
        TypeError: Wrong container types or a known stream with non-list data.
        ValueError: Duplicate streams, more than MAX_POINTS samples, or input
            that cannot be serialized as JSON. GPXError (a ValueError) may be
            raised if generated GPX exceeds the existing parser byte cap.
    Example:
        >>> normalize_activity({'type': 'Ride'}, [])['point_count']
        0

    Distance/velocity summaries are not substituted for GPX route geometry or
    missing samples. Without an absolute UTC origin, point time and timed
    metrics remain unavailable, even if relative seconds were supplied.
    """
    if not isinstance(activity, dict) or not isinstance(streams, list):
        raise TypeError("activity must be a dict and streams a list")
    for value in (sport_settings, wellness):
        if value is not None and not isinstance(value, dict):
            raise TypeError("optional settings and wellness must be dicts")
    settings = sport_settings if sport_settings is not None else {}
    health = wellness if wellness is not None else {}
    data = {}
    for stream in streams:
        if not isinstance(stream, dict) or not isinstance(stream.get("type"), str):
            raise TypeError("each stream needs a string type")
        name = stream["type"]
        if name not in _KNOWN_STREAMS:
            continue
        if name in data:
            raise ValueError("duplicate stream type")
        if not isinstance(stream.get("data"), list):
            raise TypeError("known stream data must be a list")
        data[name] = stream["data"]
    count = max((len(values) for values in data.values()), default=0)
    if count > MAX_POINTS:
        raise ValueError("stream point limit exceeded")
    try:
        payload = json.dumps([activity, streams, sport_settings, wellness],
                             sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=True).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("inputs must be JSON-compatible") from None

    def sample(name, index):
        values = data.get(name, [])
        return values[index] if index < len(values) else None

    origin = None
    for key in ("start_date", "start_date_local"):
        if isinstance(activity.get(key), str):
            origin = _timestamp(activity[key])
            if origin is not None:
                break
    root = Element("gpx")
    segment = SubElement(SubElement(root, "trk"), "trkseg")
    for index in range(max(1, count)):
        point = SubElement(segment, "trkpt")
        position = sample("latlng", index)
        if isinstance(position, (list, tuple)) and len(position) == 2:
            for key, value, low, high in (("lat", position[0], -90, 90),
                                          ("lon", position[1], -180, 180)):
                number = _numeric(value, low, high)
                point.set(key, str(number) if number is not None else "invalid")
        offset = _numeric(sample("time", index), 0)
        time = None
        if origin is not None and offset is not None:
            try:
                time = origin + timedelta(seconds=offset)
            except (OverflowError, ValueError):
                pass
        if time is not None:
            SubElement(point, "time").text = time.isoformat()
        extension = SubElement(point, "extensions")
        for name, tag in (("watts", "power"), ("heartrate", "hr"),
                          ("cadence", "cad"), ("altitude", "ele")):
            if name not in data:
                continue
            value = sample(name, index)
            number = _numeric(value, None if name == "altitude" else 0)
            node = SubElement(point if name == "altitude" else extension, tag)
            node.text = str(number) if number is not None else ("" if value is None else "invalid")
    record = parse_gpx(tostring(root, encoding="utf-8"))
    record["source"] = {"kind": "intervals_icu",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "hash_scope": "canonical_python_json_inputs/1",
                        "byte_length": len(payload)}
    # Python JSON's NaN/Infinity tokens are hashed as supplied, not raw HTTP bytes.
    if not count:
        record["segments"] = []
        record["point_count"] = 0
        record["warnings"] = []
        for metric in record["metrics"].values():
            metric["value"] = None
            metric["missing_reason"] = "no_stream_samples"
            for key in ("possible_edges", "timed_edges", "excluded_time_edges",
                        "declared_segments", "used_edges", "sample_count", "valid_sensor_samples"):
                if key in metric["coverage"]:
                    metric["coverage"][key] = 0
    record["warnings"] = [warning for warning in record["warnings"]
        if not ("latlng" not in data and warning["field"] in ("lat", "lon"))]
    for warning in record["warnings"]:
        index = warning["point_index"]
        for name, field in _STREAM_FIELDS.items():
            if index is not None and warning["field"] == field and sample(name, index) is None:
                warning["code"] = "missing_value"
    def warn(code, field):
        record["warnings"].append({"code": code, "segment_index": None,
                                   "point_index": None, "field": field})
    for name, field in {"time": "time", "latlng": "position", **_STREAM_FIELDS}.items():
        if name not in data or not data[name]:
            warn("missing_stream", field)
    if not count:
        warn("no_stream_samples", None)
    if origin is None:
        warn("missing_absolute_time_origin", "time")
    if activity.get("type") not in ("Ride", "VirtualRide"):
        warn("non_ride_activity", "activity_type")
    if len({len(values) for values in data.values()}) > 1:
        warn("stream_length_mismatch", None)
    ftp = _numeric(settings.get("ftp"), 0)
    power_zones, power_zones_note = _power_zones_watts(settings.get("power_zones"), ftp)
    record["coach_context"] = {
        "ftp": ftp,
        "power_zones": power_zones,
        "power_zones_note": power_zones_note,
        "lthr": _numeric(settings.get("lthr"), 0),
        "hr_zones": _zones(settings.get("hr_zones")),
        "ctl": _numeric(activity.get("icu_ctl"), 0),
        "atl": _numeric(activity.get("icu_atl"), 0),
        "training_load": _numeric(activity.get("icu_training_load"), 0),
        "resting_hr": _numeric(health.get("resting_hr"), 0),
        "sleep_secs": _numeric(health.get("sleep_secs"), 0),
    }
    for key in ("ftp", "lthr", "resting_hr"):
        if record["coach_context"][key] == 0:
            record["coach_context"][key] = None
    record["limitations"].extend([
        "Intervals.icu JSON normalized offline; original HTTP bytes unavailable",
        "Stream indices are aligned without interpolation or inferred samples",
        "Distance/velocity streams and activity summary totals are not route geometry",
        "Absolute time requires an explicit UTC offset; naive local time is not UTC",
        "Generated GPX is subject to the existing parser byte/point limits",
    ])
    return record