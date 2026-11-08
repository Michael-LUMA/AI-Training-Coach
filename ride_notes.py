"""User-voiced notes Mark remembers verbatim — the first brick of long-term memory.

中文摘要：用户明确说「记住」的事，原样存储、原样引用，永不推断、永不改写。
每条 1..140 字符（去控制字符），全库最多 50 条，满了拒绝而不是静默丢最旧的；
单文件原子写，损坏文件不自动修复——如实报错，不猜内容。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "luma-ride-notes/1"
MAX_NOTES = 50
MAX_TEXT_CHARS = 140
MAX_FILE_BYTES = 64 * 1024
_ID_RE = re.compile(r"^note-[0-9a-f]{12}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class NoteError(ValueError):
    """Sanitized rejection with a stable code; never echoes the note text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_note_text(text: Any) -> str:
    if type(text) is not str:
        raise NoteError("invalid_input", "Note text must be a string")
    cleaned = _CONTROL.sub("", text).strip()
    if not 1 <= len(cleaned) <= MAX_TEXT_CHARS:
        raise NoteError("invalid_input", "Note must be 1..140 characters")
    return cleaned


def _checked_entry(entry: Any) -> dict:
    if type(entry) is not dict or set(entry) != {"id", "created_at", "text"}:
        raise NoteError("storage_corrupt", "Stored notes are unreadable")
    if type(entry["id"]) is not str or _ID_RE.fullmatch(entry["id"]) is None:
        raise NoteError("storage_corrupt", "Stored notes are unreadable")
    if type(entry["created_at"]) is not str:
        raise NoteError("storage_corrupt", "Stored notes are unreadable")
    try:
        datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
    except ValueError:
        raise NoteError("storage_corrupt", "Stored notes are unreadable")
    return {"id": entry["id"], "created_at": entry["created_at"],
            "text": validate_note_text(entry["text"])}


def load_notes(path: Path) -> list[dict]:
    """Read the whole store; a corrupt file is an honest error, never a guess."""
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise NoteError("storage_unavailable", "Note storage unavailable")
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeError):
        raise NoteError("storage_corrupt", "Stored notes are unreadable") from None
    if type(raw) is not dict or raw.get("schema_version") != SCHEMA_VERSION \
            or type(raw.get("notes")) is not list or len(raw["notes"]) > MAX_NOTES:
        raise NoteError("storage_corrupt", "Stored notes are unreadable")
    return [_checked_entry(entry) for entry in raw["notes"]]


def save_notes(path: Path, notes: list[dict]) -> None:
    """Atomic write; the directory must already exist (ride storage owns it)."""
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "notes": notes},
                         ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        raise NoteError("note_limit", "Note storage is full")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-notes-",
                                         delete=False) as file:
            temporary = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def add_note(path: Path, text: Any, *, now: datetime | None = None) -> dict:
    """Append one verbatim note; the 51st is refused, never silently dropped."""
    cleaned = validate_note_text(text)
    notes = load_notes(path)
    if len(notes) >= MAX_NOTES:
        raise NoteError("note_limit", "Memory holds 50 notes; delete one first")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entry = {"id": "note-" + uuid.uuid4().hex[:12],
             "created_at": moment.isoformat().replace("+00:00", "Z"),
             "text": cleaned}
    notes.append(entry)
    save_notes(path, notes)
    return entry


def remove_note(path: Path, note_id: Any) -> dict:
    """Delete by id; unknown ids are an honest not_found."""
    if type(note_id) is not str or _ID_RE.fullmatch(note_id) is None:
        raise NoteError("invalid_input", "Invalid note id")
    notes = load_notes(path)
    remaining = [entry for entry in notes if entry["id"] != note_id]
    if len(remaining) == len(notes):
        raise NoteError("not_found", "Note not found")
    save_notes(path, remaining)
    return {"status": "deleted", "id": note_id}
