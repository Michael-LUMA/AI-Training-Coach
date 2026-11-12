"""User-owned cloud data; no bearer secrets or shared local files in exports.

Every operation locks the authenticated users row. All cloud writers use the
same lock, so a delayed profile/chat write cannot resurrect a deleted account.
"""
from datetime import datetime, timezone
import json
import uuid

import db
from cloud_rides import _transaction, InvalidCloudRide
from ride_notes import validate_note_text, MAX_NOTES


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _notes(cursor, user_id):
    cursor.execute("SELECT id, text, created_at FROM notes WHERE user_id = %s ORDER BY created_at, id", (user_id,))
    return [{"id": row[0], "text": row[1], "created_at": row[2]} for row in cursor.fetchall()]


def list_notes(user_id, database_url=None):
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        return {"notes": _notes(cursor, user_id)}


def add_note(user_id, text, database_url=None):
    try:
        text = validate_note_text(text)
    except ValueError:
        raise ValueError("Invalid note text") from None
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("SELECT COUNT(*) FROM notes WHERE user_id = %s", (user_id,))
        if cursor.fetchone()[0] >= MAX_NOTES:
            raise InvalidCloudRide("Note limit reached; export or delete existing notes first")
        identifier, created = "note-" + uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        cursor.execute("INSERT INTO notes (id, user_id, text, created_at) VALUES (%s, %s, %s, %s)",
                       (identifier, user_id, text, created))
        return {"status": "saved", "id": identifier, "created_at": created}


def delete_note(user_id, identifier, database_url=None):
    if type(identifier) is not str or not 1 <= len(identifier) <= 200:
        raise ValueError("Invalid note id")
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("DELETE FROM notes WHERE user_id = %s AND id = %s RETURNING id", (user_id, identifier))
        return {"deleted": len(cursor.fetchall())}


def export_data(user_id, database_url=None):
    """Full, unpaginated four-category snapshot; excludes token/device ID."""
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("SELECT dataset FROM rides WHERE user_id = %s ORDER BY created_at, id", (user_id,))
        rides = [_json(row[0]) for row in cursor.fetchall()]
        cursor.execute("SELECT profile, sample_count, updated_at FROM profiles WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        profile = None if row is None else {"profile": _json(row[0]), "sample_count": row[1], "updated_at": row[2]}
        cursor.execute("SELECT id, exchange_id, role, content, created_at FROM dialog_turns WHERE user_id = %s ORDER BY created_at, exchange_id, CASE role WHEN 'user' THEN 0 ELSE 1 END, id", (user_id,))
        dialog = [dict(zip(("id", "exchange_id", "role", "content", "created_at"), row)) for row in cursor.fetchall()]
        notes = _notes(cursor, user_id)
        return {"schema_version": "mark-data-export/1", "user_id": user_id,
                "exported_at": datetime.now(timezone.utc).isoformat(), "scope": "this_cloud_identity",
                "local_data_included": False, "rides": rides, "profile": profile, "dialog": dialog, "notes": notes}


def delete_data(user_id, confirm=False, database_url=None):
    """Hard delete all five tables in ONE transaction; never soft-delete."""
    if confirm is not True:
        raise ValueError("Explicit confirm=true required")
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        counts = {}
        for table, key in (("rides", "user_id"), ("profiles", "user_id"),
                           ("dialog_turns", "user_id"), ("notes", "user_id"), ("users", "id")):
            # Identifiers are constants, not client input.
            cursor.execute(f"DELETE FROM {table} WHERE {key} = %s RETURNING {key}", (user_id,))
            counts[table] = len(cursor.fetchall())
        return {"deleted": counts, "total_deleted": sum(counts.values()), "token_revoked": True}
