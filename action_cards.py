"""Source-bound action cards. No automatic execution, audio capture or background alarm."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


class ActionError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition, message="字段格式错误", code="invalid_input", status=400):
    if not condition:
        raise ActionError(code, message, status)


def text(value, limit, required=True):
    require(isinstance(value, str) and len(value) <= limit)
    value = value.strip()
    require(not required or bool(value), "必填文字为空")
    require(not any((ord(c) < 32 and c not in "\n\t\r") or 0xD800 <= ord(c) <= 0xDFFF for c in value), "文字包含不支持的控制字符或编码")
    return value


def fields(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "请求字段不匹配")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_ms():
    return int(time.time() * 1000)


def due_time(value):
    if value is None:
        return None
    require(isinstance(value, str) and len(value) <= 40, "提醒时间格式错误")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ActionError("invalid_time", "提醒时间不是有效ISO时间") from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "提醒时间必须有明确时区", "invalid_time")
    require(2000 <= parsed.year <= 2100, "提醒年份不在支持范围", "invalid_time")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def content(value):
    fields(value, ("action", "recipient", "time_text", "due_at"))
    dt = due_time(value["due_at"])
    return {"action": text(value["action"], 300, False),
            "recipient": text(value["recipient"], 100, False),
            "time_text": text(value["time_text"], 150, False),
            "due_at": dt.isoformat() if dt else None}


def candidate(raw, source):
    """JSON mode is syntax only; additionally reject unsupported or invented source fields."""
    require(isinstance(raw, str) and len(raw) <= 6000, "模型输出无效", "invalid_candidate", 502)
    try:
        value = json.loads(raw)
    except ValueError:
        raise ActionError("invalid_candidate", "模型未返回有效JSON；没有采纳", 502) from None
    try:
        fields(value, ("action", "recipient", "time_text", "evidence"))
        result = {k: text(value[k], lim, k in ("action", "evidence"))
                  for k, lim in (("action", 300), ("recipient", 100), ("time_text", 150), ("evidence", 2000))}
        require(result["evidence"] in source, "模型依据不属于原始交代")
        for key in ("recipient", "time_text"):
            require(not result[key] or result[key] in result["evidence"], "模型对象或时间不在原文依据中")
        # These checks cannot prove the action paraphrase is semantically correct.
        return result
    except ActionError:
        raise ActionError("invalid_candidate", "候选格式或原文依据不符；没有采纳", 502) from None


class ActionStore:
    MAX_CARDS = 100

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def schema(db):
        db.execute("CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS extraction_attempts (id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(id,revision))")

    @staticmethod
    def id(ident):
        require(isinstance(ident, str) and re.fullmatch(r"[a-f0-9]{32}", ident) is not None, "行动卡ID错误")
        return ident

    @staticmethod
    def expected(value):
        require(type(value) is int and 1 <= value <= 1_000_000, "版本号错误")
        return value

    def read(self, db, ident, expected=None):
        row = db.execute("SELECT revision,body FROM actions WHERE id=?", (ident,)).fetchone()
        require(row is not None, "行动卡不存在或已删除", "not_found", 404)
        if expected is not None:
            require(row["revision"] == expected, "另一页面已修改该卡；未覆盖，请刷新后核对", "revision_conflict", 409)
        return json.loads(row["body"])

    def list(self):
        if not self.path.exists():
            return []
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='actions'").fetchone():
                return []
            return [json.loads(r["body"]) for r in db.execute("SELECT body FROM actions ORDER BY rowid DESC")]

    def get(self, ident):
        self.id(ident)
        require(self.path.exists(), "行动卡不存在", "not_found", 404)
        with self.connect() as db:
            return self.read(db, ident)

    def write(self, db, card):
        db.execute("INSERT INTO actions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body",
                   (card["id"], card["revision"], json.dumps(card, ensure_ascii=False, allow_nan=False)))

    @staticmethod
    def event(card, kind):
        require(len(card["events"]) < 200, "本卡记录已满，请导出并新建", "history_full", 409)
        card["events"].append({"kind": kind, "at": now_ms(), "revision": card["revision"]})

    def create(self, source):
        source = text(source, 2000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.schema(db)
            require(db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] < self.MAX_CARDS,
                    "本机最多100张卡，请导出并删除旧记录", "store_full", 409)
            card = {"schema": "luma-action-card/1", "id": uuid.uuid4().hex, "revision": 1,
                    "source": source, "source_sha256": digest(source), "created_at": now_ms(),
                    "status": "draft", "action": "", "recipient": "", "time_text": "", "due_at": None,
                    "reminder_authorized": False, "candidate": None, "confirmed_at": None,
                    "calendar_export_revision": None, "calendar_may_exist": False, "events": []}
            self.event(card, "created")
            self.write(db, card)
            db.commit()
        return card

    def mutate(self, ident, revision, operation):
        self.id(ident)
        self.expected(revision)
        require(self.path.exists(), "行动卡不存在", "not_found", 404)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            card = self.read(db, ident, revision)
            require(revision < 1_000_000, "版本号已满", "history_full", 409)
            card["revision"] += 1
            operation(card)
            self.write(db, card)
            db.commit()
        return card

    @staticmethod
    def editable(card):
        require(card["status"] in ("draft", "confirmed"), "已完成/取消的卡不可修改，请新建", "invalid_transition", 409)

    def edit(self, ident, revision, values):
        values = content(values)
        def operation(card):
            self.editable(card)
            card.update(values, status="draft", reminder_authorized=False, confirmed_at=None)
            self.event(card, "edited_requires_confirmation")
        return self.mutate(ident, revision, operation)

    def confirm(self, ident, revision, reminder):
        require(type(reminder) is bool)
        def operation(card):
            require(card["status"] == "draft", "仅待确认卡可确认", "invalid_transition", 409)
            require(bool(card["action"].strip()), "请先填写或提取具体事项")
            if reminder:
                dt = due_time(card["due_at"])
                require(dt is not None and dt > datetime.now(timezone.utc), "授权提醒前须选择未来的明确时间", "invalid_time")
            card.update(status="confirmed", reminder_authorized=reminder, confirmed_at=now_ms())
            self.event(card, "user_confirmed")
        return self.mutate(ident, revision, operation)

    def finish(self, ident, revision, state):
        require(state in ("done", "cancelled"))
        def operation(card):
            if state == "done":
                require(card["status"] == "confirmed", "只有用户确认的行动卡可标记完成", "invalid_transition", 409)
            else:
                self.editable(card)
            card.update(status=state, reminder_authorized=False)
            self.event(card, "user_" + state)
        return self.mutate(ident, revision, operation)

    def reserve_extraction(self, ident, revision):
        self.id(ident)
        self.expected(revision)
        require(self.path.exists(), "行动卡不存在", "not_found", 404)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            card = self.read(db, ident, revision)
            require(card["status"] == "draft", "仅待确认卡可提取", "invalid_transition", 409)
            try:
                db.execute("INSERT INTO extraction_attempts VALUES (?,?)", (ident, revision))
            except sqlite3.IntegrityError:
                raise ActionError("already_attempted", "本卡此版本已尝试提取，可能已计费；不重复发送。检查账单后可人工编辑", 409) from None
            db.commit()
        return card

    def apply_candidate(self, ident, revision, extracted, provenance):
        def operation(card):
            require(card["status"] == "draft", "卡状态已改变，未采纳云端结果", "revision_conflict", 409)
            # Revalidate at the storage boundary; dates are never invented or interpreted here.
            checked = candidate(json.dumps(extracted, ensure_ascii=False), card["source"])
            card.update(action=checked["action"], recipient=checked["recipient"], time_text=checked["time_text"],
                        due_at=None, reminder_authorized=False, confirmed_at=None,
                        candidate={"fields": checked, "provenance": provenance})
            self.event(card, "model_candidate_not_confirmed")
        return self.mutate(ident, revision, operation)

    def calendar(self, ident, revision):
        def operation(card):
            require(card["status"] == "confirmed" and card["reminder_authorized"],
                    "须先确认内容并单独授权提醒", "reminder_not_authorized", 409)
            dt = due_time(card["due_at"])
            require(dt is not None and dt > datetime.now(timezone.utc), "提醒时间已过或未设置", "invalid_time")
            card["calendar_may_exist"] = True
            card["calendar_export_revision"] = card["revision"]
            self.event(card, "calendar_file_generated_not_delivery_proof")
        card = self.mutate(ident, revision, operation)
        return card, calendar_file(card)

    def delete(self, ident, revision):
        self.id(ident)
        self.expected(revision)
        require(self.path.exists(), "行动卡不存在", "not_found", 404)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            card = self.read(db, ident, revision)
            db.execute("DELETE FROM actions WHERE id=?", (ident,))
            db.execute("DELETE FROM extraction_attempts WHERE id=?", (ident,))
            db.commit()
        return {"deleted": True, "calendar_may_exist": card["calendar_may_exist"],
                "notice": "只删除本机卡；请另行删除导出的文件与日历事件，云端数据按提供商规则处理"}


def ics_text(value):
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_line(line):
    # RFC5545 75-octet folding, counting continuation's leading space, preserving UTF-8.
    parts, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            parts.append(current)
            current = " "
        current += char
    parts.append(current)
    return "\r\n".join(parts)


def calendar_file(card):
    dt = due_time(card["due_at"])
    require(dt is not None)
    stamp = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    description = "由用户确认；不代表已执行。对象：" + (card["recipient"] or "未指定") + "\n原始交代：" + card["source"] + "\n修改/完成/删除本卡不会自动修改此日历事件。"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LUMA//Action Pilot 1//ZH", "CALSCALE:GREGORIAN",
             "BEGIN:VEVENT", "UID:" + card["id"] + "@luma.local", "SEQUENCE:" + str(card["revision"]),
             "DTSTAMP:" + stamp(datetime.now(timezone.utc)), "DTSTART:" + stamp(dt),
             "DTEND:" + stamp(dt + timedelta(minutes=5)), "SUMMARY:" + ics_text(card["action"]),
             "DESCRIPTION:" + ics_text(description), "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
             "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:PT0S", "DESCRIPTION:" + ics_text(card["action"]),
             "END:VALARM", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"
