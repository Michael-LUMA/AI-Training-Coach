"""M6: bounded PostgreSQL text history. No audio, model calls or local fallback.

A round is an exchange with a user and Mark message. Chat writes both in one
transaction. Reads only expose the authenticated user's rows; incomplete or
individually deleted exchanges are never injected into model context.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import uuid

import db

MAX_ROUNDS = 1000
MAX_RECENT_ROUNDS = 5
MAX_LIST_MESSAGES = MAX_ROUNDS * 2


class DialogCapacityError(ValueError):
    """Export opportunity required before automatically removing old rounds."""


def _user(user_id):
    if type(user_id) is not str or not 1 <= len(user_id) <= 200 or ":" in user_id:
        raise ValueError("Invalid user identity")


def _id(value):
    if type(value) is not str or not 1 <= len(value) <= 64 or not all(
            c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in value):
        raise ValueError("Invalid dialog identifier")


def _text(role, content):
    if type(role) is not str or role not in ("user", "mark"):
        raise ValueError("Invalid dialog role")
    maximum = 200 if role == "user" else 480
    if (type(content) is not str or not 1 <= len(content) <= maximum or not content.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in content)):
        raise ValueError("Invalid dialog text")


def _clock(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Invalid dialog time") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware dialog time required")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _clock(value).isoformat(timespec="microseconds")


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
        if isinstance(error, (db.DatabaseError, ValueError)):
            raise
        raise db.DatabaseOperationError("Dialog storage unavailable") from None
    finally:
        try:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    raise db.DatabaseOperationError("Dialog cursor release failed") from None
        finally:
            db.release_conn(conn)


def _counts(cursor, user_id):
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT exchange_id) FROM dialog_turns WHERE user_id = %s",
                   (user_id,))
    return cursor.fetchone()


def _consent(value):
    if type(value) is not bool:
        raise ValueError("Explicit boolean pruning confirmation required")


def check_capacity(user_id, *, prune_confirmed=False, database_url=None):
    """Preflight before a model call; atomic save rechecks under a user row lock."""
    _user(user_id)
    _consent(prune_confirmed)
    with _transaction(database_url) as cursor:
        _, rounds = _counts(cursor, user_id)
        if rounds >= MAX_ROUNDS and not prune_confirmed:
            raise DialogCapacityError("Export dialog before confirming automatic cleanup")


def _make_room(cursor, user_id, prune_confirmed):
    _, rounds = _counts(cursor, user_id)
    if rounds < MAX_ROUNDS:
        return 0
    if not prune_confirmed:
        raise DialogCapacityError("Export dialog before confirming automatic cleanup")
    # All writers/deleters lock users first. Delete entire oldest exchanges only.
    cursor.execute("""DELETE FROM dialog_turns WHERE user_id = %s AND exchange_id IN (
        SELECT exchange_id FROM dialog_turns WHERE user_id = %s
        GROUP BY exchange_id ORDER BY MIN(created_at), exchange_id LIMIT %s
    ) RETURNING id""", (user_id, user_id, rounds - MAX_ROUNDS + 1))
    return len(cursor.fetchall())


def _insert(cursor, user_id, exchange_id, role, content, created_at):
    message = {"id": str(uuid.uuid4()), "exchange_id": exchange_id, "role": role,
               "content": content, "created_at": created_at}
    cursor.execute("""INSERT INTO dialog_turns (id, user_id, exchange_id, role, content, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)""",
                   (message["id"], user_id, exchange_id, role, content, created_at))
    return message


def save_exchange(user_id, question, speech, created_at=None, *, prune_confirmed=False,
                  database_url=None):
    """Only the server's successful final answer calls this atomic pair writer."""
    _user(user_id)
    _text("user", question)
    _text("mark", speech)
    _consent(prune_confirmed)
    stamp = _stamp(created_at)
    exchange_id = str(uuid.uuid4())
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        pruned = _make_room(cursor, user_id, prune_confirmed)
        messages = [_insert(cursor, user_id, exchange_id, role, text, stamp)
                    for role, text in (("user", question), ("mark", speech))]
    return {"turns": messages, "pruned_messages": pruned}


def save_turn(user_id, role, content, created_at=None, *, database_url=None):
    """Single-message contract. Mark completes the sole pending user message.

    Production chat uses save_exchange instead, so concurrent answers cannot
    interleave or leave a half-round. Ambiguous standalone writes are rejected.
    """
    _user(user_id)
    _text(role, content)
    stamp = _stamp(created_at)
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("""SELECT u.exchange_id, u.created_at FROM dialog_turns u
            WHERE u.user_id = %s AND u.role = 'user' AND NOT EXISTS (
                SELECT 1 FROM dialog_turns m WHERE m.user_id = u.user_id
                AND m.exchange_id = u.exchange_id AND m.role = 'mark')
            ORDER BY u.created_at DESC, u.id DESC LIMIT 2""", (user_id,))
        pending = cursor.fetchall()
        if role == "user":
            if pending:
                raise ValueError("Complete the pending round first")
            _make_room(cursor, user_id, False)
            exchange_id = str(uuid.uuid4())
        else:
            if len(pending) != 1 or stamp < pending[0][1]:
                raise ValueError("A single earlier user message is required")
            exchange_id = pending[0][0]
        return _insert(cursor, user_id, exchange_id, role, content, stamp)


def get_recent(user_id, n=5, within_hours=1, *, database_url=None, now=None):
    _user(user_id)
    if type(n) is not int or not 1 <= n <= MAX_RECENT_ROUNDS:
        raise ValueError("Invalid recent round count")
    if type(within_hours) is not int or not 1 <= within_hours <= 24:
        raise ValueError("Invalid dialog time window")
    instant = _clock(now)
    lower, upper = _stamp(instant - timedelta(hours=within_hours)), _stamp(instant)
    with _transaction(database_url) as cursor:
        cursor.execute("""SELECT u.content, m.content FROM dialog_turns u
            JOIN dialog_turns m ON m.user_id = u.user_id AND m.exchange_id = u.exchange_id
                AND m.role = 'mark'
            WHERE u.user_id = %s AND u.role = 'user' AND u.created_at >= %s
                AND m.created_at >= %s AND u.created_at <= %s AND m.created_at <= %s
            ORDER BY u.created_at DESC, u.exchange_id DESC LIMIT %s""",
                       (user_id, lower, lower, upper, upper, n))
        rows = cursor.fetchall()
    try:
        result = []
        for question, answer in reversed(rows):
            _text("user", question)
            _text("mark", answer)
            result.append({"q": question, "a": answer})
        return result or None
    except (ValueError, TypeError):
        raise db.DatabaseOperationError("Invalid stored dialog") from None


def list_turns(user_id, limit=100, *, export=False, database_url=None):
    _user(user_id)
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_MESSAGES or type(export) is not bool:
        raise ValueError("Invalid dialog list options")
    if export:
        limit = MAX_LIST_MESSAGES
    with _transaction(database_url) as cursor:
        # A consistent snapshot for full export versus concurrent chat/deletion.
        db.lock_user(cursor, user_id)
        total, rounds = _counts(cursor, user_id)
        cursor.execute("""SELECT id, exchange_id, role, content, created_at FROM dialog_turns
            WHERE user_id = %s ORDER BY created_at DESC, exchange_id DESC, role ASC LIMIT %s""",
                       (user_id, limit))
        rows = cursor.fetchall()
    try:
        messages = []
        for identifier, exchange, role, content, created in reversed(rows):
            _id(identifier)
            _id(exchange)
            _text(role, content)
            messages.append({"id": identifier, "exchange_id": exchange, "role": role,
                             "content": content, "created_at": _stamp(created)})
    except (ValueError, TypeError):
        raise db.DatabaseOperationError("Invalid stored dialog") from None
    return {"turns": messages, "total_messages": total, "total_rounds": rounds,
            "has_more": total > len(messages), "max_rounds": MAX_ROUNDS,
            "export_required": rounds >= MAX_ROUNDS}


def delete_one(user_id, turn_id, *, database_url=None):
    _user(user_id)
    _id(turn_id)
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("DELETE FROM dialog_turns WHERE user_id = %s AND id = %s RETURNING id",
                       (user_id, turn_id))
        return {"deleted": len(cursor.fetchall())}


def delete_all(user_id, *, database_url=None):
    _user(user_id)
    with _transaction(database_url) as cursor:
        db.lock_user(cursor, user_id)
        cursor.execute("DELETE FROM dialog_turns WHERE user_id = %s RETURNING id", (user_id,))
        return {"deleted": len(cursor.fetchall())}
