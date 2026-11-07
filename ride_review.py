"""Local deterministic review drafts; no cloud, persistence or calendar effects.

build_review(record=None, manual_summary=None): record is a ride_gpx/ride_tcx
parser dict. Manual input is {text?: str, metrics?: {metric_name: number|null}}.
Manual values remain user statements, never replace file facts. Rule inferences
are separate from ai_inferences (always empty). Draft-only extensions are
rule_inferences, facts.messages and metrics.continuous_duration_s.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "luma-ride-review/1-draft"
TEMPLATE_VERSION = "ride-review-rules/1"
MAX_TEXT_CHARS = 8192
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_NODES = 1000000
MAX_INPUT_DEPTH = 64
DISCLAIMER = "仅为规则模板和记录提示，由用户自行决定；不是专业训练处方，不宣称训练科学效果或健康收益。"
# Output name, parser key, unit, display label. Continuous time is NOT elapsed time.
METRICS = (
    ("distance_m", "total_distance_m", "m", "距离"),
    ("continuous_duration_s", "total_duration_s", "s", "有效连续时长"),
    ("elapsed_duration_s", None, "s", "总经过时长"),
    ("moving_duration_s", None, "s", "移动时长"),
    ("average_speed_mps", None, "m/s", "平均速度"),
    ("elevation_gain_m", "elevation_gain_m", "m", "爬升"),
    ("average_heart_rate_bpm", "average_heart_rate_bpm", "bpm", "心率"),
    ("average_power_w", "average_power_w", "W", "功率"),
)


class ReviewError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _bounded_json(value):
    """Bound work before serialization; reject cycles, non-JSON and nonfinite."""
    active = set()
    nodes = 0
    string_bytes = 0

    def visit(item, depth):
        nonlocal nodes, string_bytes
        nodes += 1
        if nodes > MAX_INPUT_NODES or depth > MAX_INPUT_DEPTH:
            raise ReviewError("input_too_large", "Input exceeds structural limits")
        if item is None or type(item) is bool:
            return
        if type(item) in (int, float):
            try:
                valid = math.isfinite(item)
            except OverflowError:
                valid = False
            if not valid:
                raise ReviewError("invalid_input", "Numbers must be finite")
            return
        if type(item) is str:
            if len(item) > MAX_TEXT_CHARS:
                raise ReviewError("input_too_large", "Text exceeds character limit")
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                raise ReviewError("invalid_input", "Invalid Unicode") from None
            if string_bytes > MAX_INPUT_BYTES:
                raise ReviewError("input_too_large", "Input exceeds byte limit")
            return
        if type(item) not in (dict, list):
            raise ReviewError("invalid_input", "Only JSON-compatible data is accepted")
        identity = id(item)
        if identity in active:
            raise ReviewError("invalid_input", "Cyclic input is forbidden")
        active.add(identity)
        try:
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ReviewError("invalid_input", "Keys must be strings")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        finally:
            active.remove(identity)
    visit(value, 0)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise ReviewError("input_too_large", "Input exceeds byte limit")


def _number(value, name):
    if type(value) not in (int, float) or value < 0 or (name == "average_heart_rate_bpm" and value == 0):
        raise ReviewError("invalid_metric", "Metric must be a valid nonnegative number (heart rate positive)")
    return value


def _source(record, summary):
    if record is not None:
        if record.get("schema_version") != "luma-ride-gpx/1":
            raise ReviewError("invalid_record", "Unsupported parser schema")
        source = record.get("source")
        if type(source) is not dict or source.get("kind") not in ("gpx", "tcx", "intervals_icu"):
            raise ReviewError("invalid_record", "Expected GPX/TCX or Intervals JSON source")
        digest = source.get("sha256")
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReviewError("invalid_record", "Expected source SHA256")
        expected_scope = ("canonical_python_json_inputs/1" if source["kind"] == "intervals_icu"
                          else "raw_file_bytes")
        if source.get("hash_scope") != expected_scope or type(source.get("byte_length")) is not int or source["byte_length"] < 0:
            raise ReviewError("invalid_record", "Invalid source byte provenance")
        # No copying arbitrary path, consent or authorization claims.
        result = {key: source[key] for key in ("kind", "sha256", "hash_scope", "byte_length")}
        result["text_encoding"] = None
    else:
        text = summary.get("text")
        raw = text.encode("utf-8") if text is not None else None
        result = {"kind": "manual_summary", "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                  "hash_scope": "manual_text_utf8_exact" if raw is not None else None,
                  "byte_length": len(raw) if raw is not None else None, "text_encoding": "UTF-8"}
    result.update({"display_name": None, "media_type": None, "dataset_kind": None,
                   "storage_consent": False, "source_ref": None})
    return result


def build_review(record=None, manual_summary=None, *, lang="zh", coach_context=None):
    """Return a draft without effects; lang is zh (legacy default) or en.

    English mode reuses all fact projections and inference decisions, then
    renders English templates and optional normalized coaching context.
    Invalid languages/context raise ReviewError. Context is not a diagnosis.
    """
    if lang not in ("zh", "en"):
        raise ReviewError("invalid_language", "Supported languages: zh, en")
    if coach_context is not None and type(coach_context) is not dict:
        raise ReviewError("invalid_context", "Coach context must be a dict")
    if record is not None and type(record) is not dict:
        raise ReviewError("invalid_record", "Parser record must be a dict")
    if manual_summary is not None and type(manual_summary) is not dict:
        raise ReviewError("invalid_summary", "Manual summary must be a dict")
    _bounded_json({"record": record, "manual_summary": manual_summary, "coach_context": coach_context})
    summary = manual_summary if manual_summary is not None else {}
    if set(summary) - {"text", "metrics"}:
        raise ReviewError("invalid_summary", "Unknown manual summary fields")
    if "text" in summary and type(summary["text"]) is not str:
        raise ReviewError("invalid_summary", "Summary text must be a string")
    manual_metrics = summary.get("metrics", {})
    if type(manual_metrics) is not dict or set(manual_metrics) - {m[0] for m in METRICS}:
        raise ReviewError("invalid_summary", "Unknown manual metric fields")
    for name, value in manual_metrics.items():
        if value is not None:
            _number(value, name)
    if record is not None:
        if set(record) - {"schema_version", "source", "segments", "metrics", "warnings", "point_count", "limitations", "has_position", "route_summary", "coach_context"}:
            raise ReviewError("invalid_record", "Unknown parser record fields")
        if type(record.get("segments", [])) is not list or type(record.get("warnings", [])) is not list:
            raise ReviewError("invalid_record", "Segments and warnings must be arrays")
        if "point_count" in record and (type(record["point_count"]) is not int or record["point_count"] < 0):
            raise ReviewError("invalid_record", "Point count must be a nonnegative integer")
        if "has_position" in record and type(record["has_position"]) is not bool:
            raise ReviewError("invalid_record", "Position presence must be a boolean")
        if "route_summary" in record:
            route = record["route_summary"]
            if (type(route) is not dict or set(route) != {"value", "missing_reason"}
                    or route["value"] is not None or route["missing_reason"] not in
                    ("no_valid_positions", "not_generated")
                    or type(record.get("has_position")) is not bool
                    or (route["missing_reason"] == "no_valid_positions") != (not record["has_position"])):
                raise ReviewError("invalid_record", "Invalid parser route availability")
        for segment in record.get("segments", []):
            if type(segment) is not dict or type(segment.get("points")) is not list:
                raise ReviewError("invalid_record", "Invalid parser segment")
    source = _source(record, summary)
    parser_metrics = record.get("metrics", {}) if record is not None else {}
    if type(parser_metrics) is not dict:
        raise ReviewError("invalid_record", "Parser metrics must be a dict")
    metrics, messages = {}, []
    for name, parser_key, unit, label in METRICS:
        metric = {"value": None, "unit": unit, "origin": "calculated", "evidence_ref": None,
                  "method": "parser_metric_projection/1", "missing_reason": "not_recorded"}
        if parser_key is not None and parser_key in parser_metrics:
            raw = parser_metrics[parser_key]
            if type(raw) is not dict or "value" not in raw or raw.get("unit") != unit:
                raise ReviewError("invalid_metric", "Invalid parser metric shape or unit")
            value = raw["value"]
            if value is not None:
                _number(value, name)
            origin = raw.get("origin")
            method = raw.get("method")
            if origin not in ("calculated", "file_reported") or (origin == "calculated" and (type(method) is not str or not method)):
                raise ReviewError("invalid_metric", "Metric origin and method are required")
            if origin == "file_reported" and method is not None:
                raise ReviewError("invalid_metric", "Reported metric method must be null")
            reason = raw.get("missing_reason")
            if value is None and (type(reason) is not str or not reason):
                raise ReviewError("invalid_metric", "Missing metric requires a reason")
            if value is not None and reason is not None:
                raise ReviewError("invalid_metric", "Present metric cannot have a missing reason")
            metric.update({"value": value, "origin": origin, "method": method,
                           "missing_reason": reason,
                           "evidence_ref": {"source_sha256": source["sha256"],
                                            "locator": "metrics." + parser_key + ".value"}})
            if "coverage" in raw:
                metric["coverage"] = copy.deepcopy(raw["coverage"])
        metrics[name] = metric
        messages.append("本次无" + label + "数据" if metric["value"] is None else
                        "本次" + label + "：" + format(metric["value"], ".6g") + " " + unit)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    statements = []
    if "text" in summary:
        statements.append({"id": "statement-text", "category": "review_note", "text": summary["text"],
                           "metric": None, "value": None, "unit": None, "origin": "user",
                           "evidence_ref": {"source_sha256": source["sha256"] if record is None else None,
                                            "locator": "manual_summary.text"}, "recorded_at": now})
    manual_refs = {}
    for name, _, unit, _ in METRICS:
        if name in manual_metrics and manual_metrics[name] is not None:
            manual_refs[name] = {"source_sha256": None, "locator": "user_statements[" + str(len(statements)) + "]"}
            statements.append({"id": "statement-" + name, "category": "summary_metric", "text": "用户陈述",
                               "metric": name, "value": manual_metrics[name], "unit": unit, "origin": "user",
                               "evidence_ref": {"source_sha256": None, "locator": "manual_summary.metrics." + name},
                               "recorded_at": now})

    def available(name):
        fact = metrics[name]
        if fact["value"] is not None:
            return fact["value"], fact["evidence_ref"]
        return manual_metrics.get(name), manual_refs.get(name)

    distance, distance_ref = available("distance_m")
    gain, gain_ref = available("elevation_gain_m")
    inferences = []
    # Product display heuristic only. Raw ascent ratio is NOT a gradient or a
    # physiological threshold; sensor noise can trigger it. No intensity zones.
    if distance is not None and distance > 0 and gain is not None and gain >= distance * 0.02:
        inferences.append({"id": "terrain-variation", "origin": "rule_template", "template_id": "terrain-ratio",
                           "template_version": TEMPLATE_VERSION,
                           "text": "[推测] 原始爬升与有效距离之比偏高，路线可能起伏较大；这不是坡度判定，海拔噪声和缺段也可能影响结果。",
                           "basis_refs": [distance_ref, gain_ref],
                           "method": "positive_distance_and_gain_ge_distance_times_0.02/1",
                           "limitations": ["2% 为展示启发式，不是训练科学阈值", "可能依据用户陈述；来源见 basis_refs"]})
    available_refs = [ref for name, _, _, _ in METRICS for value, ref in [available(name)] if value is not None]
    items = []
    if not available_refs:
        items.append({"template_id": "complete-record", "text": "先补充本次骑行的时间、距离或主观记录，再自行决定是否安排下一次骑行。",
                      "basis_refs": [], "kind": "data_quality"})
    else:
        items.append({"template_id": "self-directed-next-ride", "text": "若你决定安排下一次骑行，请自行选择可接受的路线与时长；本记录不推定适合你的强度。",
                      "basis_refs": available_refs, "kind": "planning"})
        if any(available(name)[0] is None for name in ("distance_m", "continuous_duration_s", "average_heart_rate_bpm", "average_power_w")):
            items.append({"template_id": "record-coverage", "text": "下次可按现有设备条件补充缺失记录；没有心率或功率设备时不据此编造数据。",
                          "basis_refs": available_refs, "kind": "data_quality"})
        if inferences:
            items.append({"template_id": "route-choice", "text": "若再次骑行且希望减少路线起伏，可自行选择更平缓路线；不据此承诺训练效果。",
                          "basis_refs": [distance_ref, gain_ref], "kind": "planning"})
    for item in items:
        item.update({"origin": "rule_template", "template_version": TEMPLATE_VERSION, "disclaimer": DISCLAIMER})
    suggestion = {"id": "suggestion-draft", "origin": "rule_template", "template_id": "ride-review-minimal",
                  "template_version": TEMPLATE_VERSION, "text": "\n".join(item["text"] for item in items),
                  "basis_refs": available_refs, "items": items, "user_edits": [], "scheduled_start": None,
                  "duration_s": None, "disclaimer": DISCLAIMER}
    # Source segments are retained as parser evidence, not rewritten into fake
    # per-segment summaries, times, locations or training intervals.
    facts = {"metrics": metrics, "messages": messages,
             "route_summary": {"point_count": record.get("point_count") if record is not None else None,
                               "segment_count": len(record["segments"]) if record is not None and type(record.get("segments")) is list else None,
                               "start_time": None, "end_time": None},
             "segments": copy.deepcopy(record.get("segments", [])) if record is not None else [],
             "zones": {"heart_rate": None, "power": None},
             "warnings": copy.deepcopy(record.get("warnings", [])) if record is not None else []}
    if record is not None and "has_position" in record:
        facts["has_position"] = record["has_position"]
        if "route_summary" in record:
            facts["route_summary"].update(copy.deepcopy(record["route_summary"]))
        if not record["has_position"]:
            messages.append("???GPS?????????????????????????")
    result = {"schema_version": SCHEMA_VERSION, "id": uuid.uuid4().hex, "revision": 1,
            "created_at": now, "updated_at": now, "source": source, "facts": facts,
            "user_statements": statements, "ai_inferences": [], "rule_inferences": inferences,
            "training_suggestion": suggestion, "confirmation": None, "status": "draft",
            "calendar_authorization": None, "feedback": None, "receipts": []}


    if lang == "en":
        from coach_review_en import _render_english
        context = coach_context if coach_context is not None else (record or {}).get("coach_context")
        _render_english(result, context, context_from_record=coach_context is None)
    return result
