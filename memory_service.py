"""M5: authenticated aggregate-only memory, no LLM calls or local fallback.

Budget is a conservative UTF-8 byte upper bound for byte-level BPE, not a
character/token estimate. Check both the returned bundle and its builder-facing
projection with the SAME JSON spacing used by builders. Reserve 32 bytes for
joining fields. No strings, units or JSON are ever sliced to fit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import re

import cloud_rides
import dialog_store
import profile_service

MAX_MEMORY_TOKENS = 800
MEMORY_RESERVE = 32
LOCAL_TIMEZONE = timezone(timedelta(hours=8))


class MemoryMatches(list):
    """Server-created history matches with the China calendar, not host TZ."""


class SummarySession:
    """Internal, validated aggregate; never pretend an empty series was recorded."""
    def __init__(self, record):
        self.record = cloud_rides.validate_summary(record)

    def __deepcopy__(self, memo):
        return SummarySession(deepcopy(self.record))


def _now(now):
    return profile_service._clock(now)


def _records(user_id, database_url=None):
    cloud_rides._key(user_id, "validation")
    with cloud_rides._transaction(database_url) as cursor:
        cursor.execute("SELECT dataset FROM rides WHERE user_id = %s", (user_id,))
        records = []
        while True:
            batch = cursor.fetchmany(100)
            if not batch:
                break
            records.extend(cloud_rides._stored_summary(row) for row in batch)
    return records


def _eligible(records, now, activity="ride", before=None, exclude=None):
    result = []
    for value in records:
        record = cloud_rides.validate_summary(value)
        start = cloud_rides._instant(record["started_at"])
        end = cloud_rides._instant(record["ended_at"])
        if (record["id"] == exclude or record["source"] == "simulated"
                or record["activity"] != activity or end > now
                or record["summary"]["duration_s"] <= 0
                or (before is not None and (start >= before or end > before))):
            continue
        result.append(record)
    return sorted(result, key=lambda item: (cloud_rides._instant(item["started_at"]), item["id"]))


def _brief(records, now):
    value = profile_service.build_profile(records, now=now)
    if value is None:
        return None
    return {"sessions": value["total_sessions"],
            "usual_start_hour": value["usual_start_hour"],
            "usual_duration_min": value["usual_duration_min"]}


def _round(value, precision="1"):
    result = Decimal(str(value)).quantize(Decimal(precision), rounding=ROUND_HALF_UP)
    return int(result) if result == result.to_integral() else float(result)


def _aggregates(records, now):
    if not records:
        return None, None
    day = now.astimezone(LOCAL_TIMEZONE).date()
    month = day.replace(day=1)
    monday = day - timedelta(days=day.weekday())

    def group(items):
        distances = [item["summary"].get("distance_m") for item in items]
        # A partial distance must never masquerade as a complete total.
        distance = (_round(sum(distances) / 1000, "0.1")
                    if distances and all(value is not None for value in distances) else None)
        return {"sessions": len(items), "duration_min": _round(sum(
            item["summary"]["duration_s"] for item in items) / 60), "distance_km": distance}

    monthly = group([item for item in records if cloud_rides._instant(
        item["started_at"]).astimezone(LOCAL_TIMEZONE).date() >= month])
    week = group([item for item in records if cloud_rides._instant(
        item["started_at"]).astimezone(LOCAL_TIMEZONE).date() >= monday])
    all_time = group(records)
    return {"sessions_count": all_time["sessions"], "duration_min": all_time["duration_min"],
            "distance_km": all_time["distance_km"], "month_sessions": monthly["sessions"],
            "month_duration_min": monthly["duration_min"],
            "month_distance_km": monthly["distance_km"]}, week


def _recent_summary(record):
    # Keep measured comparison dimensions; the existing budget may drop whole
    # older records, never truncate fields or invent missing measurements.
    keys = ("duration_s", "avg_hr", "avg_speed_mps", "ascent_m")
    return {key: record["summary"][key] for key in keys if key in record["summary"]}


def _match_summary(record):
    keys = ("duration_s", "avg_hr", "distance_m", "ascent_m")
    return {key: record["summary"][key] for key in keys if key in record["summary"]}


def _matches(records, question, now):
    """Deterministic retrieval; no extra paid planner call. None = not searched."""
    day = now.astimezone(LOCAL_TIMEZONE).date()
    subset = records
    if any(word in question for word in ("最长", "最久", "最短")):
        subset = sorted(records, key=lambda item: (item["summary"]["duration_s"],
                        item["started_at"]), reverse="最短" not in question)[0:1]
    else:
        date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", question)
        if date_match:
            try:
                target = datetime.strptime(date_match.group(), "%Y-%m-%d").date()
            except ValueError:
                return None
            start = end = target
        elif "上周" in question:
            end = day - timedelta(days=day.weekday() + 1)
            start = end - timedelta(days=6)
        elif "昨天" in question or "前天" in question:
            start = end = day - timedelta(days=2 if "前天" in question else 1)
        elif "30天" in question or "三十天" in question:
            start, end = day - timedelta(days=29), day
        elif "上次" in question or "最近一次" in question:
            subset = records[-1:]
            start = end = None
        else:
            return None
        if start is not None:
            subset = [item for item in records if start <= cloud_rides._instant(
                item["started_at"]).astimezone(LOCAL_TIMEZONE).date() <= end]
    # Never truncate a range and let the model claim its partial count is total.
    if len(subset) > 7:
        raise ValueError("Selected history exceeds safe context budget")
    return MemoryMatches([{"started_at": cloud_rides._instant(item["started_at"]).astimezone(LOCAL_TIMEZONE).isoformat(),
             "summary": _match_summary(item)} for item in subset])


def _projection(context, lane, current=None):
    # Reuse the exact existing fact validators; this is not a second numeric gate.
    from ride_brain import _facts
    from ride_chat import _check_history, _history_facts, _matched_facts, _profile_facts, _totals_facts, _week_facts
    if lane == "live":
        return {key: value for key, value in (
            ("previous", context.get("previous_summary")),
            ("profile", context.get("profile_brief"))) if value is not None}
    if lane == "review":
        complete, _ = _facts(current, context.get("previous_summary"), context.get("recent_summaries"))
        base, _ = _facts(current, None)
        # display contains memory-derived conversions/differences as well.
        return {key: value for key, value in complete.items() if base.get(key) != value}
    facts, _ = _history_facts(context.get("recent_summaries"), None, None)
    for key, validator in (("matched_summaries", _matched_facts), ("profile", _profile_facts),
                           ("totals", _totals_facts), ("week", _week_facts)):
        if context.get(key) is not None:
            facts[key] = validator(context[key])[0]
    if context.get("recent_dialog") is not None:
        facts["recent_dialog"] = _check_history(context["recent_dialog"])
    if current is not None:
        facts["selected_session"], _ = _facts(current, None)
    return facts


def memory_upper_bound(context, lane, current=None):
    """Upper bound of added memory text, NOT of system/current question tokens."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return max(size(context), size(_projection(context, lane, current))) + MEMORY_RESERVE


def _budget(context, lane, current=None):
    # Drop whole optional groups. Preserve complete totals and complete matches.
    while memory_upper_bound(context, lane, current) > MAX_MEMORY_TOKENS:
        recent = context.get("recent_summaries")
        if recent:
            context["recent_summaries"] = recent[1:]
            continue
        dialog = context.get("recent_dialog")
        if dialog:
            context["recent_dialog"] = dialog[1:]
            continue
        removed = False
        for key in ("week", "profile", "profile_brief", "previous_summary", "totals"):
            if context.get(key) is not None:
                context[key] = None
                removed = True
                break
        if not removed:
            raise ValueError("Selected cloud memory exceeds safe context budget")
    return context


def _comparison(user_id, current_session, database_url, now, lane):
    now = _now(now)
    if isinstance(current_session, SummarySession):
        current = cloud_rides.validate_summary(current_session.record)
    elif lane == "review":
        from ride_memory import validate_session
        current = validate_session(current_session)
    else:
        # Live descriptor is NOT a fake session: it has no summary/series.
        current = current_session
    activity = current.get("activity", "ride")
    if activity not in ("ride", "hike", "run"):
        raise ValueError("Invalid activity")
    before = cloud_rides._instant(current["started_at"])
    records = _eligible(_records(user_id, database_url), now, activity, before, current.get("id"))
    context = {"previous_summary": records[-1]["summary"] if records else None,
               "profile_brief": _brief(records, now) if activity == "ride" else None}
    if lane == "review":
        context["recent_summaries"] = [_recent_summary(item) for item in records[-3:]]
    return _budget(context, lane, current_session if lane == "review" else None)


def get_context_for_live(user_id, current_session, database_url=None, *, now=None):
    return _comparison(user_id, current_session, database_url, now, "live")


def get_context_for_review(user_id, current_session, database_url=None, *, now=None):
    return _comparison(user_id, current_session, database_url, now, "review")


def get_context_for_chat(user_id, question, database_url=None, *, now=None, session_id=None,
                         selected_session=None):
    now = _now(now)
    if type(question) is not str or not 1 <= len(question) <= 200:
        raise ValueError("Invalid question")
    records = _records(user_id, database_url)
    selected = None
    if session_id is not None:
        selected = next((item for item in records if item["id"] == session_id), None)
        if selected is None:
            raise cloud_rides.CloudRideNotFound("Ride not found")
        if cloud_rides._instant(selected["ended_at"]) > now:
            raise ValueError("Selected ride is not complete")
        if selected_session is not None:
            if (not isinstance(selected_session, SummarySession)
                    or cloud_rides.validate_summary(selected_session.record) != selected):
                raise ValueError("Selected cloud ride changed during retrieval")
    activity = (selected["activity"] if selected is not None else
                "run" if "跑步" in question else "hike" if "徒步" in question else "ride")
    eligible = _eligible(records, now, activity)
    totals, week = _aggregates(eligible, now)
    context = {"matched_summaries": _matches(eligible, question, now),
               "recent_summaries": [_recent_summary(item) for item in eligible
                                     if selected is None or item["id"] != selected["id"]][-3:],
               "recent_dialog": dialog_store.get_recent(user_id, n=5, within_hours=1,
                                                        database_url=database_url, now=now), "user_notes": [],
               "profile": _brief(eligible, now) if activity == "ride" else None,
               "totals": totals, "week": week}
    current = SummarySession(selected) if selected is not None else None
    return _budget(context, "chat", current)
