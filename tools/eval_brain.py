"""Run the Chinese golden corpus through the real bounded chat verifier (stdlib only)."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys

# The CLI must not create extra files, including imported-module bytecode caches.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ride_chat

DEFAULT_CORPUS = ROOT / "corpus" / "eval_zh_v1.json"
SUMMARY_FIELDS = {"duration_s", "avg_hr", "max_hr", "time_in_zone_s",
                  "zone_target", "gaps", "alerts"}
TOTAL_LIMITS = {"sessions_count": 100000, "duration_min": 600000,
                "distance_km": 200000, "month_sessions": 100000,
                "month_duration_min": 100000, "month_distance_km": 100000}
REQUIRED = {"id", "question", "expect", "note"}
OPTIONAL = {"recent_summaries", "totals", "activity_ordinal", "accept_any",
            "reject_any", "canned_speech"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _text(value, maximum=None):
    return (type(value) is str and bool(value.strip())
            and (maximum is None or len(value) <= maximum)
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_corpus(corpus):
    """Validate every case before any provider is called; return the original list."""
    _require(type(corpus) is list, "corpus must be a list")
    _require(bool(corpus), "corpus must contain at least one case")
    seen = set()
    for index, case in enumerate(corpus):
        prefix = f"case[{index}]"
        _require(type(case) is dict, f"{prefix}: case must be an object")
        _require(REQUIRED <= case.keys(), f"{prefix}: missing required fields {sorted(REQUIRED - case.keys())}")
        _require(not (case.keys() - REQUIRED - OPTIONAL), f"{prefix}: unknown case fields")
        identifier = case["id"]
        _require(type(identifier) is str and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier),
                 f"{prefix}: id must be a kebab-case string")
        _require(identifier not in seen, f"{prefix}: duplicate id {identifier}")
        seen.add(identifier)
        _require(_text(case["question"], 200), f"{prefix}: question must be 1..200 printable characters")
        _require(_text(case["note"]), f"{prefix}: note must be a non-empty string")
        _require(type(case["expect"]) is str and case["expect"] in ("ok", "fallback"),
                 f"{prefix}: expect must be ok or fallback")
        for field in ("accept_any", "reject_any"):
            if field in case:
                values = case[field]
                _require(type(values) is list and all(_text(v) for v in values),
                         f"{prefix}: {field} must be a list of non-empty strings")
                if field == "accept_any":
                    _require(bool(values) and case["expect"] == "ok",
                             f"{prefix}: accept_any requires expect=ok and at least one substring")
        if "canned_speech" in case:
            _require(_text(case["canned_speech"], 300), f"{prefix}: invalid canned_speech")
        if "activity_ordinal" in case:
            value = case["activity_ordinal"]
            _require(type(value) is int and 0 <= value <= 100000,
                     f"{prefix}: activity_ordinal must be a bounded non-negative integer")
        if "recent_summaries" in case:
            summaries = case["recent_summaries"]
            _require(type(summaries) is list, f"{prefix}: recent_summaries must be a list")
            for summary in summaries:
                _require(type(summary) is dict, f"{prefix}: summary must be an object")
                _require(not (summary.keys() - SUMMARY_FIELDS), f"{prefix}: unknown summary field")
                _require(all(v is None or _number(v) for v in summary.values()),
                         f"{prefix}: summary values must be non-negative finite numbers or null")
        if "totals" in case:
            totals = case["totals"]
            _require(type(totals) is dict and totals.keys() == TOTAL_LIMITS.keys(),
                     f"{prefix}: totals must contain all six registered keys")
            for field, maximum in TOTAL_LIMITS.items():
                value = totals[field]
                if field in ("distance_km", "month_distance_km") and value is None:
                    continue
                _require(_number(value) and value <= maximum, f"{prefix}: invalid totals.{field}")
                if field in ("sessions_count", "month_sessions"):
                    _require(type(value) is int, f"{prefix}: totals.{field} must be an integer")
    return corpus


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_corpus(path):
    """Invalid files and invalid schemas are CLI usage errors, never partial runs."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        corpus = json.loads(text, object_pairs_hook=_unique_object)
        return validate_corpus(corpus)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        print(f"Invalid corpus: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def mock_transport(case):
    """Replay text in the upstream HTTP envelope; do not bypass ride_chat checks."""
    speech = case.get("canned_speech", "这个我没记到。")
    envelope = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"speech": speech}, ensure_ascii=False)}}]}
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    def http_post(request, timeout):
        return 200, {"Content-Type": "application/json"}, body

    return http_post


def judge_case(case, result):
    """Any non-ok chat status is a fallback; rejected is also counted separately."""
    status = result["llm_status"]
    draft = result.get("draft")
    speech = draft["speech"] if status == "ok" and draft else ride_chat.chat_template()
    outcome = "ok" if status == "ok" else "fallback"
    reasons = []
    if outcome != case["expect"]:
        reasons.append(f"expected {case['expect']}, got {outcome} ({status})")
    if case["expect"] == "ok" and case.get("accept_any"):
        if not any(part in speech for part in case["accept_any"]):
            reasons.append("speech did not match accept_any")
    forbidden = [part for part in case.get("reject_any", []) if part in speech]
    if forbidden:
        reasons.append(f"speech matched reject_any: {forbidden}")
    return {"id": case["id"], "expect": case["expect"], "status": status,
            "outcome": outcome, "speech": speech, "passed": not reasons,
            "reasons": reasons}


def run_evaluation(cases, provider="mock", api_key=None):
    """Run all cases and return a JSON-serializable report without writing files."""
    validate_corpus(cases)
    if provider not in ("mock", "dashscope"):
        raise ValueError("provider must be mock or dashscope")
    if provider == "dashscope" and not api_key:
        raise ValueError("dashscope requires --key or DASHSCOPE_API_KEY")
    rows = []
    for case in cases:
        result = ride_chat.build_ride_chat(
            case["question"], session=None,
            recent_summaries=case.get("recent_summaries"),
            totals=case.get("totals"), activity_ordinal=case.get("activity_ordinal"),
            api_key="offline-eval-key" if provider == "mock" else api_key,
            http_post=mock_transport(case) if provider == "mock" else None)
        row = judge_case(case, result)
        rows.append(row)
        print(f"{row['id']}\t{row['status']}\t{'pass' if row['passed'] else 'fail'}")
    summary = {"pass": sum(row["passed"] for row in rows), "total": len(rows),
               "fallback": sum(row["outcome"] == "fallback" for row in rows),
               "rejected": sum(row["status"].startswith("rejected") for row in rows)}
    summary["pass_rate"] = summary["pass"] / summary["total"]
    print(f"{provider}: {summary['pass']}/{summary['total']} pass "
          f"({summary['pass_rate']:.0%}), fallback {summary['fallback']}, "
          f"rejected {summary['rejected']}")
    return {"provider": provider, "summary": summary, "cases": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--provider", choices=("mock", "dashscope"), default="mock")
    parser.add_argument("--key", help="DashScope API key (or DASHSCOPE_API_KEY)")
    parser.add_argument("--out", type=Path, help="Write a UTF-8 JSON report")
    args = parser.parse_args(argv)
    cases = load_corpus(args.corpus)
    key = args.key or os.environ.get("DASHSCOPE_API_KEY")
    if args.provider == "dashscope" and not key:
        parser.error("dashscope requires --key or DASHSCOPE_API_KEY")
    report = run_evaluation(cases, provider=args.provider, api_key=key)
    if args.out is not None:
        try:
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                           allow_nan=False) + "\n", encoding="utf-8")
        except (OSError, ValueError) as error:
            parser.error(f"cannot write report: {error}")
    return 0 if report["summary"]["pass"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
