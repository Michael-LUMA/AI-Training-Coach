"""English presentation and context facts for the shared ride review engine.

中文摘要：复用原复盘事实与规则，仅增加英文文案及有据的上下文、区间事实。
No HTTP, persistence, medical advice, inferred zones or training prescriptions.
"""
from __future__ import annotations

import copy
from bisect import bisect_right
from typing import Any

from ride_gpx import MAX_GAP_S, _timestamp

DISCLAIMER = ("Rule-based record notes only, not a training prescription or medical advice. "
              "No training outcome or health benefit is promised; decisions remain yours.")
_CONTEXT_UNITS = {"ftp": "W", "power_zones": "W", "lthr": "bpm", "hr_zones": "bpm",
                  "ctl": None, "atl": None, "training_load": None,
                  "resting_hr": "bpm", "sleep_secs": "s", "power_zones_note": None}
_LABELS = ("Distance", "Valid continuous duration", "Elapsed duration", "Moving duration",
           "Average speed", "Elevation gain", "Average heart rate", "Average power")
_TEXT = {
    "complete-record": "Add time, distance or a personal note before deciding on another ride.",
    "self-directed-next-ride": "If you choose another ride, select your own route and duration; these records do not establish a suitable intensity.",
    "record-coverage": "Record missing measurements with available equipment next time; do not invent heart rate or power.",
    "route-choice": "If you prefer less elevation variation, consider a flatter route; no training outcome is promised.",
}


def build_review(record: dict[str, Any] | None = None,
                 manual_summary: dict[str, Any] | None = None, *,
                 coach_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an unconfirmed English draft through the shared review engine.

    Args:
        record: Normalized GPX/TCX/Intervals record, including optional context.
        manual_summary: Optional user statements; never override record facts.
        coach_context: Optional normalized context, overriding embedded context.
    Returns:
        Shared review schema: facts, rule_inferences, training_suggestion.
    Raises:
        ride_review.ReviewError: Invalid or oversized record/summary/context.
    Example:
        >>> build_review()['status']
        'draft'
    """
    from ride_review import build_review as shared_build
    return shared_build(record, manual_summary, lang="en", coach_context=coach_context)


def _context_values(context):
    from ride_review import ReviewError
    if context is None:
        context = {}
    if type(context) is not dict or set(context) - set(_CONTEXT_UNITS):
        raise ReviewError("invalid_context", "Unknown or invalid context fields")
    result = {}
    for key in _CONTEXT_UNITS:
        value = context.get(key)
        if value is not None:
            if key == "power_zones_note":
                if type(value) is not str or len(value) > 128:
                    raise ReviewError("invalid_context", "power_zones_note must be a short string")
                result[key] = value
                continue
            values = value if key.endswith("zones") and type(value) is list else [value]
            if (key.endswith("zones") and (type(value) is not list or not value)) or any(
                    type(v) not in (int, float) or v < 0 for v in values):
                raise ReviewError("invalid_context", "Context requires nonnegative numbers or ordered zone arrays")
            if key.endswith("zones") and any(a >= b for a, b in zip(values, values[1:])):
                raise ReviewError("invalid_context", "Zone boundaries must increase strictly")
            if key in ("ftp", "lthr", "resting_hr") and value == 0:
                raise ReviewError("invalid_context", "FTP and heart rates must be positive")
        result[key] = copy.deepcopy(value)
    return result


def _power_zones(review, values, refs):
    """Left-held samples on valid within-segment edges; never bridge gaps."""
    thresholds = values["power_zones"]
    if values["ftp"] is None or thresholds is None or len(thresholds) < 2 or thresholds[0] != 0:
        return None
    seconds = [0.0] * len(thresholds)
    used = excluded = 0
    capture_gap = any(w.get("code") == "capture_gap" for w in review["facts"]["warnings"]
                      if isinstance(w, dict))
    for segment in review["facts"]["segments"]:
        points = segment["points"]
        for a, b in zip(points, points[1:]):
            if not isinstance(a, dict) or not isinstance(b, dict):
                excluded += 1
                continue
            ta, tb = a.get("time"), b.get("time")
            ta = _timestamp(ta) if isinstance(ta, str) else None
            tb = _timestamp(tb) if isinstance(tb, str) else None
            pa, pb = a.get("power_w"), b.get("power_w")
            duration = (tb - ta).total_seconds() if ta is not None and tb is not None else 0
            if (capture_gap or not 0 < duration <= MAX_GAP_S or
                    any(type(p) not in (int, float) or p < 0 for p in (pa, pb))):
                excluded += 1
                continue
            seconds[bisect_right(thresholds, pa) - 1] += duration
            used += 1
    total = sum(seconds)
    if total == 0:
        return None
    converted = values.get("power_zones_note") == "percent_of_ftp_upper_bounds_converted_to_watts"
    basis = ("Zone boundaries were supplied as percent-of-FTP uppers and converted to absolute watts using the reported FTP."
             if converted else
             "Supplied zone boundaries are treated as absolute watt lower bounds, not FTP percentages.")
    return {"method": "left_sample_hold_valid_edges/1", "unit": "s",
            "ftp_w": values["ftp"], "classified_duration_s": total,
            "used_edges": used, "excluded_edges": excluded,
            "basis_refs": [refs["ftp"], refs["power_zones"],
                           {"source_sha256": review["source"]["sha256"], "locator": "segments"}],
            "limitations": [basis,
                            "Percentages cover classified time only, not elapsed or whole-ride time.",
                            "Both endpoint samples must be valid; no interpolation or cross-segment bridging.",
                            "A declared capture gap disables zone calculation conservatively."],
            "bins": [{"zone": i + 1, "lower_w": low,
                      "upper_w": thresholds[i + 1] if i + 1 < len(thresholds) else None,
                      "duration_s": seconds[i], "percentage": seconds[i] / total * 100}
                     for i, low in enumerate(thresholds)]}


def _render_english(review, context, *, context_from_record):
    from ride_review import METRICS
    values = _context_values(context)
    facts = review["facts"]
    facts["messages"] = [
        f"{label}: insufficient data" if facts["metrics"][name]["value"] is None else
        f"{label}: {facts['metrics'][name]['value']:.6g} {unit}"
        for (name, _, unit, _), label in zip(METRICS, _LABELS)]
    if facts.get("has_position") is False:
        facts["messages"].append("GPS: insufficient data; no route or position is invented.")
    for statement in review["user_statements"]:
        if statement["metric"] is not None:
            statement["text"] = "User-reported measurement"
    for inference in review["rule_inferences"]:
        inference["text"] = ("[Inference] Raw ascent relative to valid distance suggests terrain variation. "
                             "This is not a gradient estimate; altitude noise and missing sections may affect it.")
        inference["limitations"] = ["2% is a display heuristic, not a training-science threshold.",
                                    "May rely on user statements; inspect basis_refs."]
    suggestion = review["training_suggestion"]
    for item in suggestion["items"]:
        item["text"] = _TEXT[item["template_id"]]
        item["disclaimer"] = DISCLAIMER
    suggestion["text"] = "\n".join(item["text"] for item in suggestion["items"])
    suggestion["disclaimer"] = DISCLAIMER
    refs = {key: {"source_sha256": review["source"]["sha256"] if context_from_record and review["source"]["kind"] == "intervals_icu" else None,
                  "locator": ("coach_context." if context_from_record else "provided_coach_context.") + key}
            for key in values}
    facts["coach_context"] = {key: {"value": value, "unit": _CONTEXT_UNITS[key],
        "origin": "provided_context", "evidence_ref": refs[key],
        "missing_reason": "insufficient data" if value is None else None}
        for key, value in values.items()}
    for key in ("ftp", "lthr", "ctl", "atl", "training_load", "resting_hr", "sleep_secs"):
        value = values[key]
        facts["messages"].append(f"{key}: insufficient data" if value is None else f"Reported {key}: {value:g}")
    facts["zones"]["power"] = _power_zones(review, values, refs)
    if facts["zones"]["power"] is None:
        facts["messages"].append("Power zones: insufficient data; no default zones are inferred.")
    facts["messages"].append("CTL/ATL and wellness are reported context, not proof of fatigue, recovery or readiness; insufficient data for those conclusions.")
