"""Anonymous device identity for Mark's future cloud persistence.

This module intentionally does not implement passwords, OAuth, phone login, or
API-key storage.  A caller supplies a stable device_id (normally persisted by
the client); the service issues a credential only for a newly registered device.
Device identifiers cannot recover an existing bearer credential.
"""
from __future__ import annotations

from datetime import datetime, timezone
import secrets
import uuid
from typing import Any

import db


class AuthError(RuntimeError):
    """Base error for anonymous identity operations."""


class InvalidDeviceId(AuthError, ValueError):
    """Raised when a caller supplies an unusable device identifier."""


class DeviceAlreadyRegistered(AuthError):
    """A non-secret device identifier cannot recover an existing credential."""


class InvalidToken(AuthError, ValueError):
    """Raised when a token is missing or is not known to the database."""


class AuthStorageError(AuthError):
    """Raised when an identity operation cannot complete in storage."""


MAX_DEVICE_ID_LENGTH = 200
MAX_TOKEN_LENGTH = 512


def _validate_device_id(device_id: str | None) -> str:
    if device_id is None:
        return str(uuid.uuid4())
    if not isinstance(device_id, str):
        raise InvalidDeviceId("device_id must be a string")
    value = device_id.strip()
    if not value or len(value) > MAX_DEVICE_ID_LENGTH:
        raise InvalidDeviceId("device_id must be 1-200 characters")
    return value


def _validate_token(token: str) -> str:
    if not isinstance(token, str):
        raise InvalidToken("token must be a string")
    value = token.strip()
    if not value or len(value) > MAX_TOKEN_LENGTH:
        raise InvalidToken("token is invalid")
    return value


def _row_value(row: Any, index: int, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def _close_cursor(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            raise AuthStorageError("identity cursor release failed") from None


def register_device(device_id: str | None = None, database_url: str | None = None) -> dict[str, str]:
    """Create a NEW anonymous identity; conflict never returns the old token."""
    stable_device_id = _validate_device_id(device_id)
    conn = db.get_conn(database_url)
    cursor = None
    try:
        cursor = conn.cursor()
        user_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT INTO users (id, device_id, token, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (device_id) DO NOTHING
            RETURNING id, token
            """.strip(),
            (user_id, stable_device_id, token, created_at),
        )
        row = cursor.fetchone()
        if row is None:
            raise DeviceAlreadyRegistered("Device already registered; existing credential required")
        conn.commit()
        return {
            "user_id": str(_row_value(row, 0, "id")),
            "token": str(_row_value(row, 1, "token")),
        }
    except (db.DatabaseError, AuthError):
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise AuthStorageError("identity registration failed") from exc
    finally:
        try:
            if cursor is not None:
                _close_cursor(cursor)
        finally:
            db.release_conn(conn)


def whoami(token: str, database_url: str | None = None) -> str:
    """Validate a bearer token and return its anonymous user id."""
    value = _validate_token(token)
    conn = db.get_conn(database_url)
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE token = %s", (value,))
        row = cursor.fetchone()
        if row is None:
            raise InvalidToken("token is invalid")
        return str(_row_value(row, 0, "id"))
    except db.DatabaseError:
        raise
    except InvalidToken:
        raise
    except Exception as exc:
        raise AuthStorageError("identity lookup failed") from exc
    finally:
        try:
            if cursor is not None:
                _close_cursor(cursor)
        finally:
            db.release_conn(conn)


