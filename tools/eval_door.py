"""Door-level golden corpus: one door, three lanes, real routing and verification (stdlib only).

Seeds two stored sessions and two live sessions, then runs every case through
App._chat_ride with a replay transport keyed on the question text. No network,
no files written, no real upstream — the full verification chain stays live.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import Mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import coach_server

DEFAULT_CORPUS = ROOT / "corpus" / "eval_door_v1.json"
REQUIRED = {"id", "question", "expect", "note"}
OPTIONAL = {"lane", "live_id", "canned", "accept_any", "reject_any", "weather", "nav", "plan"}
LANES = ("live", "personal", "knowledge", "general")
HONEST_DEFAULT = "这个我没记到。"

# Seed data: two stored rides (sums are pinned by corpus cases).
SESSIONS = (
    ("alpha", "2026-11-01T08:00:00Z", 3600, 130, 25200.0),
    ("beta", "2026-11-05T08:00:00Z", 2700, 125, 18900.0),
)
LIVE_SESSIONS = {
    "eval-ride": ("ride", [
        {"t": 0.0, "distance_m": 0.0, "speed_mps": 7.0, "hr": 130},
        {"t": 600.0, "distance_m": 700.0, "speed_mps": 6.944, "hr": 142},
    ]),
    "eval-run": ("run", [
        {"t": 0.0, "distance_m": 0.0, "speed_mps": 3.0, "hr": 150},
        {"t": 600.0, "distance_m": 420.0, "speed_mps": 3.0, "hr": 148},
    ]),
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _text(value, maximum=None):
    return (type(value) is str and bool(value.strip())
            and (maximum is None or len(value) <= maximum)
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def validate_corpus(corpus):
    """Validate every case before any app is built; return the original list."""
    _require(type(corpus) is list and bool(corpus), "corpus must be a non-empty list")
    seen = set()
    for index, case in enumerate(corpus):
        prefix = f"case[{index}]"
        _require(type(case) is dict, f"{prefix}: case must be an object")
        _require(REQUIRED <= case.keys(), f"{prefix}: missing required fields")
        _require(not (case.keys() - REQUIRED - OPTIONAL), f"{prefix}: unknown case fields")
        identifier = case["id"]
        _require(type(identifier) is str and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier),
                 f"{prefix}: id must be a kebab-case string")
        _require(identifier not in seen, f"{prefix}: duplicate id {identifier}")
        seen.add(identifier)
        _require(_text(case["question"], 200), f"{prefix}: question must be 1..200 printable characters")
        _require(_text(case["note"]), f"{prefix}: note must be a non-empty string")
        _require(case["expect"] in ("ok", "fallback"), f"{prefix}: expect must be ok or fallback")
        if "lane" in case:
            _require(case["lane"] in LANES, f"{prefix}: lane must be one of {LANES}")
        if "live_id" in case:
            _require(type(case["live_id"]) is str
                     and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", case["live_id"]),
                     f"{prefix}: invalid live_id")
        if "canned" in case:
            _require(_text(case["canned"], 300), f"{prefix}: invalid canned speech")
        if "weather" in case:
            _require(type(case["weather"]) is dict, f"{prefix}: weather must be an object")
        if "nav" in case:
            _require(type(case["nav"]) is dict, f"{prefix}: nav must be an object")
        if "plan" in case:
            _require(type(case["plan"]) is list
                     and all(type(call) is dict and set(call) == {"name", "args"}
                             for call in case["plan"]),
                     f"{prefix}: plan must be a list of name/args calls")
        for field in ("accept_any", "reject_any"):
            if field in case:
                values = case[field]
                _require(type(values) is list and all(_text(v) for v in values),
                         f"{prefix}: {field} must be a list of non-empty strings")
    return corpus


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_corpus(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
        return validate_corpus(json.loads(text, object_pairs_hook=_unique_object))
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        print(f"Invalid corpus: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def _session_file(identifier, started, duration, avg_hr, distance_m):
    from datetime import datetime, timedelta, timezone
    begin = datetime.fromisoformat(started.replace("Z", "+00:00"))
    ended = (begin + timedelta(seconds=duration)).astimezone(timezone.utc)
    ended_at = ended.isoformat().replace("+00:00", "Z")
    avg_speed = distance_m / duration
    return {"schema_version": "luma-ride-memory/3", "id": identifier, "started_at": started,
            "ended_at": ended_at, "samples_hz": 1,
            "series": [{"t": 0, "hr": avg_hr}, {"t": duration, "hr": avg_hr}],
            "summary": {"duration_s": duration, "avg_hr": avg_hr, "max_hr": avg_hr,
                        "time_in_zone_s": None, "zone_target": None, "gaps": 0, "alerts": 0,
                        "avg_cadence": None, "avg_power": None, "max_power": None,
                        "distance_m": distance_m, "avg_speed_mps": avg_speed,
                        "max_speed_mps": avg_speed + 2.0},
            "coach_notes": [], "persona_version": "ride-persona/19", "source": "ble_gatt_0x180d"}


def make_transport(cases):
    """Replay canned speech keyed on which case question the request carries.

    Planner requests (marked "task": "plan") get the case's canned tool plan
    instead of a speech — the agent lane's two calls share one transport."""
    by_question = {case["question"]: case.get("canned", HONEST_DEFAULT) for case in cases}
    plan_by_question = {case["question"]: case.get("plan", []) for case in cases}
    # The planner marker rides inside the nested JSON message content, so the
    # outer serialization escapes its quotes: look for both spellings.
    PLAN_MARKERS = ('"task": "plan"', '"task":"plan"',
                    '\\"task\\": \\"plan\\"', '\\"task\\":\\"plan\\"')

    def http_post(request, timeout):
        body = request.data.decode("utf-8") if isinstance(request.data, bytes) else str(request.data)
        for question in by_question:
            if question in body:
                if any(marker in body for marker in PLAN_MARKERS):
                    content = json.dumps({"tools": plan_by_question[question]}, ensure_ascii=False)
                else:
                    content = json.dumps({"speech": by_question[question]}, ensure_ascii=False)
                envelope = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
                return 200, {"Content-Type": "application/json"}, json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        content = json.dumps({"speech": HONEST_DEFAULT}, ensure_ascii=False)
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        return 200, {"Content-Type": "application/json"}, json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    return http_post


def build_app(cases, tmp_root, *, real_transport=False):
    """App with seeded ride history and live sessions; transport is the only mock.

    real_transport=True keeps the App's own network transport — used by model
    bakeoff runs against the live upstream, never by the offline golden corpus."""
    store = Mock()
    store.path = Path(tmp_root) / "coach.db"
    app = coach_server.App(store=store, llm_post=None if real_transport else make_transport(cases))
    ride_dir = Path(tmp_root) / "data" / "ride_sessions"
    ride_dir.mkdir(parents=True)
    app._ride_dir = ride_dir
    for identifier, started, duration, avg_hr, distance_m in SESSIONS:
        (ride_dir / f"{identifier}.json").write_text(
            json.dumps(_session_file(identifier, started, duration, avg_hr, distance_m),
                       ensure_ascii=False), encoding="utf-8")
    for live_id, (activity, points) in LIVE_SESSIONS.items():
        for point in points:
            app._live_ride({"live_id": live_id, "activity": activity, "point": dict(point)})
    return app


def judge_case(case, result):
    status = result["speech_status"]
    speech = result.get("speech") or ""
    outcome = "ok" if status == "ok" else "fallback"
    reasons = []
    if outcome != case["expect"]:
        reasons.append(f"expected {case['expect']}, got {outcome} ({status})")
    if case["expect"] == "ok" and case.get("accept_any"):
        if not any(part in speech for part in case["accept_any"]):
            reasons.append(f"speech did not match accept_any: {speech!r}")
    forbidden = [part for part in case.get("reject_any", []) if part in speech]
    if forbidden:
        reasons.append(f"speech matched reject_any: {forbidden}")
    if "lane" in case and result.get("lane") != case["lane"]:
        reasons.append(f"expected lane {case['lane']}, got {result.get('lane')}")
    return {"id": case["id"], "lane": result.get("lane"), "status": status,
            "speech": speech, "passed": not reasons, "reasons": reasons}


def run_evaluation(cases):
    """Run all cases through the real door; return a JSON-serializable report."""
    validate_corpus(cases)
    previous = os.environ.get("DASHSCOPE_API_KEY")
    os.environ["DASHSCOPE_API_KEY"] = "offline-eval-key"
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix="eval-door-") as tmp_root:
            app = build_app(cases, tmp_root)
            for case in cases:
                payload = {"question": case["question"]}
                if "live_id" in case:
                    payload["live_id"] = case["live_id"]
                if "weather" in case:
                    payload["weather"] = case["weather"]
                if "nav" in case:
                    payload["nav"] = case["nav"]
                result = app._chat_ride(payload)
                row = judge_case(case, result)
                rows.append(row)
                print(f"{row['id']}\t{row['lane']}\t{row['status']}\t{'pass' if row['passed'] else 'fail'}")
    finally:
        if previous is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = previous
    summary = {"pass": sum(row["passed"] for row in rows), "total": len(rows),
               "by_lane": {lane: sum(row["lane"] == lane for row in rows) for lane in LANES}}
    summary["pass_rate"] = summary["pass"] / summary["total"]
    print(f"door: {summary['pass']}/{summary['total']} pass ({summary['pass_rate']:.0%}), "
          f"lanes live={summary['by_lane']['live']} personal={summary['by_lane']['personal']} "
          f"knowledge={summary['by_lane']['knowledge']} general={summary['by_lane']['general']}")
    return {"provider": "door", "summary": summary, "cases": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    args = parser.parse_args(argv)
    report = run_evaluation(load_corpus(args.corpus))
    return 0 if report["summary"]["pass"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
