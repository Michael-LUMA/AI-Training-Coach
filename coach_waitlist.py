"""Local opt-in waitlist, separate from credentials; no email delivery.

中文摘要：严格校验邮箱，独立 SQLite 幂等保存；端面不披露首次/重复注册状态。
"""
from pathlib import Path
from contextlib import closing
import re
import sqlite3

from action_cards import require


def normalize_email(value: str) -> str:
    """Validate a conservative ASCII mailbox and return its lowercase identity.

    Args:
        value: Unpadded address, at most 254 characters; local part at most 64.
    Returns:
        Lowercase address, with dot-atom local part and a public-style DNS domain.
    Raises:
        ActionError: Invalid, quoted, internationalized or single-label address.
    """
    valid = isinstance(value, str) and 3 <= len(value) <= 254 and value.isascii()
    require(valid, 'Enter a valid ASCII email address', 'invalid_email')
    parts = value.split('@')
    require(len(parts) == 2, 'Enter a valid ASCII email address', 'invalid_email')
    local, domain = parts
    labels = domain.split('.')
    valid = (1 <= len(local) <= 64 and not local.startswith('.') and not local.endswith('.')
             and '..' not in local and re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
             and len(labels) >= 2 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels)
             and re.fullmatch(r'[A-Za-z]{2,63}', labels[-1]))
    require(valid, 'Enter a valid ASCII email address', 'invalid_email')
    return value.lower()


class WaitlistStore:
    """Lazy, dedicated SQLite store; insert-only, no membership query API."""

    def __init__(self, path, *, credential_path):
        self.path = Path(path)
        self.credential_path = Path(credential_path)
        self._separate()

    def _separate(self):
        same = self.path.resolve() == self.credential_path.resolve()
        if self.path.exists() and self.credential_path.exists():
            same = same or self.path.samefile(self.credential_path)
        if same:
            raise ValueError('Waitlist and credential databases must be separate')

    def register(self, email: str) -> str:
        """Validate and persist once; return an identical acknowledgement for repeats.

        Raises ActionError for invalid email, ValueError for shared credential
        storage, or sqlite3/OSError for storage failure. No network is used.
        """
        email = normalize_email(email)
        self._separate()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            self.path.chmod(0o600)
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS waitlist (email TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                db.execute('INSERT INTO waitlist(email) VALUES (?) ON CONFLICT(email) DO NOTHING', (email,))
        # Means "now on the list", not "was on the list before this request".
        return 'already_registered'
