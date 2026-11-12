"""Small PostgreSQL connection layer for Mark's cloud summary store.

The local Mark service deliberately keeps its existing SQLite credential store and
JSON fallback.  This module is only the opt-in PostgreSQL boundary for future
cloud persistence.  It never reads API credentials and never uploads raw ride
telemetry.
"""
from __future__ import annotations

from copy import deepcopy
import importlib
import os
from threading import RLock
from typing import Any


MIN_CONNECTIONS = 1
MAX_CONNECTIONS = 5
DATABASE_ENV = "DATABASE_URL"

# Cloud data is limited to aggregate ride summaries and bounded dialog text.
# Raw GPS, point series, sensor streams and audio never cross this boundary.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    device_id TEXT UNIQUE NOT NULL,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    phone TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_device ON users(device_id);

CREATE TABLE IF NOT EXISTS rides (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    schema_version TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    dataset JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft',
    consent TEXT,
    feedback INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rides_user ON rides(user_id);

CREATE TABLE IF NOT EXISTS dialog_turns (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exchange_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'mark')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, exchange_id, role)
);
CREATE INDEX IF NOT EXISTS idx_dialog_user ON dialog_turns(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id, created_at);

CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY,
    profile JSONB NOT NULL,
    sample_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
""".strip()

_FORBIDDEN_CLOUD_KEYS = frozenset({
    "series",
    "gps",
    "track",
    "tracks",
    "track_points",
    "coordinates",
    "coordinate",
    "polyline",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "sensor_data",
    "raw_sensor",
    "raw_sensors",
    "raw_telemetry",
    "telemetry_raw",
})


class DatabaseError(RuntimeError):
    """Base error for the PostgreSQL boundary."""


class DatabaseNotConfigured(DatabaseError):
    """Raised when no PostgreSQL connection string was provided."""


class DatabaseDriverUnavailable(DatabaseError):
    """Raised when psycopg2 is not installed in the current environment."""


class DatabaseConnectionError(DatabaseError):
    """Raised when the connection pool cannot be created."""


class DatabaseOperationError(DatabaseError):
    """Raised when schema initialization or another database operation fails."""


class CloudDataError(DatabaseError, ValueError):
    """Raised when raw/private telemetry is about to be sent to the cloud."""


_pool: Any | None = None
_pool_dsn: str | None = None
_pool_driver: Any | None = None
_lock = RLock()


def _resolve_dsn(database_url: str | None = None) -> str:
    """Resolve an explicit DSN, falling back only to DATABASE_URL."""
    dsn = database_url if database_url is not None else os.environ.get(DATABASE_ENV)
    if not isinstance(dsn, str) or not dsn.strip():
        raise DatabaseNotConfigured(
            "PostgreSQL is not configured: pass database_url or set DATABASE_URL"
        )
    if "\x00" in dsn:
        raise DatabaseNotConfigured("PostgreSQL connection string contains an invalid character")
    return dsn.strip()


def _load_driver():
    try:
        return importlib.import_module("psycopg2")
    except (ImportError, ModuleNotFoundError) as exc:
        raise DatabaseDriverUnavailable(
            "PostgreSQL driver is unavailable; install psycopg2-binary before deployment"
        ) from exc


def _pool_class(driver):
    try:
        return driver.pool.SimpleConnectionPool
    except AttributeError:
        try:
            return importlib.import_module("psycopg2.pool").SimpleConnectionPool
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise DatabaseDriverUnavailable(
                "PostgreSQL driver does not provide psycopg2.pool.SimpleConnectionPool"
            ) from exc


def _safe_error(exc: BaseException) -> str:
    """Return a useful error without echoing a password-bearing DSN."""
    text = str(exc).strip()
    return text or exc.__class__.__name__


def get_conn(database_url: str | None = None):
    """Get one connection from a 1--5 connection PostgreSQL pool.

    The returned connection belongs to the pool.  Call :func:`release_conn`
    when finished; callers must not call ``close()`` on it directly.
    """
    global _pool, _pool_dsn, _pool_driver
    dsn = _resolve_dsn(database_url)
    with _lock:
        if _pool is not None and _pool_dsn != dsn:
            _pool.closeall()
            _pool = None
            _pool_dsn = None
            _pool_driver = None

        if _pool is None:
            driver = _load_driver()
            pool_type = _pool_class(driver)
            try:
                _pool = pool_type(MIN_CONNECTIONS, MAX_CONNECTIONS, dsn=dsn)
            except TypeError:
                # A small fake pool and some compatible drivers accept the DSN
                # positionally; psycopg2 accepts both forms.
                try:
                    _pool = pool_type(MIN_CONNECTIONS, MAX_CONNECTIONS, dsn)
                except Exception as exc:  # pragma: no cover - exact driver exception varies
                    raise DatabaseConnectionError(
                        f"PostgreSQL connection failed: {_safe_error(exc)}"
                    ) from exc
            except Exception as exc:  # pragma: no cover - exact driver exception varies
                raise DatabaseConnectionError(
                    f"PostgreSQL connection failed: {_safe_error(exc)}"
                ) from exc
            _pool_dsn = dsn
            _pool_driver = driver

        try:
            return _pool.getconn()
        except Exception as exc:  # pragma: no cover - exact driver exception varies
            raise DatabaseConnectionError(
                f"PostgreSQL connection failed: {_safe_error(exc)}"
            ) from exc


def release_conn(conn) -> None:
    """Return a connection obtained from :func:`get_conn` to the pool."""
    if conn is None:
        return
    with _lock:
        if _pool is None:
            return
        try:
            _pool.putconn(conn)
        except Exception as exc:  # pragma: no cover - exact driver exception varies
            raise DatabaseOperationError(
                f"PostgreSQL connection release failed: {_safe_error(exc)}"
            ) from exc


def init_db(database_url: str | None = None) -> None:
    """Create the cloud summary schema, failing closed on every error."""
    conn = get_conn(database_url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseOperationError(
            f"PostgreSQL schema initialization failed: {_safe_error(exc)}"
        ) from exc
    finally:
        release_conn(conn)


def close() -> None:
    """Close the current pool; safe to call repeatedly."""
    global _pool, _pool_dsn, _pool_driver
    with _lock:
        if _pool is not None:
            try:
                _pool.closeall()
            finally:
                _pool = None
                _pool_dsn = None
                _pool_driver = None


def validate_cloud_summary(dataset: Any) -> dict[str, Any]:
    """Validate and copy an aggregated summary before cloud persistence.

    This is intentionally a boundary check, not the M3 summary builder.  It
    rejects forbidden field names recursively so a future caller cannot
    accidentally upload GPS tracks or raw sensor streams.
    """
    if not isinstance(dataset, dict):
        raise CloudDataError("Cloud dataset must be an object containing an aggregated summary")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise CloudDataError(f"Cloud dataset contains a non-string key at {path or '<root>'}")
                normalized = key.strip().lower().replace("-", "_")
                if normalized in _FORBIDDEN_CLOUD_KEYS:
                    raise CloudDataError(f"Raw telemetry field is not allowed in cloud summary: {path + '.' if path else ''}{key}")
                walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(dataset, "")
    return deepcopy(dataset)


__all__ = [
    "CloudDataError",
    "DatabaseConnectionError",
    "DatabaseDriverUnavailable",
    "DatabaseError",
    "DatabaseNotConfigured",
    "DatabaseOperationError",
    "SCHEMA_SQL",
    "close",
    "get_conn",
    "init_db",
    "release_conn",
    "validate_cloud_summary",
]


def lock_user(cursor, user_id: str) -> None:
    """Serialize a user's summary mutations and derived-cache refreshes.

    Must run inside the caller's transaction, before reading/writing rides or
    profiles. The row lock prevents an older refresh from resurrecting a cache
    after a concurrent deletion. Identity must already have been authenticated.
    """
    if type(user_id) is not str or not 1 <= len(user_id) <= 200:
        raise DatabaseOperationError("Invalid cloud identity")
    cursor.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
    if cursor.fetchone() is None:
        raise DatabaseOperationError("Unknown cloud identity")
