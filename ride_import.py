"""Import external GPX/TCX activity files as honest ride memories.

中文摘要：把外部 GPX/TCX 骑行文件构建成 luma-ride-memory/3 会话。来源如实
标注 imported_gpx/imported_tcx；汇总数字全部由产出的 series 用记忆模块自身
的公式重算，不放宽任何校验；无心率、时间轴损坏或速度异常的文件如实拒绝，
不修补、不猜测。缺口语义不套用到外部文件（gaps 恒为 0，由 coach_notes 声明）。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from ride_gpx import GPXError, parse_gpx
from ride_tcx import TCXError, parse_tcx
from ride_memory import (MAX_SERIES, SCHEMA_VERSION_V3, RideMemoryError,
                         _ascent_m, _track_stats, validate_session)

MAX_IMPORT_BYTES = 5 * 1024 * 1024  # Aligned with the parsers' raw-file cap.
MIN_HR_POINTS = 2  # A memory without at least two HR samples is not a ride record.

_IMPORT_NOTE = {
    "gpx": "从 GPX 文件导入，由其他设备录制；样本与汇总按文件原值计算，未套用断线缺口语义，时间空洞保留在点的间隔里。",
    "tcx": "从 TCX 文件导入，由其他设备录制；样本与汇总按文件原值计算，未套用断线缺口语义，时间空洞保留在点的间隔里。",
}


class ImportBuildError(ValueError):
    """Sanitized import rejection with a stable code; never echoes file content."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sniff_kind(text: str) -> str:
    """Detect GPX or TCX from the document head; reject everything else."""
    head = text[:4096]
    if "<gpx" in head:
        return "gpx"
    if "<TrainingCenterDatabase" in head:
        return "tcx"
    raise ImportBuildError("unsupported_format", "Unrecognized file: expected a GPX or TCX activity")


def _rfc3339(moment) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _moment(raw: str | None):
    """Parsers emit RFC3339 strings; restore datetimes for arithmetic."""
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def build_import_session(text: str) -> dict[str, Any]:
    """Build and fully validate a v3 memory from GPX/TCX file text.

    Raises ImportBuildError for unsupported/empty/HR-less/implausible files;
    every accepted session passes validate_session unchanged.
    """
    if type(text) is not str or not text.strip():
        raise ImportBuildError("invalid_input", "Expected non-empty file text")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_IMPORT_BYTES:
        raise ImportBuildError("body_too_large", "File exceeds the 5 MB import cap")
    kind = sniff_kind(text)
    try:
        record = parse_gpx(encoded) if kind == "gpx" else parse_tcx(encoded)
    except (GPXError, TCXError) as error:
        raise ImportBuildError(error.code, str(error)) from None

    points = [p for segment in record["segments"] for p in segment["points"]]
    timed = []
    for point in points:
        moment = _moment(point["time"])
        if moment is not None:
            timed.append((moment, point))
    if not timed:
        raise ImportBuildError("no_timeline", "File carries no usable timestamps")
    timed.sort(key=lambda item: item[0])  # Defensive only; parsers keep file order.
    started, ended = timed[0][0], timed[-1][0]
    duration = (ended - started).total_seconds()
    if duration <= 0:
        raise ImportBuildError("no_duration", "File covers no positive time span")

    # Series points are HR samples on the shared timeline. Points without an
    # HR reading are simply not series points; no gap semantics is invented
    # for a file recorded elsewhere.
    series, track, hrs, cads, pwrs = [], [], [], [], []
    previous_t = -1.0
    for moment, point in timed:
        hr = point["heart_rate_bpm"]
        if hr is None or not (0 <= int(round(hr)) <= 65535):
            continue
        t = (moment - started).total_seconds()
        if t <= previous_t:
            continue  # Duplicate/out-of-order timestamp: keep the first sample.
        entry: dict[str, Any] = {"t": t, "hr": int(round(hr))}
        cad, pwr = point["cadence_rpm"], point["power_w"]
        if cad is not None and 0 <= int(round(cad)) <= 255:
            entry["cad"] = int(round(cad))
            cads.append(entry["cad"])
        if pwr is not None and 0 <= int(round(pwr)) <= 65535:
            entry["pwr"] = int(round(pwr))
            pwrs.append(entry["pwr"])
        if point["lat"] is not None and point["lon"] is not None \
                and -90.0 <= point["lat"] <= 90.0 and -180.0 <= point["lon"] <= 180.0:
            alt = point["ele"]
            if alt is not None and not (-500.0 <= alt <= 9000.0):
                alt = None
            entry["lat"], entry["lng"] = float(point["lat"]), float(point["lon"])
            if alt is not None:
                entry["alt"] = float(alt)
            track.append((t, entry["lat"], entry["lng"], entry.get("alt")))
        series.append(entry)
        hrs.append(entry["hr"])
        previous_t = t
    if len(hrs) < MIN_HR_POINTS:
        raise ImportBuildError("no_heart_rate",
                               "File carries too few heart-rate samples; HR-less imports are not supported")

    if len(series) > MAX_SERIES:
        stride = math.ceil(len(series) / MAX_SERIES)
        picked = series[::stride]
        series = picked
        hrs = [p["hr"] for p in series]
        cads = [p["cad"] for p in series if "cad" in p]
        pwrs = [p["pwr"] for p in series if "pwr" in p]
        track = [(p["t"], p["lat"], p["lng"], p.get("alt")) for p in series if "lat" in p]

    summary: dict[str, Any] = {
        "duration_s": duration,
        "avg_hr": math.fsum(hrs) / len(hrs),
        "max_hr": max(hrs),
        "time_in_zone_s": None, "zone_target": None,
        "gaps": 0, "alerts": 0,
        "avg_cadence": math.fsum(cads) / len(cads) if cads else None,
        "avg_power": math.fsum(pwrs) / len(pwrs) if pwrs else None,
        "max_power": max(pwrs) if pwrs else None,
    }
    if track:
        distance, max_speed = _track_stats(track)
        if max_speed > 70.0:
            raise ImportBuildError("implausible_speed",
                                   "Track contains an implausible speed segment; not imported")
        summary["distance_m"] = distance
        summary["avg_speed_mps"] = distance / duration
        summary["max_speed_mps"] = max_speed
        summary["ascent_m"] = _ascent_m(track)
    else:
        summary["distance_m"] = summary["avg_speed_mps"] = summary["max_speed_mps"] = None
        summary["ascent_m"] = None

    session = {
        "schema_version": SCHEMA_VERSION_V3,
        "id": "import-" + record["source"]["sha256"][:24],
        "started_at": _rfc3339(started), "ended_at": _rfc3339(ended),
        "samples_hz": len(series) / duration,
        "series": series, "summary": summary,
        "coach_notes": [_IMPORT_NOTE[kind]],
        "persona_version": "import/1",
        "source": "imported_" + kind,
    }
    try:
        return validate_session(session)
    except RideMemoryError as error:  # Contract details are our bug, not the user's file.
        raise ImportBuildError(error.code, str(error)) from None
