"""Numeric-only riding baselines derived from validated cloud summaries (M4).

No model calls, API credentials, raw telemetry, ratings or medical inference.
China-first calendar: UTC+08, Monday week start. Baselines use the latest 30
completed, non-simulated rides; totals/week include all eligible history.
A PostgreSQL cache is derived data, never a substitute for the source records.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import heapq
import json
import logging
import math
from queue import Queue
from statistics import median
from threading import Lock, Thread

import cloud_rides
import db

MIN_SAMPLES = 3
BASELINE_LIMIT = 30
FETCH_BATCH = 100
MAX_PENDING_REFRESHES = 64
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
_FIELDS = {"usual_start_hour", "usual_duration_min", "usual_distance_km",
           "avg_hr_baseline", "total_sessions", "total_distance_km", "this_week",
           "sample_counts"}
_WEEK_FIELDS = {"sessions", "distance_km", "duration_min"}
_COUNT_FIELDS = {"baseline", "distance", "hr", "total_distance", "week_distance"}
_log = logging.getLogger(__name__)


def _clock(now):
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Profile time must be timezone-aware")
    return now.astimezone(timezone.utc)


def _start_hour(hours):
    # Hour mode; ties choose the smallest hour, independent of upload order.
    counts = Counter(hours)
    return min(counts, key=lambda hour: (-counts[hour], hour))


def _median(values, scale=1):
    return median(values) / scale if len(values) >= MIN_SAMPLES else None


def build_profile(records, *, now=None):
    """Pure, bounded-memory aggregation of an iterable of summary dictionaries.

    Missing measurements stay None; only recorded HR contributes to its median.
    A positive-duration completed ride is eligible. Run/hike and simulations
    never influence riding baselines. Invalid summaries fail, not get repaired.
    Free-text coach_notes are validated but are never copied into the profile.
    """
    now = _clock(now)
    local_now = now.astimezone(LOCAL_TIMEZONE)
    week_start = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    recent = []
    count = distance_count = week_count = week_distance_count = 0
    distance_total = week_distance = week_duration = 0.0
    for index, value in enumerate(records):
        record = cloud_rides.validate_summary(value)
        start = cloud_rides._instant(record["started_at"])
        end = cloud_rides._instant(record["ended_at"])
        summary = record["summary"]
        if (record["source"] == "simulated" or record["activity"] != "ride"
                or summary["duration_s"] <= 0 or end > now):
            continue
        count += 1
        distance = summary.get("distance_m")
        if distance is not None:
            distance_total += distance
            distance_count += 1
        if start >= week_start:
            week_count += 1
            week_duration += summary["duration_s"]
            if distance is not None:
                week_distance += distance
                week_distance_count += 1
        heapq.heappush(recent, (start, record["id"], index, record))
        if len(recent) > BASELINE_LIMIT:
            heapq.heappop(recent)
    if count < MIN_SAMPLES:
        return None
    summaries = [item[3]["summary"] for item in recent]
    distances = [s["distance_m"] for s in summaries if s.get("distance_m") is not None]
    hrs = [s["avg_hr"] for s in summaries if s["avg_hr"] is not None]
    hours = [item[0].astimezone(LOCAL_TIMEZONE).hour for item in recent]
    return {
        "usual_start_hour": _start_hour(hours),
        "usual_duration_min": _median([s["duration_s"] for s in summaries], 60),
        "usual_distance_km": _median(distances, 1000),
        "avg_hr_baseline": _median(hrs),
        "total_sessions": count,
        "total_distance_km": distance_total / 1000 if distance_count else None,
        "this_week": {"sessions": week_count,
                      "distance_km": week_distance / 1000 if week_distance_count or not week_count else None,
                      "duration_min": week_duration / 60},
        "sample_counts": {"baseline": len(recent), "distance": len(distances), "hr": len(hrs),
                          "total_distance": distance_count, "week_distance": week_distance_count},
    }


def _validate_cached(value, sample_count):
    """Reject corrupt/non-numeric stored profiles rather than feed them to AI."""
    def check(condition):
        if not condition:
            raise db.DatabaseOperationError("Invalid stored numeric profile")

    def number(value, nullable=False):
        if value is None:
            check(nullable)
            return
        check(type(value) in (int, float))
        try:
            check(math.isfinite(value) and value >= 0)
        except (OverflowError, ValueError):
            check(False)

    try:
        if isinstance(value, str):
            value = json.loads(value)
        check(type(value) is dict and set(value) == _FIELDS)
        check(type(sample_count) is int and MIN_SAMPLES <= sample_count <= BASELINE_LIMIT)
        for key in ("usual_start_hour", "usual_duration_min", "usual_distance_km",
                    "avg_hr_baseline", "total_distance_km"):
            number(value[key], key in {"usual_distance_km", "avg_hr_baseline", "total_distance_km"})
        check(type(value["usual_start_hour"]) is int and value["usual_start_hour"] < 24
              and value["usual_duration_min"] > 0)
        check(value["avg_hr_baseline"] is None or value["avg_hr_baseline"] <= 65535)
        check(type(value["total_sessions"]) is int and value["total_sessions"] >= sample_count)
        week, counts = value["this_week"], value["sample_counts"]
        check(type(week) is dict and set(week) == _WEEK_FIELDS)
        check(type(counts) is dict and set(counts) == _COUNT_FIELDS)
        for number_value in counts.values():
            check(type(number_value) is int and number_value >= 0)
        check(counts["baseline"] == sample_count)
        check(counts["distance"] <= sample_count and counts["hr"] <= sample_count)
        check(type(week["sessions"]) is int and 0 <= week["sessions"] <= value["total_sessions"])
        check(counts["total_distance"] <= value["total_sessions"])
        check(counts["week_distance"] <= week["sessions"])
        number(week["distance_km"], True)
        number(week["duration_min"])
        check((value["usual_distance_km"] is None) == (counts["distance"] < MIN_SAMPLES))
        check((value["avg_hr_baseline"] is None) == (counts["hr"] < MIN_SAMPLES))
        check((value["total_distance_km"] is None) == (counts["total_distance"] == 0))
        check((week["distance_km"] is None) == (week["sessions"] > 0 and counts["week_distance"] == 0))
        return value
    except (TypeError, KeyError, ValueError):
        raise db.DatabaseOperationError("Invalid stored numeric profile") from None


def _compute(cursor, user_id, now):
    cursor.execute("SELECT dataset FROM rides WHERE user_id = %s", (user_id,))

    def records():
        while True:
            batch = cursor.fetchmany(FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                yield cloud_rides._stored_summary(row)

    profile = build_profile(records(), now=now)
    if profile is None:
        cursor.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
    else:
        sample_count = profile["sample_counts"]["baseline"]
        _validate_cached(profile, sample_count)
        cursor.execute("""INSERT INTO profiles (user_id, profile, sample_count, updated_at)
            VALUES (%s, %s::jsonb, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET profile = EXCLUDED.profile,
            sample_count = EXCLUDED.sample_count, updated_at = EXCLUDED.updated_at""",
            (user_id, json.dumps(profile, allow_nan=False, separators=(",", ":")),
             sample_count, now.isoformat()))
    return profile


def compute_profile(user_id, database_url=None, *, now=None):
    """Refresh and persist one user's numeric profile; fewer than 3 -> None."""
    now = _clock(now)
    with cloud_rides._transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        return _compute(cursor, user_id, now)


def get_profile(user_id, database_url=None, *, now=None):
    """Read today's valid cache, or compute it. Never use an invalidated cache.

    Day rollover refreshes time-based totals even without another upload. This
    also admits previously future-dated summaries only once they are complete.
    """
    now = _clock(now)
    with cloud_rides._transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("SELECT profile, sample_count, updated_at FROM profiles WHERE user_id = %s",
                       (user_id,))
        row = cursor.fetchone()
        if row is not None:
            profile = _validate_cached(row[0], row[1])
            try:
                updated = cloud_rides._instant(row[2])
            except cloud_rides.InvalidCloudRide:
                raise db.DatabaseOperationError("Invalid stored profile timestamp") from None
            if updated <= now and updated.astimezone(LOCAL_TIMEZONE).date() == now.astimezone(LOCAL_TIMEZONE).date():
                return profile
        return _compute(cursor, user_id, now)


class _RefreshWorker:
    """Single bounded/coalescing background queue, no retries and no payload logs."""
    def __init__(self):
        self.queue = Queue()
        self.pending = set()
        self.lock = Lock()
        self.closed = False
        self.thread = Thread(target=self._run, name="mark-profile-refresh", daemon=True)
        self.thread.start()

    def request(self, user_id, database_url):
        key = (user_id, database_url)
        with self.lock:
            if self.closed:
                return False
            if key in self.pending:
                return True
            if len(self.pending) >= MAX_PENDING_REFRESHES:
                return False  # Durable invalidation remains; next read computes.
            self.pending.add(key)
            self.queue.put_nowait(key)
            return True

    def _run(self):
        while True:
            key = self.queue.get()
            try:
                if key is None:
                    return
                with self.lock:
                    # Allow one new refresh while this one is running. Otherwise
                    # a mutation after our commit could leave a cache missing.
                    self.pending.discard(key)
                try:
                    compute_profile(key[0], key[1])
                except Exception:
                    _log.warning("Profile refresh unavailable; next read will recompute")
            finally:
                self.queue.task_done()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.queue.put_nowait(None)
        self.thread.join()


_worker = None
_worker_lock = Lock()


def request_refresh(user_id, database_url=None):
    """Best-effort derived-cache update, never a ride persistence acknowledgement."""
    global _worker
    with _worker_lock:
        if _worker is None:
            try:
                _worker = _RefreshWorker()
            except RuntimeError:
                _log.warning("Profile refresh unavailable; next read will recompute")
                return False
        return _worker.request(user_id, database_url)


def shutdown():
    """Drain accepted refreshes before the server closes its PostgreSQL pool."""
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.close()
            _worker = None
