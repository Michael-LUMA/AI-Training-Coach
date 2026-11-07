"""Offline Creem webhook skeleton: signed bytes, atomic deduplication, ordered state.

中文摘要：只验证并记录回调，不创建支付、不授予权限；原始正文与密钥不落库/日志。
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import math
from pathlib import Path
import re
import sqlite3
from action_cards import require

STATES = {'checkout.completed': 'completed', 'subscription.active': 'active',
          'subscription.paid': 'paid', 'subscription.canceled': 'canceled',
          'subscription.expired': 'expired', 'subscription.past_due': 'past_due',
          'refund.created': 'refunded'}
RANK = {'completed': 0, 'active': 1, 'paid': 2, 'past_due': 3,
        'canceled': 4, 'expired': 5, 'refunded': 6}


def verify(raw_body: bytes, signature: str, secret: str) -> bool:
    """Compare HMAC-SHA256 hex for the exact received bytes; fail closed.

    Args: raw_body: Original bytes. signature: 64 hex characters. secret: Secret.
    Returns: True only for a matching signature and a nonblank secret.
    """
    if (not isinstance(raw_body, bytes) or not isinstance(signature, str)
            or re.fullmatch(r'[0-9a-fA-F]{64}', signature) is None
            or not isinstance(secret, str) or not secret.strip()):
        return False
    expected = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def _text(value, limit=254):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def _timestamp(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)):
            seconds = float(value)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError
            if seconds >= 100000000000:  # Creem epoch milliseconds, also accept seconds.
                seconds /= 1000
            dt = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            if not isinstance(value, str) or len(value) > 64:
                raise ValueError
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError
        stamp = int(dt.timestamp() * 1000000)
        if not 0 <= stamp <= 253402300799999999:
            raise ValueError
        return stamp
    except (ValueError, TypeError, OverflowError, OSError):
        require(False, 'Invalid event created_at', 'invalid_billing_event')


class BillingStore:
    """Dedicated local metadata only; not a multi-user entitlement system."""

    def __init__(self, path, *, credential_path=None):
        self.path = Path(path)
        self.credential_path = Path(credential_path) if credential_path is not None else None
        self._separate()

    def _separate(self):
        if self.credential_path is not None:
            same = self.path.resolve() == self.credential_path.resolve()
            if self.path.exists() and self.credential_path.exists():
                same = same or self.path.samefile(self.credential_path)
            if same:
                raise ValueError('Billing and credential databases must be separate')

    def handle(self, event: dict) -> dict:
        """Atomically record an event once and update only newer subscription state.

        Args: event: id, eventType/event_type, timezone-aware/epoch created_at, object.
        Returns: received acknowledgement; duplicate/unknown events are accepted.
        Raises: ActionError for invalid envelope; sqlite3/OSError for storage errors.
        Unknown types and events without a subscription identity never grant access.
        Equal timestamps use deterministic state severity then event id, not arrival.
        """
        require(type(event) is dict, 'Expected event object', 'invalid_billing_event')
        kind = event.get('eventType', event.get('event_type'))
        require(_text(event.get('id'), 200) and _text(kind, 100), 'Invalid event identity', 'invalid_billing_event')
        require('eventType' not in event or 'event_type' not in event or event['eventType'] == event['event_type'], 'Conflicting event types', 'invalid_billing_event')
        stamp = _timestamp(event.get('created_at'))
        obj = event.get('object')
        require(type(obj) is dict, 'Expected event object payload', 'invalid_billing_event')
        sub = obj.get('subscription')
        sub_id = sub.get('id') if isinstance(sub, dict) else sub
        if kind.startswith('subscription.'):
            sub_id = obj.get('id')
        sub_id = sub_id if _text(sub_id, 200) else None
        customer = obj.get('customer')
        if not isinstance(customer, dict) and isinstance(sub, dict):
            customer = sub.get('customer')
        email = customer.get('email') if isinstance(customer, dict) else obj.get('email')
        email = email.lower() if _text(email) and '@' in email else None
        state = STATES.get(kind)
        self._separate()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            self.path.chmod(0o600)
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created_at INTEGER NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS subscriptions (subscription_id TEXT PRIMARY KEY, email TEXT, status TEXT NOT NULL, updated_at INTEGER NOT NULL, state_rank INTEGER NOT NULL, event_id TEXT NOT NULL)')
                inserted = db.execute('INSERT INTO events VALUES (?, ?, ?) ON CONFLICT(event_id) DO NOTHING', (event['id'], kind, stamp)).rowcount
                if inserted and sub_id and state:
                    db.execute('''INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(subscription_id) DO UPDATE SET
                        email=COALESCE(excluded.email, subscriptions.email), status=excluded.status,
                        updated_at=excluded.updated_at, state_rank=excluded.state_rank, event_id=excluded.event_id
                        WHERE (excluded.updated_at, excluded.state_rank, excluded.event_id) >
                              (subscriptions.updated_at, subscriptions.state_rank, subscriptions.event_id)''',
                        (sub_id, email, state, stamp, RANK[state], event['id']))
        return {'received': True}

    def current_status(self) -> str:
        """Read latest local subscription status without creating a DB or exposing email.

        No account identity is available in this single-user pilot; this is display
        metadata, never authorization. Empty/missing store returns 'none'.
        """
        self._separate()
        if not self.path.exists():
            return 'none'
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            row = db.execute('SELECT status FROM subscriptions ORDER BY updated_at DESC, state_rank DESC, event_id DESC LIMIT 1').fetchone()
        return row[0] if row else 'none'
