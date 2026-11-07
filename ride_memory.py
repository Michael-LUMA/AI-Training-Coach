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

No HTTP, BLE, TTS, filesystem writes, model calls, diagnosis or prescriptions.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "luma-ride-memory/1"
MAX_SERIES = 20000
MAX_COACH_NOTES = 200
MAX_NOTE_CHARS = 2000
SOURCES = frozenset({"ble_gatt_0x180d", "simulated"})
_FIELDS = frozenset({"schema_version", "id", "started_at", "ended_at", "samples_hz",
                     "series", "summary", "coach_notes", "persona_version", "source"})
_SUMMARY = frozenset({"duration_s", "avg_hr", "max_hr", "time_in_zone_s", "zone_target",
                      "gaps", "alerts"})
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


def validate_session(session: dict[str, Any]) -> dict[str, Any]:
    """Validate one complete v1 memory and return a detached, unchanged copy.

    Args:
        session: JSON-compatible v1 object described in this module's contract.
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
    _fields(session, _FIELDS, "session")
    _require(session["schema_version"] == SCHEMA_VERSION, "Unsupported schema_version")
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
    _fields(summary, _SUMMARY, "summary")
    duration = _number(summary["duration_s"], "duration_s")
    _require(math.isclose(duration, elapsed, rel_tol=0, abs_tol=1e-6),
             "duration_s must match the recorded timestamps")
    previous = -1.0
    hrs = []
    gaps = 0
    for point in series:
        _require(type(point) is dict and set(point) in ({"t", "hr"}, {"t", "gap"}),
                 "series point requires t and exactly one of hr/gap")
        t = _number(point["t"], "series.t")
        _require(previous < t <= duration, "series times must increase within duration_s")
        previous = t
        if "gap" in point:
            _require(point["gap"] is True, "gap marker must be true")
            gaps += 1
        else:
            hr = point["hr"]
            _require(type(hr) is int and 0 <= hr <= 65535, "hr must be an unsigned GATT integer")
            hrs.append(hr)
    _count(summary["gaps"], "gaps")
    _count(summary["alerts"], "alerts")
    _require(summary["gaps"] == gaps, "gaps must count explicit series markers")
    if not hrs:
        _require(summary["avg_hr"] is None and summary["max_hr"] is None,
                 "Absent HR requires null avg_hr and max_hr")
    else:
        avg = _number(summary["avg_hr"], "avg_hr")
        maximum = _number(summary["max_hr"], "max_hr")
        _require(maximum == max(hrs), "max_hr must match recorded samples")
        _require(math.isclose(avg, math.fsum(hrs) / len(hrs), rel_tol=1e-12, abs_tol=1e-6),
                 "avg_hr must be the recorded sample mean")
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
