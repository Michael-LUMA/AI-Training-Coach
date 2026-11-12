"""User-scoped PostgreSQL storage for aggregate-only Mark ride memories.

Callers must authenticate before supplying user_id. Legacy local JSON recording
is deliberately independent: cloud failure is explicit, never a local-success
response disguised as successful synchronization. No API keys or raw telemetry.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import re

import db
from ride_memory import ACTIVITIES, SOURCES, MAX_COACH_NOTES, MAX_NOTE_CHARS

SCHEMA_VERSION = "luma-ride-cloud/1"
MAX_SYNC_BATCH = 20
MAX_LIST_LIMIT = 100
_FIELDS = {"id", "started_at", "ended_at", "summary", "source", "activity", "coach_notes"}
_V1 = {"duration_s", "avg_hr", "max_hr", "time_in_zone_s", "zone_target", "gaps", "alerts"}
_V2 = _V1 | {"avg_cadence", "avg_power", "max_power"}
_V3 = _V2 | {"distance_m", "avg_speed_mps", "max_speed_mps"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z")


class InvalidCloudRide(ValueError):
    """Malformed or privacy-unsafe aggregate payload; no content in errors."""


class CloudRideConflict(ValueError):
    """The authenticated user already stored different data for this ride ID."""


class CloudRideNotFound(LookupError):
    """No such ride in the authenticated user's namespace."""


def _check(condition, message="Invalid cloud ride summary"):
    if not condition:
        raise InvalidCloudRide(message)


def _number(value, maximum=None):
    _check(type(value) in (int, float))
    try:
        valid = math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum)
    except (OverflowError, ValueError):
        valid = False
    _check(valid)
    return value


def _instant(value):
    _check(type(value) is str and _TIME.fullmatch(value) is not None)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidCloudRide("Invalid summary timestamp") from None


def _ride_id(value):
    _check(type(value) is str and _ID.fullmatch(value) is not None, "Invalid ride ID")
    return value


def validate_summary(value):
    """Validate aggregate structure, not sample-derived truth (no samples here).

    Only the seven documented cloud fields are accepted. A complete recording,
    injected user_id, extra metadata or nested raw payload is rejected, not
    silently stripped. This differs from to_cloud_summary, the local projector.
    """
    _check(type(value) is dict and set(value) == _FIELDS)
    _ride_id(value["id"])
    start, end = _instant(value["started_at"]), _instant(value["ended_at"])
    _check(end >= start)
    _check(type(value["source"]) is str and value["source"] in SOURCES)
    _check(type(value["activity"]) is str and value["activity"] in ACTIVITIES)
    summary = value["summary"]
    _check(type(summary) is dict and set(summary) in (_V1, _V2, _V3, _V3 | {"ascent_m"}))
    duration = _number(summary["duration_s"])
    _check(math.isclose(duration, (end - start).total_seconds(), rel_tol=1e-12, abs_tol=1e-6))
    for key in ("gaps", "alerts"):
        _check(type(summary[key]) is int and summary[key] >= 0)
    for avg, maximum, ceiling in (("avg_hr", "max_hr", 65535),
                                   ("avg_power", "max_power", 65535)):
        if avg not in summary:
            continue
        mean, peak = summary[avg], summary[maximum]
        _check((mean is None) == (peak is None))
        if mean is not None:
            _check(_number(mean, ceiling) <= _number(peak, ceiling))
    if summary.get("avg_cadence") is not None:
        _number(summary["avg_cadence"], 255)
    target, zones = summary["zone_target"], summary["time_in_zone_s"]
    if target is None:
        _check(zones is None)
    else:
        _check(type(target) is dict and set(target) == {"lower", "upper"})
        _check(_number(target["lower"], 65535) <= _number(target["upper"], 65535))
        if zones is not None:
            _check(type(zones) is dict and set(zones) == {"below", "within", "above"})
            _check(sum(_number(v, duration) for v in zones.values()) <= duration + 1e-6)
            _check(summary["avg_hr"] is not None or sum(zones.values()) == 0)
    if "distance_m" in summary:
        distance, speed, peak = (summary[k] for k in ("distance_m", "avg_speed_mps", "max_speed_mps"))
        _check((distance is None) == (speed is None) == (peak is None))
        if distance is not None:
            _number(distance)
            _check(_number(speed, 70) <= _number(peak, 70))
            _check(duration > 0 or distance == 0)
            _check(math.isclose(speed, distance / duration if duration else 0,
                                rel_tol=1e-12, abs_tol=1e-6))
    if summary.get("ascent_m") is not None:
        _number(summary["ascent_m"])
    notes = value["coach_notes"]
    _check(type(notes) is list and len(notes) <= MAX_COACH_NOTES)
    _check(all(type(note) is str and 0 < len(note) <= MAX_NOTE_CHARS for note in notes))
    return db.validate_cloud_summary(value)


@contextmanager
def _transaction(database_url=None):
    conn = db.get_conn(database_url)
    cursor = None
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as error:
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(error, (db.DatabaseError, InvalidCloudRide, CloudRideConflict, CloudRideNotFound)):
            raise
        raise db.DatabaseOperationError("Cloud ride storage unavailable") from None
    finally:
        try:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    raise db.DatabaseOperationError("Cloud ride cursor release failed") from None
        finally:
            db.release_conn(conn)


def _stored_summary(row):
    try:
        value = row[0]
        if isinstance(value, str):
            value = json.loads(value)
        return validate_summary(value)
    except (ValueError, TypeError, IndexError, KeyError):
        raise db.DatabaseOperationError("Invalid stored cloud summary") from None


def _key(user_id, ride_id):
    # Existing schema uses one global TEXT primary key; namespacing lets two
    # users independently store the same local ID without changing that schema.
    _check(type(user_id) is str and bool(user_id) and ":" not in user_id, "Invalid user identity")
    return user_id + ":" + _ride_id(ride_id)


def sync(user_id, summaries, database_url=None):
    """Atomically insert one batch; exact duplicates succeed, conflicts roll back.

    ON CONFLICT handles concurrent uploads without overwriting prior records.
    Any failure is raised: no partial acknowledgement and no automatic retry.
    """
    _check(type(summaries) is list and 0 < len(summaries) <= MAX_SYNC_BATCH, "Invalid sync batch")
    records = [validate_summary(value) for value in summaries]
    keys = [_key(user_id, record["id"]) for record in records]
    results = []
    now = datetime.now(timezone.utc).isoformat()
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        for key, record in zip(keys, records):
            raw = json.dumps(record, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
            cursor.execute("""INSERT INTO rides
                (id, user_id, schema_version, created_at, updated_at, dataset)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING RETURNING id""",
                (key, user_id, SCHEMA_VERSION, now, now, raw))
            inserted = cursor.fetchone() is not None
            if not inserted:
                cursor.execute("SELECT dataset FROM rides WHERE id = %s AND user_id = %s", (key, user_id))
                row = cursor.fetchone()
                if row is None or _stored_summary(row) != record:
                    raise CloudRideConflict("Ride ID already contains different summary data")
            results.append({"id": record["id"], "status": "saved" if inserted else "duplicate"})
        if any(row["status"] == "saved" for row in results):
            cursor.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
    if any(row["status"] == "saved" for row in results):
        _refresh_profile(user_id, database_url)
    return {"storage": "postgresql", "results": results,
            "saved": sum(row["status"] == "saved" for row in results),
            "duplicates": sum(row["status"] == "duplicate" for row in results)}


def list_rides(user_id, limit=20, database_url=None):
    _key(user_id, "validation")
    _check(type(limit) is int and 1 <= limit <= MAX_LIST_LIMIT, "Invalid list limit")
    with _transaction(database_url) as cursor:
        cursor.execute("SELECT dataset FROM rides WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
                       (user_id, limit))
        return {"storage": "postgresql", "rides": [_stored_summary(row) for row in cursor.fetchall()]}


def get_ride(user_id, ride_id, database_url=None):
    key = _key(user_id, ride_id)
    with _transaction(database_url) as cursor:
        cursor.execute("SELECT dataset FROM rides WHERE id = %s AND user_id = %s", (key, user_id))
        row = cursor.fetchone()
        if row is None:
            raise CloudRideNotFound("Ride not found")
        return {"storage": "postgresql", "ride": _stored_summary(row)}


def delete_ride(user_id, ride_id, database_url=None):
    key = _key(user_id, ride_id)
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("DELETE FROM rides WHERE id = %s AND user_id = %s RETURNING id", (key, user_id))
        deleted = cursor.fetchone() is not None
        if deleted:
            cursor.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
    if deleted:
        _refresh_profile(user_id, database_url)
    return {"storage": "postgresql", "id": ride_id, "deleted": deleted}


def _refresh_profile(user_id, database_url):
    # Import after module initialization: profile_service reuses this module's
    # summary validator/transaction boundary, never raw sessions or credentials.
    import profile_service
    profile_service.request_refresh(user_id, database_url)
