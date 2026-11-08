"""Versioned, offline ride memory contract for the reusable Jarvis memory layer.

中文摘要：只校验结构化记录并返回深拷贝；缺口不插值、缺数据不补零、模拟来源不洗白。
This is a device/UI-independent data boundary, not a database or coaching engine.
Future storage/adapters must preserve schema_version, source and persona_version;
new field shapes require an explicit version migration, not silent reinterpretation.

v1 conventions (all listed fields are required, even when null):
* id: filesystem-safe ASCII identifier, 1..64 letters/digits/underscore/hyphen,
  starting with a letter/digit. No paths, timestamps or IDs are generated here.
* started_at/ended_at: timezone-aware RFC3339 strings (Z or numeric offset).
* samples_hz: positive nominal sampling frequency; gaps never imply regular data.
* series: strictly increasing elapsed seconds t, each {t, hr} OR {t, gap: true}.
  hr is an unsigned 16-bit integer from GATT, not a claim of physiological validity.
  A gap marks loss of observation until the next reported HR, without interpolation.
* summary: duration_s is elapsed wall-clock duration; avg_hr is the arithmetic
  mean of recorded HR samples (not time-weighted); max_hr is their maximum.
  Both are null if no HR was recorded. gaps counts explicit gap markers; alerts
  is a reported nonnegative integer, not inferred from coaching note text.
* zone_target: null or {lower, upper} inclusive user-supplied bpm boundaries.
  time_in_zone_s: null or {below, within, above} observed seconds, total at most
  duration_s. Missing coverage is NOT allocated to a zone. No target implies null
  zone times. This validator does not invent LTHR zones or recompute coverage.
* coach_notes: bounded strings as actually reported/spoken, not instructions.
* source: explicit ble_gatt_0x180d or simulated; absent/unknown is never inferred.

v2 additions (luma-ride-memory/2, everything above unchanged except):
* HR series points may carry optional cad (integer rpm 0..255) and/or pwr
  (integer watts 0..65535), attached only when the sensor reading was fresh.
  Gap points never carry them. Absent channels stay absent, never zero-filled.
* summary is exactly the v1 keys plus avg_cadence, avg_power and max_power,
  each null when no such sample was recorded; present values must equal the
  recorded sample mean/maximum. A v1 summary inside a v2 file is rejected.

v3 additions (luma-ride-memory/3, everything above unchanged except):
* HR series points may carry an optional lat/lng coordinate pair (always
  together; finite, lat in -90..90, lng in -180..180). Gap points never carry
  them. Coordinates are recorded positions, never interpolated across gaps.
* summary is exactly the v2 keys plus distance_m, avg_speed_mps and
  max_speed_mps. The three are all-or-none: one null requires all null.
  When the series carries coordinates, all three must equal the values
  recomputed from the recorded track: distance_m is the haversine sum over
  consecutive coordinate points (spherical Earth, radius 6371000 m, matching
  the recording client), avg_speed_mps is distance_m/duration_s and
  max_speed_mps is the largest per-segment distance/time ratio.
  When the series carries NO coordinates (a privacy-stripped upload), the
  aggregates cannot be recomputed; they are still required to be internally
  consistent: avg_speed_mps == distance_m/duration_s within tolerance and
  0 <= avg_speed_mps <= max_speed_mps <= 70 (a generous physical ceiling,
  not a performance claim). A v2 summary inside a v3 file is rejected.

v3 optional extensions (records without them remain valid v3):
* HR series points with coordinates may also carry alt (finite meters,
  -500..9000, GPS altitude as recorded). alt never appears without lat/lng.
* summary may add ascent_m: cumulative climb, the sum of positive altitude
  deltas between consecutive track points that both carry alt (a point
  without alt breaks the pair, never bridged). When the track holds at
  least one such pair, ascent_m is required and must equal the recomputed
  value; when the track has coordinates but no alt pair, ascent_m must be
  absent or null. On privacy-stripped uploads (no track) ascent_m is kept
  as a nonnegative number or opted out to null.
* session may add activity: "ride", "hike" or "run" (default ride when absent).
  It is a declared scene label, never inferred from speed or terrain.

No HTTP, BLE, TTS, filesystem writes, model calls, diagnosis or prescriptions.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "luma-ride-memory/1"
SCHEMA_VERSION_V2 = "luma-ride-memory/2"
SCHEMA_VERSION_V3 = "luma-ride-memory/3"
SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, SCHEMA_VERSION_V2, SCHEMA_VERSION_V3})
MAX_SERIES = 20000
MAX_COACH_NOTES = 200
MAX_NOTE_CHARS = 2000
EARTH_RADIUS_M = 6371000.0  # Matches the recording client's haversine constant.
MAX_PLAUSIBLE_SPEED_MPS = 70.0  # Generous physical ceiling, never a claim.
SOURCES = frozenset({"ble_gatt_0x180d", "simulated"})
ACTIVITIES = frozenset({"ride", "hike", "run"})
MIN_ALT_M = -500.0
MAX_ALT_M = 9000.0
_FIELDS = frozenset({"schema_version", "id", "started_at", "ended_at", "samples_hz",
                     "series", "summary", "coach_notes", "persona_version", "source"})
_SUMMARY = frozenset({"duration_s", "avg_hr", "max_hr", "time_in_zone_s", "zone_target",
                      "gaps", "alerts"})
_SUMMARY_V2 = _SUMMARY | {"avg_cadence", "avg_power", "max_power"}
_SUMMARY_V3 = _SUMMARY_V2 | {"distance_m", "avg_speed_mps", "max_speed_mps"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z")


class RideMemoryError(ValueError):
    """Sanitized contract rejection with a stable code; never echoes input data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require(condition: bool, message: str, code: str = "invalid_schema") -> None:
    if not condition:
        raise RideMemoryError(code, message)


def _fields(value: Any, names: frozenset | set, label: str) -> None:
    _require(type(value) is dict and set(value) == names, label + " fields do not match v1")


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    _require(type(value) in (int, float), label + " must be a finite number")
    try:
        valid = math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        valid = False
    _require(valid, label + " must be finite and " + ("positive" if positive else "nonnegative"))
    return float(value)


def _count(value: Any, label: str) -> None:
    _require(type(value) is int and 0 <= value <= 2**53 - 1,
             label + " must be a nonnegative JSON-safe integer")


def _text(value: Any, label: str, maximum: int) -> None:
    _require(type(value) is str and 0 < len(value) <= maximum and bool(value.strip()),
             label + " must be bounded nonblank text")
    _require(not any(ord(c) < 32 and c not in "\n\t" for c in value),
             label + " contains unsupported control characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise RideMemoryError("invalid_schema", label + " must be valid Unicode") from None


def _time(value: Any, label: str) -> datetime:
    _require(type(value) is str and _TIME.fullmatch(value) is not None,
             label + " must be timezone-aware RFC3339")
    # RFC3339 -00:00 means unknown local offset; it is not an observed instant.
    _require(not value.endswith("-00:00"), label + " requires a known timezone")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RideMemoryError("invalid_schema", label + " is not a valid instant") from None


def _series_stats(summary: dict, avg_key: str, values: list, max_key: str | None = None) -> None:
    """Recorded mean/maximum cross-check shared by hr/cadence/power channels."""
    if not values:
        _require(summary[avg_key] is None and (max_key is None or summary[max_key] is None),
                 "Absent samples require null " + avg_key + ("" if max_key is None else " and " + max_key))
        return
    avg = _number(summary[avg_key], avg_key)
    _require(math.isclose(avg, math.fsum(values) / len(values), rel_tol=1e-12, abs_tol=1e-6),
             avg_key + " must be the recorded sample mean")
    if max_key is not None:
        maximum = _number(summary[max_key], max_key)
        _require(maximum == max(values), max_key + " must match recorded samples")


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Spherical haversine identical to the recording client's formula."""
    rad = math.pi / 180.0
    dlat = (lat2 - lat1) * rad
    dlng = (lng2 - lng1) * rad
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _track_stats(track: list[tuple]) -> tuple[float, float]:
    """Distance and max per-segment speed recomputed from recorded coordinates."""
    distance = 0.0
    max_speed = 0.0
    for (t1, lat1, lng1, _a1), (t2, lat2, lng2, _a2) in zip(track, track[1:]):
        segment = _haversine_m(lat1, lng1, lat2, lng2)
        distance += segment
        dt = t2 - t1
        if dt > 0:
            max_speed = max(max_speed, segment / dt)
    return distance, max_speed


def _ascent_m(track: list[tuple]) -> float | None:
    """Cumulative climb: sum of positive deltas between consecutive track
    points that BOTH carry alt (identical to the recording client's rule;
    a point without alt breaks the pair, never bridged). Returns None when
    no such pair exists — no altitude data, no climb claim."""
    ascent = 0.0
    pairs = 0
    for (_t1, _la1, _ln1, a1), (_t2, _la2, _ln2, a2) in zip(track, track[1:]):
        if a1 is None or a2 is None:
            continue
        pairs += 1
        delta = a2 - a1
        if delta > 0:
            ascent += delta
    return ascent if pairs else None


def validate_session(session: dict[str, Any]) -> dict[str, Any]:
    """Validate one complete v1 or v2 memory and return a detached, unchanged copy.

    Args:
        session: JSON-compatible v1/v2 object described in this module's contract.
            Missing/null observations must remain explicit; source is mandatory.
    Returns:
        Deep copy with the original values/order, including every gap and source.
        No aggregates, timestamps, zones, alerts or identifiers are synthesized.
    Raises:
        RideMemoryError: Missing/unknown fields, unsupported version/source,
            invalid value/range/ordering, inconsistent recorded summary or limits.
            Error messages contain field labels, not user notes/HR/identifiers.
    Example:
        >>> validate_session({})  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        RideMemoryError: session fields do not match v1
    """
    _require(type(session) is dict and set(session) - {"activity"} == _FIELDS,
             "session fields do not match v1")
    if "activity" in session:
        _require(session["schema_version"] == SCHEMA_VERSION_V3
                 and session["activity"] in ACTIVITIES,
                 "activity must be a declared v3 scene label")
    _require(type(session["schema_version"]) is str and session["schema_version"] in SCHEMA_VERSIONS,
             "Unsupported schema_version")
    v2 = session["schema_version"] in (SCHEMA_VERSION_V2, SCHEMA_VERSION_V3)
    v3 = session["schema_version"] == SCHEMA_VERSION_V3
    _require(type(session["id"]) is str and _ID.fullmatch(session["id"]) is not None,
             "id must be a safe v1 identifier")
    _text(session["persona_version"], "persona_version", 64)
    _require(type(session["source"]) is str and session["source"] in SOURCES,
             "source must explicitly identify BLE or simulated data")
    start = _time(session["started_at"], "started_at")
    end = _time(session["ended_at"], "ended_at")
    _require(end >= start, "ended_at precedes started_at")
    elapsed = (end - start).total_seconds()
    _number(session["samples_hz"], "samples_hz", positive=True)
    series = session["series"]
    _require(type(series) is list and len(series) <= MAX_SERIES,
             "series must be a bounded list", "series_too_large" if type(series) is list else "invalid_schema")
    summary = session["summary"]
    if v3:
        _require(type(summary) is dict and set(summary) - {"ascent_m"} == _SUMMARY_V3,
                 "summary fields do not match v1")
    else:
        _fields(summary, _SUMMARY_V2 if v2 else _SUMMARY, "summary")
    duration = _number(summary["duration_s"], "duration_s")
    _require(math.isclose(duration, elapsed, rel_tol=0, abs_tol=1e-6),
             "duration_s must match the recorded timestamps")
    previous = -1.0
    hrs = []
    cads = []
    pwrs = []
    track = []
    gaps = 0
    for point in series:
        if type(point) is dict:
            allowed = {"t", "hr", "gap", "cad", "pwr", "lat", "lng", "alt"} if v3 else (
                {"t", "hr", "gap", "cad", "pwr"} if v2 else {"t", "hr", "gap"})
            shape_ok = not (set(point) - allowed) and "t" in point and (("hr" in point) != ("gap" in point))
        else:
            shape_ok = False
        _require(type(point) is dict and shape_ok,
                 "series point requires t and exactly one of hr/gap")
        t = _number(point["t"], "series.t")
        _require(previous < t <= duration, "series times must increase within duration_s")
        previous = t
        if "gap" in point:
            _require(point["gap"] is True and set(point) == {"t", "gap"},
                     "gap marker must be true and carry no sensor values")
            gaps += 1
        else:
            hr = point["hr"]
            _require(type(hr) is int and 0 <= hr <= 65535, "hr must be an unsigned GATT integer")
            hrs.append(hr)
            if v2:
                if "cad" in point:
                    _require(type(point["cad"]) is int and 0 <= point["cad"] <= 255,
                             "cad must be an integer rpm 0..255")
                    cads.append(point["cad"])
                if "pwr" in point:
                    _require(type(point["pwr"]) is int and 0 <= point["pwr"] <= 65535,
                             "pwr must be an unsigned watt integer")
                    pwrs.append(point["pwr"])
            if v3 and ("lat" in point or "lng" in point):
                _require("lat" in point and "lng" in point,
                         "lat/lng must always appear together")
                lat, lng = point["lat"], point["lng"]
                _require(type(lat) in (int, float) and math.isfinite(lat),
                         "series.lat must be a finite number")
                _require(type(lng) in (int, float) and math.isfinite(lng),
                         "series.lng must be a finite number")
                _require(-90.0 <= lat <= 90.0, "series.lat must be within -90..90")
                _require(-180.0 <= lng <= 180.0, "series.lng must be within -180..180")
                alt = None
                if "alt" in point:
                    alt = point["alt"]
                    _require(type(alt) in (int, float) and math.isfinite(alt),
                             "series.alt must be a finite number")
                    _require(MIN_ALT_M <= alt <= MAX_ALT_M,
                             "series.alt must be within -500..9000 meters")
                    alt = float(alt)
                track.append((t, float(lat), float(lng), alt))
            else:
                _require("alt" not in point, "alt requires lat/lng coordinates")
    _count(summary["gaps"], "gaps")
    _count(summary["alerts"], "alerts")
    _require(summary["gaps"] == gaps, "gaps must count explicit series markers")
    _series_stats(summary, "avg_hr", hrs, "max_hr")
    if v2:
        _series_stats(summary, "avg_cadence", cads)
        _series_stats(summary, "avg_power", pwrs, "max_power")
    if v3:
        keys = ("distance_m", "avg_speed_mps", "max_speed_mps")
        nulls = [summary[k] is None for k in keys]
        _require(all(nulls) or not any(nulls),
                 "distance_m/avg_speed_mps/max_speed_mps are all-or-none")
        if not any(nulls):
            distance = _number(summary["distance_m"], "distance_m")
            avg_speed = _number(summary["avg_speed_mps"], "avg_speed_mps")
            max_speed = _number(summary["max_speed_mps"], "max_speed_mps")
            _require(max_speed <= MAX_PLAUSIBLE_SPEED_MPS,
                     "max_speed_mps exceeds a generous physical ceiling")
            if track:
                expected_distance, expected_max = _track_stats(track)
                _require(math.isclose(distance, expected_distance, rel_tol=1e-9, abs_tol=1e-6),
                         "distance_m must equal the recorded track length")
                _require(math.isclose(max_speed, expected_max, rel_tol=1e-9, abs_tol=1e-9),
                         "max_speed_mps must match the recorded track")
            _require(math.isclose(avg_speed, distance / duration, rel_tol=1e-9, abs_tol=1e-9),
                     "avg_speed_mps must equal distance_m/duration_s")
            _require(avg_speed <= max_speed, "avg_speed_mps cannot exceed max_speed_mps")
        else:
            _require(not track, "Recorded coordinates require distance/speed aggregates")
        expected_ascent = _ascent_m(track)
        if expected_ascent is not None:
            _require(summary.get("ascent_m") is not None,
                     "Recorded altitude requires ascent_m")
            _require(math.isclose(_number(summary["ascent_m"], "ascent_m"), expected_ascent,
                                  rel_tol=1e-9, abs_tol=1e-6),
                     "ascent_m must equal the recorded track climb")
        elif track:
            _require(summary.get("ascent_m") is None,
                     "No altitude data forbids an ascent claim")
        elif summary.get("ascent_m") is not None:
            _number(summary["ascent_m"], "ascent_m")  # Stripped upload: bounded check only.
    target, times = summary["zone_target"], summary["time_in_zone_s"]
    if target is None:
        _require(times is None, "No zone_target requires null time_in_zone_s")
    else:
        _fields(target, {"lower", "upper"}, "zone_target")
        lower = _number(target["lower"], "zone_target.lower")
        upper = _number(target["upper"], "zone_target.upper")
        _require(lower <= upper <= 65535, "zone_target bounds must be ordered GATT values")
        if times is not None:
            _fields(times, {"below", "within", "above"}, "time_in_zone_s")
            values = [_number(times[k], "time_in_zone_s." + k) for k in ("below", "within", "above")]
            _require(all(value <= duration for value in values), "Zone time exceeds duration_s")
            total = math.fsum(values)
            _require(total <= duration + 1e-6, "Zone times exceed duration_s")
            _require(bool(hrs) or total == 0, "No HR samples can support positive zone time")
    notes = session["coach_notes"]
    _require(type(notes) is list and len(notes) <= MAX_COACH_NOTES, "coach_notes must be a bounded list")
    for note in notes:
        _text(note, "coach_notes item", MAX_NOTE_CHARS)
    return copy.deepcopy(session)
