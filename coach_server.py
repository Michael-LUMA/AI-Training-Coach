"""Loopback-first English coach MVP. Remote binding is explicit, not public authentication.

中文摘要：独立 SQLite 加密保存密钥（无可用后端则拒绝），严格同源和 token；复盘仅按用户请求生成。
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import ipaddress
import os
import warnings
import re
import secrets
import sqlite3
import tempfile
import time
from collections import deque
from threading import Lock
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from action_cards import ActionError, require
from coach_icu_client import IntervalsClient, ICUClientError
from coach_icu import normalize_activity
from coach_demo import build_demo_review
from coach_keystore import KeyStore
from coach_review_en import build_review
from llm_review import build_llm_review
from coach_waitlist import WaitlistStore
from coach_weekly import build_weekly_summary
from coach_billing import BillingStore, verify as verify_billing
from ride_gpx import GPXError
from ride_import import ImportBuildError, build_import_session
from ride_review import ReviewError
from ride_memory import validate_session, RideMemoryError
from ride_brain import build_ride_review
import llm_router
from ride_chat import NAV_QUESTION_MARKERS, build_ride_chat
from ride_agent import MAX_MATCHES, SEARCH_HINTS, plan_searches, search_sessions
from ride_knowledge import build_knowledge_answer, load_cards
from ride_general import build_general_answer
from ride_asr import MAX_AUDIO_BYTES, transcribe_wav
from ride_live import LIVE_TEMPLATE, LiveSession, build_live_answer, build_live_speech
from ride_nav import plan_route, _http_get as _nav_http_get
from ride_notes import NoteError, add_note, load_notes, remove_note
from ride_tts import synthesize as tts_synthesize
from ride_weather import fetch_weather
from ride_weekly import build_ride_weekly, aggregate_ride_weeks, split_week_sessions, weekly_template
from ride_server import strict_json, dumps

BRAND_NAME = "AI Ride Coach"
SUPPORT_EMAIL = "support@example.invalid"  # Placeholder; replace before public launch.
ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / ".runtime/coach/coach.sqlite3"
MAX_BODY_BYTES = 8192
MAX_RIDE_BODY_BYTES = 2 * 1024 * 1024
MAX_IMPORT_BODY_BYTES = 6 * 1024 * 1024  # 5 MB file text plus JSON envelope.
MAX_IMPORT_BATCH = 20  # One request carries at most this many file texts.
RIDE_POST = "/api/coach/ride/session"
RIDE_LIST = "/api/coach/ride/sessions"
RIDE_IMPORT = "/api/coach/ride/import"
RIDE_REVIEW = "/api/coach/ride/review"
RIDE_WEEKLY = "/api/coach/ride/weekly"
RIDE_ROUTE = "/api/coach/ride/route"
RIDE_WEATHER = "/api/coach/ride/weather"
RIDE_NOTES = "/api/coach/ride/notes"
RIDE_NOTE = "/api/coach/ride/note"
RIDE_NOTE_DELETE = "/api/coach/ride/note/delete"
RIDE_CHAT = "/api/coach/ride/chat"
RIDE_LIVE = "/api/coach/ride/live"
RIDE_SPEAK = "/api/coach/ride/speak"
RIDE_ASR = "/api/coach/ride/asr"


def _usage_profile(entries):
    """Median start hour/duration over (when, summary) pairs of real sessions.

    Below three sessions Mark stays silent about "usual" rather than guessing;
    the caller decides which sessions are real and in scope. Local-clock hours.
    """
    if len(entries) < 3:
        return None
    hours = sorted(when.astimezone().hour for when, _ in entries)
    durations = sorted(summary["duration_s"] / 60 for _, summary in entries
                       if type(summary.get("duration_s")) in (int, float) and summary["duration_s"] > 0)
    median_min = durations[(len(durations) - 1) // 2] if durations else None
    return {"sessions": len(entries),
            "usual_start_hour": hours[(len(entries) - 1) // 2],
            "usual_duration_min": (None if median_min is None else
                                   int(median_min) if median_min == int(median_min)
                                   else round(median_min, 1))}
DEFAULT_PORT = 8794
_PERSONAL_MARKERS = ("我", "这次", "上次", "最近", "今天", "昨天",
                     "这周", "本周", "这个月", "本月", "累计")
# 记录词：自称词必须与记录词同时出现才进个人记录道——否则「今天星期几」
# 「帮我写周报」这类开放问题会被误路由，得到「我没记到」的傻子回答。
_RECORD_WORDS = ("心率", "踏频", "功率", "配速", "速度", "均速", "里程", "公里",
                 "千米", "骑", "跑步", "跑了", "徒步", "训练", "运动", "消耗",
                 "卡路里", "爬坡", "爬升", "海拔", "天气", "路线", "记录",
                 "状态", "几次", "多久", "多远", "多快", "多长", "出门",
                 "膝盖", "旧伤", "受伤")
# 求教词：「怎么练/是什么/为什么」是知识问题，不是记录问题。
_ADVICE_MARKERS = ("怎么练", "怎么提高", "如何", "是什么", "为什么", "什么意思", "区别", "教程")
_LIVE_SILENCE_S = 60.0      # 主动播报全局静默期：克制即智能
_LIVE_FRESH_S = 30.0        # Three missed 10-second heartbeats make current facts unavailable.
_CONV_TTL_S = 1800.0        # 对话记忆 30 分钟无活动自焚，只在内存，不落盘
_CONV_MAX_TURNS = 3         # 只带最近 3 轮上下文：够接追问，不长尾泄露
_LIVE_IDLE_S = 3 * 3600.0   # 实时会话空闲回收
_LIVE_MAX_SESSIONS = 200    # 进程内存实时会话上限
_LIVE_URGENT_TYPES = frozenset({"hr_high", "hr_low"})  # 安全事件直通，不受静默期约束
_LIVE_MARKERS = ("现在", "当前", "这会", "此刻", "实时", "刚刚")  # 问「当下」进实时道


class CoachStore:
    """Dedicated encrypted credential DB; not a multi-user vault.

    Legacy plaintext rows are rejected; reconnect using a fresh database.
    """

    def __init__(self, path=None, *, keystore=None):
        path = os.environ.get("LUMA_COACH_DB", str(DEFAULT_DB)) if path is None else path
        if not str(path).strip() or "\x00" in str(path):
            raise ValueError("LUMA_COACH_DB must be a non-empty filesystem path")
        self.path = Path(path)
        self.keystore = keystore if keystore is not None else KeyStore()

    @property
    def encryption_status(self):
        return self.keystore.status

    def save_key(self, key):
        encrypted = self.keystore.encrypt(key)  # Fail before creating/writing the DB.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            self.path.chmod(0o600)  # Windows still relies on inherited account ACLs.
            db.execute("PRAGMA secure_delete=ON")
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS credential (id INTEGER PRIMARY KEY CHECK(id=1), api_key TEXT NOT NULL)")
                db.execute("INSERT OR REPLACE INTO credential VALUES (1, ?)", (encrypted,))

    def get_key(self):
        if not self.path.exists():
            return None
        # Read-only: querying connection state must not create an empty store.
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            row = db.execute("SELECT api_key FROM credential WHERE id=1").fetchone()
            return self.keystore.decrypt(row[0]) if row else None


def _masked(key):
    # Never echo the whole key, even if it has four characters or fewer.
    return "****" + (key[-4:] if len(key) > 4 else "") if key else None


def _page(filename="coach.html"):
    text = (ROOT / "static" / filename).read_text(encoding="utf-8-sig")
    def hashes(tag):
        return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(part.encode()).digest()).decode() + "'"
                        for part in re.findall(r"<" + tag + r">(.*?)</" + tag + r">", text, re.S))
    csp = ("default-src 'none'; script-src " + hashes("script") + "; style-src " + hashes("style") +
           "; manifest-src 'self'; img-src 'self'; connect-src 'self'"
           "; media-src https://*.aliyuncs.com http://*.aliyuncs.com"
           "; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    return text.encode("utf-8"), csp


def _public_origin():
    """Optional deployed origin, e.g. https://app.onrender.com (bare origin only).

    When set, Host/Origin checks target this origin instead of loopback;
    the loopback defense-in-depth then applies to the public hostname.
    """
    raw = os.environ.get("LUMA_COACH_PUBLIC_ORIGIN", "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (parsed.scheme not in ("https", "http") or not parsed.netloc or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment or
            parsed.username or parsed.password or len(raw) > 253):
        raise ValueError("LUMA_COACH_PUBLIC_ORIGIN must be a bare origin like https://app.example.com")
    return parsed.scheme + "://" + parsed.netloc


def _build_rule_snapshot(record):
    """Content-address a stateless rule snapshot, identically for L1 and L2.

    The shared engine creates editable entities with random IDs/wall-clock times.
    HTTP reviews here are not persisted entities: their creation/update timestamps
    are unknown (null), not invented from the ride date. All factual content stays
    untouched. Hashing the complete versioned snapshot also covers rule/context
    changes; no activity cache, shared mutable review, or user credential is used.
    """
    review = build_review(record)
    review.update({"id": None, "created_at": None, "updated_at": None})
    canonical = json.dumps(review, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("utf-8")
    review["id"] = hashlib.sha256(b"coach-rule-snapshot/1\0" + canonical).hexdigest()[:32]
    return review


class App:
    """Transport-injectable API; dispatch is testable without binding sockets."""

    def __init__(self, store=None, *, fetch=None, today=None, llm_post=None, nav_get=None,
                 ws_dial=None):
        self.store = store if store is not None else CoachStore()
        self._billing = None
        self._waitlist = None
        self.fetch = fetch
        self.llm_post = llm_post
        # Production default mirrors the llm_post=None pattern: the module's
        # own no-redirect transport. Tests inject strict fakes instead.
        self.nav_get = nav_get if nav_get is not None else _nav_http_get
        self.ws_dial = ws_dial
        self.today = today if today is not None else date.today
        self.token = secrets.token_urlsafe(32)
        # Credential-adjacent ephemeral data; tests/custom DBs stay isolated.
        self._ride_dir = None
        self._ride_lock = Lock()
        self._ride_requests = {"GET": deque(), "POST": deque()}
        self._knowledge_cards = None
        self._live = {}  # live_id -> [LiveSession, last_call_mono, last_seen_mono]
        self._live_lock = Lock()
        # Dialog memory for follow-up questions: memory-only, TTL-bounded,
        # never written to disk. conv_id -> [turns, last_seen_mono].
        self._convs = {}
        self._conv_lock = Lock()

    @property
    def knowledge_cards(self):
        """Curated grounded-knowledge library, loaded once per process."""
        if self._knowledge_cards is None:
            self._knowledge_cards = load_cards(ROOT / "knowledge" / "cards_zh_v1.json")
        return self._knowledge_cards

    @property
    def ride_dir(self):
        """Resolve storage only on ride requests, preserving injected store doubles."""
        if self._ride_dir is None:
            self._ride_dir = Path(self.store.path).parent / "data" / "ride_sessions"
        return self._ride_dir

    @property
    def notes_path(self):
        """User-voiced memory lives beside the ride sessions, same data root."""
        return self.ride_dir.parent / "ride_notes.json"

    @property
    def billing(self):
        """Lazy billing metadata store, separate from credentials and waitlist."""
        if self._billing is None:
            self._billing = BillingStore(self.store.path.with_name("billing.db"), credential_path=self.store.path)
        return self._billing

    @property
    def waitlist(self):
        """Resolve the separate local store only when the waitlist is used."""
        if self._waitlist is None:
            self._waitlist = WaitlistStore(self.store.path.with_name("waitlist.db"), credential_path=self.store.path)
        return self._waitlist

    def preflight(self, method, path, headers, port):
        origin = _public_origin() or ("http://127.0.0.1:" + str(port))
        if hasattr(headers, "get_all"):
            for name in ("Host", "Origin", "Sec-Fetch-Site", "X-Coach-Token", "Content-Type", "Content-Length", "Transfer-Encoding", "creem-signature"):
                require(len(headers.get_all(name, [])) <= 1, "Duplicate security headers", "invalid_input")
        # Server-to-server callback: signature replaces browser token and Host allowlist.
        # Never expose browser routes by this exception; forwarded headers are ignored.
        if method == "POST" and path == "/api/coach/billing/webhook":
            require(bool(os.environ.get("CREEM_WEBHOOK_SECRET", "").strip()), "Billing is not configured", "billing_not_configured", 503)
            require(headers.get("Host") is not None and headers.get("Origin") is None and headers.get("Sec-Fetch-Site") in (None, "none"), "Server callback required", "origin_denied", 403)
            require(headers.get("Transfer-Encoding") is None and headers.get("Content-Type") == "application/json", "Fixed-length JSON required", "invalid_body", 415)
            raw = headers.get("Content-Length", "")
            require(type(raw) is str and re.fullmatch(r"[0-9]{1,8}", raw) is not None and 0 < int(raw) <= MAX_BODY_BYTES, "Body size exceeds limit", "body_too_large", 413)
            return int(raw)
        # Public liveness only: no secrets, DB, token or upstream dependencies.
        # Render probes may use their own Host. All other routes stay strict.
        if method == "GET" and path == "/health":
            require(headers.get("Host") is not None, "Host required", "host_denied", 403)
            require(headers.get("Origin") is None and headers.get("Sec-Fetch-Site") in (None, "none"),
                    "Cross-origin access denied", "origin_denied", 403)
            return 0
        require(headers.get("Host") == urlsplit(origin).netloc, "Loopback Host required", "host_denied", 403)
        require((headers.get("Origin") is None and method == "GET") or headers.get("Origin") == origin,
                "Cross-origin access denied", "origin_denied", 403)
        require(headers.get("Sec-Fetch-Site") in (None, "same-origin", "none"), "Cross-site access denied", "origin_denied", 403)
        require(type(path) is str and len(path) <= 2048 and path.startswith("/") and not path.startswith("//"), "Invalid request target")
        parsed = urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment, "Invalid request target")
        require(method in ("GET", "POST"), "Method not allowed", "method_not_allowed", 405)
        font_path = re.fullmatch(r"/fonts/[a-z0-9-]+\.woff2", parsed.path) if method == "GET" else None
        public = method == "GET" and (parsed.path in ("/", "/ride", "/mark", "/privacy", "/terms", "/ride.webmanifest", "/icon.svg", "/api/coach/config") or font_path is not None)
        if not public:
            token = headers.get("X-Coach-Token", "")
            require(type(token) is str and token.isascii() and secrets.compare_digest(token, self.token), "Session token required", "token_denied", 403)
        allowed = ((method == "GET" and (parsed.path in ("/", "/privacy", "/terms", "/api/coach/config", "/api/coach/activities", "/ride", "/mark", "/ride.webmanifest", "/icon.svg", RIDE_LIST, RIDE_NOTES) or font_path is not None)) or
                   (method == "POST" and path in ("/api/coach/connect", "/api/coach/review", "/api/coach/review-l2", "/api/coach/demo-review", "/api/coach/waitlist", "/api/coach/weekly", RIDE_POST, RIDE_REVIEW, RIDE_WEEKLY, RIDE_ROUTE, RIDE_CHAT, RIDE_LIVE, RIDE_IMPORT, RIDE_WEATHER, RIDE_NOTE, RIDE_NOTE_DELETE, RIDE_SPEAK, RIDE_ASR)))
        require(allowed, "Endpoint not found", "not_found", 404)
        if method == "POST":
            asr = path == RIDE_ASR
            require(headers.get("Transfer-Encoding") is None
                    and headers.get("Content-Type") == ("audio/wav" if asr else "application/json"),
                    "Fixed-length body required", "invalid_body", 415)
            raw = headers.get("Content-Length", "")
            require(type(raw) is str and re.fullmatch(r"[0-9]{1,8}", raw) is not None, "Body length invalid", "body_too_large", 413)
            length = int(raw)
            limit = (MAX_IMPORT_BODY_BYTES if path == RIDE_IMPORT
                     else MAX_RIDE_BODY_BYTES if path == RIDE_POST
                     else MAX_AUDIO_BYTES if asr else MAX_BODY_BYTES)
            require(0 < length <= limit, "Body size exceeds limit", "body_too_large", 413)
            return length
        return 0

    def dispatch(self, method, path, headers, body=b"", port=DEFAULT_PORT):
        length = self.preflight(method, path, headers, port)
        if urlsplit(path).path in (RIDE_POST, RIDE_LIST, RIDE_REVIEW, RIDE_WEEKLY, RIDE_ROUTE, RIDE_CHAT, RIDE_LIVE, RIDE_IMPORT, RIDE_WEATHER, RIDE_NOTES, RIDE_NOTE, RIDE_NOTE_DELETE, RIDE_SPEAK, RIDE_ASR):
            self._ride_rate_limit(method)
        if method == "POST" and path == "/api/coach/billing/webhook":
            require(len(body) == length, "Incomplete request body", "invalid_body")
            require(verify_billing(body, headers.get("creem-signature", ""), os.environ.get("CREEM_WEBHOOK_SECRET", "")), "Invalid webhook signature", "billing_signature_invalid", 403)
            try:
                event = strict_json(body)
            except ActionError:
                raise ActionError("invalid_json", "Strict UTF-8 JSON required") from None
            return self.billing.handle(event)
        if method == "GET":
            if path == "/health":
                return {"status": "ok"}
            parsed = urlsplit(path)
            if parsed.path in ("/", "/ride", "/mark", "/privacy", "/terms"):
                return _page({"/": "coach.html", "/ride": "ride.html", "/mark": "mark.html", "/privacy": "privacy.html", "/terms": "terms.html"}[parsed.path])
            if parsed.path in ("/ride.webmanifest", "/icon.svg"):
                filename, mime = {"/ride.webmanifest": ("ride.webmanifest", "application/manifest+json; charset=utf-8"),
                                  "/icon.svg": ("icon.svg", "image/svg+xml")}[parsed.path]
                return ((ROOT / "static" / filename).read_bytes(),
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'", mime)
            font_match = re.fullmatch(r"/fonts/([a-z0-9-]+\.woff2)", parsed.path)
            if font_match is not None:
                font_file = ROOT / "static" / "fonts" / font_match.group(1)
                require(font_file.is_file(), "Endpoint not found", "not_found", 404)
                return (font_file.read_bytes(),
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'", "font/woff2")
            if path == "/api/coach/config":
                key = self.store.get_key()
                return {"billing_enabled": bool(os.environ.get("CREEM_WEBHOOK_SECRET", "").strip()), "subscription_status": self.billing.current_status(), "brand_name": BRAND_NAME, "support_email": SUPPORT_EMAIL, "token": self.token, "connected": key is not None,
                        "masked_key": _masked(key), "verified": False, **self.store.encryption_status,
                        "asr_available": bool(os.environ.get("DASHSCOPE_API_KEY", "").strip()),
                        "mode": "local_only", "external_requests": "Only after explicit list/review actions"}
            parsed = urlsplit(path)
            if parsed.path == RIDE_LIST:
                return self._ride_sessions(parsed.query)
            if parsed.path == RIDE_NOTES:
                require(not parsed.query, "Endpoint not found", "not_found", 404)
                return self._list_notes()
            require(parsed.path == "/api/coach/activities", "Endpoint not found", "not_found", 404)
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
            except ValueError:
                raise ActionError("invalid_input", "Expected one days parameter") from None
            require(set(query) == {"days"} and len(query["days"]) == 1 and re.fullmatch(r"[0-9]{1,3}", query["days"][0]) is not None, "Expected one days parameter")
            days = int(query["days"][0])
            require(1 <= days <= 90, "Days must be between 1 and 90")
            end = self.today()
            activities = self._client().list_activities((end - timedelta(days=days-1)).isoformat(), end.isoformat())
            # Return only display fields, not arbitrary upstream properties.
            return {"activities": [{k: activity.get(k) for k in ("id", "name", "start_date_local", "type", "distance", "moving_time")}
                                   for activity in activities]}
        require(len(body) == length, "Incomplete request body", "invalid_body")
        if path == RIDE_ASR:
            return self._asr_ride(body)
        try:
            payload = strict_json(body)
        except ActionError:
            raise ActionError("invalid_json", "Strict UTF-8 JSON required") from None
        if path == RIDE_POST:
            return self._save_ride(payload)
        if path == RIDE_IMPORT:
            return self._import_ride(payload)
        if path == RIDE_ROUTE:
            return self._route_ride(payload)
        if path == RIDE_WEATHER:
            return self._weather_ride(payload)
        if path == RIDE_NOTE:
            return self._add_note(payload)
        if path == RIDE_NOTE_DELETE:
            return self._delete_note(payload)
        if path == RIDE_WEEKLY:
            return self._weekly_ride(payload)
        if path == RIDE_REVIEW:
            return self._review_ride(payload)
        if path == RIDE_CHAT:
            return self._chat_ride(payload)
        if path == RIDE_LIVE:
            return self._live_ride(payload)
        if path == RIDE_SPEAK:
            return self._speak_ride(payload)
        if path == "/api/coach/waitlist":
            require(type(payload) is dict and set(payload) == {"email"}, "Expected email only")
            status = self.waitlist.register(payload["email"])
            return {"status": status, "message": "Your request has been recorded. No email has been sent."}
        if path == "/api/coach/demo-review":
            require(type(payload) is dict and not payload, "Demo expects an empty object")
            return build_demo_review()
        required = {"api_key"} if path == "/api/coach/connect" else (
            {"week_start"} if path == "/api/coach/weekly" else {"activity_id"})
        require(type(payload) is dict and set(payload) == required, "Request fields do not match")
        if path == "/api/coach/connect":
            key = payload["api_key"]
            IntervalsClient(key, fetch=self.fetch)  # Validate locally, never fetch.
            self.store.save_key(key)
            return {"connected": True, "masked_key": _masked(key), "verified": False, **self.store.encryption_status}
        if path == "/api/coach/weekly":
            return self._weekly(payload["week_start"])
        api = self._client()
        activity = api.get_activity(payload["activity_id"])
        warnings = []
        def optional(label, call, fallback):
            try:
                return call()
            except ICUClientError as error:
                if error.code != "not_found":
                    raise
                warnings.append({"code": "optional_data_missing", "field": label})
                return fallback
        streams = optional("streams", lambda: api.get_streams(payload["activity_id"]), [])
        settings = optional("sport_settings", api.get_sport_settings, None)
        # Wellness follows the reported activity date, not the server's current day.
        day = activity.get("start_date_local") or activity.get("start_date")
        wellness = None
        try:
            day = date.fromisoformat(day[:10]).isoformat() if type(day) is str else None
        except ValueError:
            day = None
        if day:
            wellness = optional("wellness", lambda: api.get_wellness(day), None)
        else:
            warnings.append({"code": "optional_data_missing", "field": "wellness_date"})
        try:
            record = normalize_activity(activity, streams, settings, wellness)
            review = _build_rule_snapshot(record)
        except (GPXError, ReviewError, TypeError, ValueError, OverflowError, RecursionError):
            raise ActionError("invalid_upstream_data", "Activity data could not be normalized", 502) from None
        result = {"review": review, "warnings": warnings, "verified": False}
        if path == "/api/coach/review-l2":
            # Explicit opt-in only; never store the provider key or retry a draft.
            result["llm"] = llm_router.run(
                'review_l2',
                lambda **kw: build_llm_review(copy.deepcopy(review), http_post=self.llm_post, **kw))
        return result

    def _review_ride(self, payload):
        """Read a validated snapshot under lock; never hold disk lock during cloud I/O."""
        require(type(payload) is dict and set(payload) == {"session_id"}, "Expected session_id only")
        identifier = payload["session_id"]
        require(type(identifier) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier) is not None,
                "Invalid session_id")
        with self._ride_lock:
            directory = self.ride_dir
            require(not directory.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            path = directory / (identifier + ".json")
            require(not path.is_symlink(), "Session not found", "not_found", 404)
            try:
                require(path.stat().st_size <= MAX_RIDE_BODY_BYTES, "Invalid stored session", "storage_unavailable", 503)
                memory = validate_session(strict_json(path.read_bytes()))
                require(memory["id"] == identifier, "Invalid stored session", "storage_unavailable", 503)
            except FileNotFoundError:
                raise ActionError("not_found", "Session not found", 404) from None
            except (RideMemoryError, OSError):
                raise ActionError("storage_unavailable", "Ride storage unavailable", 503) from None
            current_time = datetime.fromisoformat(memory["started_at"].replace("Z", "+00:00"))
            activity = memory.get("activity", "ride")
            previous, previous_time = None, None
            history = []  # (when, summary) of non-simulated same-activity earlier sessions
            excluded_simulated = 0
            for candidate in directory.glob("*.json"):
                if candidate == path or candidate.is_symlink():
                    continue
                try:
                    if candidate.stat().st_size > MAX_RIDE_BODY_BYTES:
                        continue
                    stored = validate_session(strict_json(candidate.read_bytes()))
                    when = datetime.fromisoformat(stored["started_at"].replace("Z", "+00:00"))
                    if stored["id"] != identifier and when < current_time and (previous_time is None or when > previous_time):
                        previous, previous_time = stored["summary"], when
                    if stored["id"] == identifier or when >= current_time:
                        continue
                    if stored.get("activity", "ride") != activity:
                        continue
                    if stored["source"] == "simulated":
                        excluded_simulated += 1
                        continue
                    history.append((when, stored["summary"]))
                except (RideMemoryError, ActionError, OSError, ValueError):
                    continue
            history.sort(key=lambda entry: entry[0])
            recent = [summary for _, summary in history[-3:]]
            ordinal = len(history) + 1
        result = llm_router.run(
            'review',
            lambda **kw: build_ride_review(copy.deepcopy(memory), copy.deepcopy(previous),
                                           recent_summaries=copy.deepcopy(recent),
                                           activity_ordinal=ordinal,
                                           excluded_simulated=excluded_simulated,
                                           http_post=self.llm_post, **kw))
        return {"speech_status": result["llm_status"],
                "speech": result["draft"]["speech"] if result["draft"] else None,
                "fallback": result["fallback"], "usage": result["usage"]}

    def _chat_ride(self, payload):
        """Q&A over stored sessions; cloud request outside the disk lock.

        The question is data, never an instruction channel: bounded and validated
        by build_ride_chat. Optional conv_id carries at most _CONV_MAX_TURNS
        recent turns as context — memory-only, TTL-bounded, never persisted.
        """
        require(type(payload) is dict and set(payload) - {"weather", "nav", "tz_offset_min"} in ({"question"}, {"question", "session_id"},
                                                        {"question", "live_id"}, {"question", "conv_id"},
                                                        {"question", "session_id", "conv_id"},
                                                        {"question", "live_id", "conv_id"}),
                "Expected question and optional session_id, live_id or conv_id")
        # Optional client-relayed AMap weather; validated and bounded again by
        # ride_chat._weather_facts, dropped whole when malformed.
        weather = payload.get("weather")
        # Optional client-relayed AMap route state; validated and bounded again
        # by ride_chat._nav_facts, dropped whole when malformed. Nav-intent
        # questions bypass the live lane: they need route facts, not telemetry.
        nav = payload.get("nav")
        nav_intent = any(marker in payload["question"] for marker in NAV_QUESTION_MARKERS)
        live_id = payload.get("live_id")
        if live_id is not None:
            require(type(live_id) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", live_id) is not None,
                    "Invalid live_id")
        # Optional client-relayed timezone offset (minutes east of UTC, like
        # -Date.getTimezoneOffset()): keeps "现在几点" honest on UTC servers.
        tz_offset = payload.get("tz_offset_min")
        if tz_offset is not None:
            require(type(tz_offset) is int and -720 <= tz_offset <= 840, "Invalid tz_offset_min")
        identifier = payload.get("session_id")
        if identifier is not None:
            require(type(identifier) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier) is not None,
                    "Invalid session_id")
        conv_id = payload.get("conv_id")
        if conv_id is not None:
            require(type(conv_id) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", conv_id) is not None,
                    "Invalid conv_id")
        dialog = self._dialog_read(conv_id)
        question_for_record = payload["question"]
        with self._ride_lock:
            directory = self.ride_dir
            require(not directory.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            memory = None
            if identifier is not None:
                path = directory / (identifier + ".json")
                require(not path.is_symlink(), "Session not found", "not_found", 404)
                try:
                    require(path.stat().st_size <= MAX_RIDE_BODY_BYTES, "Invalid stored session", "storage_unavailable", 503)
                    memory = validate_session(strict_json(path.read_bytes()))
                    require(memory["id"] == identifier, "Invalid stored session", "storage_unavailable", 503)
                except FileNotFoundError:
                    raise ActionError("not_found", "Session not found", 404) from None
                except (RideMemoryError, OSError):
                    raise ActionError("storage_unavailable", "Ride storage unavailable", 503) from None
            activity = memory.get("activity", "ride") if memory is not None else None
            history = []  # (when, summary) of non-simulated sessions in scope
            searchable = []  # planner-visible descriptors: when/summary/activity
            excluded_simulated = 0
            for candidate in directory.glob("*.json"):
                if candidate.is_symlink():
                    continue
                try:
                    if candidate.stat().st_size > MAX_RIDE_BODY_BYTES:
                        continue
                    stored = validate_session(strict_json(candidate.read_bytes()))
                    if stored["id"] != candidate.stem:
                        continue
                    if memory is not None and stored["id"] == identifier:
                        continue
                    if activity is not None and stored.get("activity", "ride") != activity:
                        continue
                    when = datetime.fromisoformat(stored["started_at"].replace("Z", "+00:00"))
                    if stored["source"] == "simulated":
                        excluded_simulated += 1
                        continue
                    history.append((when, stored["summary"]))
                    searchable.append({"when": when, "summary": stored["summary"],
                                       "activity": stored.get("activity", "ride")})
                except (RideMemoryError, ActionError, OSError, ValueError):
                    continue
            history.sort(key=lambda entry: entry[0])
            recent = [summary for _, summary in history[-3:]]
            ordinal = len(history) + (1 if memory is not None else 0)
            # Cumulative + current-month aggregates over the same honest scope:
            # real sessions only; distance only when every session recorded it.
            entries = list(history)
            if memory is not None and memory["source"] != "simulated":
                when_memory = datetime.fromisoformat(memory["started_at"].replace("Z", "+00:00"))
                entries.append((when_memory, memory["summary"]))
                searchable.append({"when": when_memory, "summary": memory["summary"],
                                   "activity": memory.get("activity", "ride")})
            totals = None
            week = None
            if entries:
                month_start = self.today().replace(day=1)

                def _minutes(pairs):
                    seconds = sum(summary.get("duration_s") or 0 for _, summary in pairs)
                    return int((Decimal(str(seconds)) / 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

                def _kilometers(pairs):
                    distances = [summary.get("distance_m") for _, summary in pairs]
                    if not distances or any(type(d) not in (int, float) or not -1e15 < d < 1e15 for d in distances):
                        return None
                    total = (Decimal(str(sum(distances))) / 1000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                    return int(total) if total == total.to_integral() else float(total)

                month_entries = [entry for entry in entries
                                 if entry[0].astimezone().date().replace(day=1) == month_start]
                totals = {"sessions_count": len(entries), "duration_min": _minutes(entries),
                          "distance_km": _kilometers(entries), "month_sessions": len(month_entries),
                          "month_duration_min": _minutes(month_entries),
                          "month_distance_km": _kilometers(month_entries)}
                monday = self.today() - timedelta(days=self.today().weekday())
                week_entries = [entry for entry in entries
                                if entry[0].astimezone().date() >= monday]
                week = {"sessions": len(week_entries), "duration_min": _minutes(week_entries),
                        "distance_km": _kilometers(week_entries) if week_entries else None}
            # Usage profile: medians over the same honest scope (real sessions
            # only). Below three records Mark stays silent about "usual".
            profile = _usage_profile(entries)
            # User-voiced notes: verbatim context for the answer. A corrupt
            # note store degrades to no notes — the notes panel reports the
            # error honestly, but one bad file never blocks a question.
            try:
                user_notes = [entry["text"] for entry in load_notes(self.notes_path)]
            except NoteError:
                user_notes = []
        # Two lanes, one door: personal-record questions try the private lane
        # first; everything else tries the grounded knowledge library. The
        # other lane is consulted once when the preferred one cannot answer.
        question = payload["question"]
        # Agent planner: history-search questions get a model-chosen local
        # lookup whose results join the answer facts. The planner call is a
        # cloud request — strictly outside the disk lock. Any planner failure
        # degrades to no search; the base bundle answers exactly as before.
        # None = 未检索（基础包照常作答）；[] = 检索落空（该时段无记录，这本身
        # 是事实）；非空列表 = 检索命中。
        matched_sessions = None
        if (os.environ.get("AGENT_PLANNER", "on") != "off"
                and SEARCH_HINTS.search(question)):
            known_dates = sorted({entry["when"].astimezone().date().isoformat()
                                  for entry in searchable}, reverse=True)
            # Week anchors spare the planner all weekday arithmetic.
            this_monday = self.today() - timedelta(days=self.today().weekday())
            last_monday = this_monday - timedelta(days=7)
            last_sunday = this_monday - timedelta(days=1)
            plan = llm_router.run(
                'planner',
                lambda **kw: plan_searches(question, today=self.today().isoformat(),
                                           known_dates=known_dates,
                                           week_start=this_monday.isoformat(),
                                           last_week_start=last_monday.isoformat(),
                                           last_week_end=last_sunday.isoformat(),
                                           http_post=self.llm_post, **kw),
                question=question)
            if plan.get("llm_status") == "ok" and plan["tools"]:
                found = []
                for tool_call in plan["tools"]:
                    found.extend(search_sessions(searchable, tool_call["args"]))
                matched_sessions = found[:MAX_MATCHES]
        if live_id is not None and not nav_intent and any(marker in question for marker in _LIVE_MARKERS):
            # Snapshot and freshness belong to one validated telemetry update.
            # Never substitute historical records for unavailable current facts.
            with self._live_lock:
                entry = self._live.get(live_id)
                live_facts = None
                if entry is not None and time.monotonic() - entry[2] < _LIVE_FRESH_S:
                    live_facts = entry[0].facts()
                    for name in ("hr", "hr_upper", "hr_lower", "speed_mps"):
                        if name in entry[3]:
                            live_facts[name] = entry[3][name]
            if live_facts is None:
                return {"speech_status": "ok", "speech": LIVE_TEMPLATE,
                        "fallback": "template", "usage": None, "lane": "live"}
            live = llm_router.run(
                'live_answer',
                lambda **kw: build_live_answer(question, live_facts, history=dialog,
                                               http_post=self.llm_post, **kw))
            result = {"speech_status": live["llm_status"], "speech": live["speech"],
                      "fallback": live["fallback"], "usage": live["usage"], "lane": "live"}
            self._dialog_record(conv_id, question_for_record, result)
            return result
        prefer_personal = nav_intent or (
            any(marker in question for marker in _PERSONAL_MARKERS)
            and any(word in question for word in _RECORD_WORDS)
            and not any(marker in question for marker in _ADVICE_MARKERS))

        def personal():
            return "personal", llm_router.run(
                'personal',
                lambda **kw: build_ride_chat(
                    question, session=copy.deepcopy(memory) if memory is not None else None,
                    recent_summaries=copy.deepcopy(recent),
                    activity_ordinal=ordinal if ordinal else None,
                    excluded_simulated=excluded_simulated, totals=totals, history=dialog,
                    profile=profile, week=week, notes=list(user_notes), weather=weather,
                    nav=nav, matched_sessions=matched_sessions,
                    http_post=self.llm_post, **kw),
                question=question)

        def knowledge():
            return "knowledge", llm_router.run(
                'knowledge',
                lambda **kw: build_knowledge_answer(
                    question, self.knowledge_cards, history=dialog,
                    http_post=self.llm_post, **kw))

        def general():
            tzinfo = timezone(timedelta(minutes=tz_offset)) if tz_offset is not None else None
            now_local = datetime.now(tzinfo) if tzinfo is not None else datetime.now().astimezone()
            now_bundle = {'local_time': now_local.strftime('%Y-%m-%d %H:%M'),
                          'weekday': '星期' + '一二三四五六日'[now_local.weekday()]}
            return "general", llm_router.run(
                'general',
                lambda **kw: build_general_answer(
                    question, history=dialog, now=now_bundle, weather=weather,
                    http_post=self.llm_post, **kw))

        # Cross-lane fallback only when the first lane genuinely has nothing:
        # transport failure or no matching card. A verification rejection means
        # the right lane found material but the draft failed honesty checks —
        # the other lane cannot fix that and only produces non-sequitur answers.
        # A knowledge miss goes to the general lane (open chat, safety gates on),
        # not to the personal lane, which would have nothing to say either.
        if prefer_personal:
            lane, result = personal()
            if result["llm_status"] in ("upstream_error", "no_knowledge"):
                lane, result = knowledge()
        else:
            lane, result = knowledge()
            if result["llm_status"] == "no_knowledge":
                lane, result = general()
            elif result["llm_status"] == "upstream_error":
                lane, result = personal()
        final = {"speech_status": result["llm_status"],
                 "speech": result["draft"]["speech"] if result["draft"] else None,
                 "fallback": result["fallback"], "usage": result["usage"], "lane": lane}
        self._dialog_record(conv_id, question_for_record, final)
        return final

    def _dialog_read(self, conv_id):
        """Recent turns for this conversation, or None when unknown/expired."""
        if conv_id is None:
            return None
        now = time.monotonic()
        with self._conv_lock:
            for key in [key for key, entry in self._convs.items() if now - entry[1] >= _CONV_TTL_S]:
                del self._convs[key]
            entry = self._convs.get(conv_id)
            if entry is None:
                return None
            entry[1] = now
            return [dict(turn) for turn in entry[0]] or None

    def _dialog_record(self, conv_id, question, result):
        """Remember one answered turn; only successful speeches are remembered."""
        if conv_id is None or result.get("speech_status") != "ok":
            return
        speech = result.get("speech")
        if type(speech) is not str or not speech.strip():
            return
        now = time.monotonic()
        with self._conv_lock:
            entry = self._convs.get(conv_id)
            turns = [] if entry is None else entry[0]
            turns = (turns + [{"q": question, "a": speech}])[-_CONV_MAX_TURNS:]
            self._convs[conv_id] = [turns, now]

    def _asr_ride(self, body):
        """Push-to-talk ASR relay: WAV bytes in, transcript out; key never leaves."""
        result = transcribe_wav(body, api_key=os.environ.get("DASHSCOPE_API_KEY"),
                                ws_dial=self.ws_dial)
        if result["asr_status"] == "invalid_input":
            raise ActionError("invalid_input", "Invalid WAV audio")
        return result

    def _speak_ride(self, payload):
        """Voice proxy: verified speech text in, a bounded audio URL out.

        The key never leaves the server; the client gets only a signed OSS
        URL (24h) whose host is whitelisted by ride_tts. No key configured
        returns not_configured so the client falls back to browser speech.
        """
        require(type(payload) is dict and set(payload) == {"text"}, "Expected text only")
        result = tts_synthesize(payload["text"],
                                api_key=os.environ.get("DASHSCOPE_API_KEY"),
                                http_post=self.llm_post)
        if result["tts_status"] == "rejected_bad_schema":
            raise ActionError("invalid_input", "Invalid text", 400)
        return result

    def _live_ride(self, payload):
        """In-ride companion: telemetry point in, bounded spoken calls out.

        Live sessions stay in process memory — no disk lock is touched; the
        cloud request happens after state update. Anti-spam is the product:
        at most one spoken call per live session per _LIVE_SILENCE_S.
        """
        require(type(payload) is dict and set(payload) in ({"live_id", "point"}, {"live_id", "point", "activity"}),
                "Expected live_id and point")
        activity = payload.get("activity", "ride")
        require(type(activity) is str and activity in ("ride", "hike", "run"), "Invalid activity")
        live_id = payload["live_id"]
        require(type(live_id) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", live_id) is not None,
                "Invalid live_id")
        require(type(payload["point"]) is dict, "Invalid telemetry point")
        now = time.monotonic()
        entry_point = dict(payload["point"])
        with self._live_lock:
            entry = self._live.get(live_id)
            if entry is not None and now - entry[2] >= _LIVE_IDLE_S:
                entry = None
            session = entry[0] if entry is not None else LiveSession(activity)
            try:
                events = session.ingest(entry_point)
            except ValueError:
                raise ActionError("invalid_json", "Invalid telemetry point", 400) from None
            # Rejected packets neither refresh freshness nor allocate a session.
            for key in [key for key, value in self._live.items() if now - value[2] >= _LIVE_IDLE_S]:
                del self._live[key]
            if entry is None:
                overflow = len(self._live) - _LIVE_MAX_SESSIONS + 1
                if overflow > 0:
                    eldest = sorted(self._live.items(), key=lambda item: item[1][2])[:overflow]
                    for key, _ in eldest:
                        del self._live[key]
                entry = [session, None, now, entry_point]
                self._live[live_id] = entry
            else:
                entry[2] = now
                entry[3] = entry_point
            last_call = entry[1]
            speech_facts = session.facts()
        urgent = any(event.get("type") in _LIVE_URGENT_TYPES for event in events)
        if not events or (not urgent and last_call is not None and now - last_call < _LIVE_SILENCE_S):
            return {"calls": []}
        result = llm_router.run(
            'live',
            lambda **kw: build_live_speech(events, speech_facts,
                                           http_post=self.llm_post, **kw))
        speech = result["speech"]
        if type(speech) is not str or not speech.strip():
            return {"calls": []}
        with self._live_lock:
            if self._live.get(live_id) is entry:
                entry[1] = now
        return {"calls": [{"speech": speech, "speech_status": result["llm_status"],
                           "fallback": result["fallback"], "usage": result["usage"],
                           "urgent": result.get("urgent") is True}],
                "events": [event["type"] for event in events]}

    def _weekly_ride(self, payload):
        """Snapshot/aggregate under disk lock; cloud request always outside it.

        Week dates use the server's local calendar, matching the local browser
        deployment. A remote deployment must align its timezone with its user;
        the current request contract contains no client timezone information.
        """
        require(type(payload) is dict and set(payload) == {"week_start"}, "Expected week_start only")
        raw = payload["week_start"]
        require(type(raw) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) is not None,
                "week_start must be an ISO date (YYYY-MM-DD)")
        try:
            start = date.fromisoformat(raw)
            # Keep both preceding/following boundaries representable.
            start - timedelta(days=7)
            start + timedelta(days=7)
        except (ValueError, OverflowError):
            raise ActionError("invalid_input", "week_start must be an ISO date (YYYY-MM-DD)") from None
        require(start.weekday() == 0, "week_start must be a Monday")
        require(start <= self.today(), "week_start must not be in the future")
        with self._ride_lock:
            directory = self.ride_dir
            require(not directory.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            stored = []
            for candidate in sorted(directory.glob("*.json")):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    if candidate.stat().st_size > MAX_RIDE_BODY_BYTES:
                        continue
                    memory = validate_session(strict_json(candidate.read_bytes()))
                    if memory["id"] != candidate.stem:
                        continue
                    stored.append(memory)
                except (RideMemoryError, ActionError, OSError, ValueError):
                    continue
            current, previous = split_week_sessions(stored, start)
            facts = aggregate_ride_weeks(current, previous)
            profile = _usage_profile(
                [(datetime.fromisoformat(m["started_at"].replace("Z", "+00:00")), m["summary"])
                 for m in stored if m.get("source") != "simulated"])
        # Provider receives detached validated records, never the locked files.
        result = llm_router.run(
            'weekly',
            lambda **kw: build_ride_weekly(current, previous, profile=profile,
                                           http_post=self.llm_post, **kw))
        speech = (result["draft"]["speech"] if result["llm_status"] == "ok" else
                  weekly_template({**facts["week"], "source": facts["source"]}, facts["previous_week"]))
        def public_week(week):
            return {key: week[key] for key in ("count", "total_duration_s")} if week is not None else None
        return {"speech_status": result["llm_status"], "speech": speech,
                "fallback": result["fallback"], "usage": result["usage"],
                "week": public_week(facts["week"]), "previous_week": public_week(facts["previous_week"])}

    def _route_ride(self, payload):
        """Proxy one bicycling/walking plan; the provider key never reaches the client."""
        require(type(payload) is dict and set(payload) in ({"origin", "destination_address"},
                {"origin", "destination_address", "mode"}),
                "Expected origin and destination_address only")
        mode = payload.get("mode", "bicycling")
        require(type(mode) is str and mode in ("bicycling", "walking"), "Invalid mode")
        result = plan_route(payload["origin"], payload["destination_address"],
                            api_key=os.environ.get("AMAP_API_KEY"), http_get=self.nav_get,
                            mode=mode)
        require(result["route_status"] != "invalid_input", "Invalid origin or destination_address")
        return result

    def _weather_ride(self, payload):
        """Proxy one weather lookup; coordinates stay server-side, key never leaves."""
        require(type(payload) is dict and set(payload) == {"lat", "lng"}, "Expected lat and lng only")
        result = fetch_weather(payload["lat"], payload["lng"],
                               api_key=os.environ.get("AMAP_API_KEY"), http_get=self.nav_get)
        require(result["weather_status"] != "invalid_input", "Invalid coordinates")
        return result

    def _list_notes(self):
        """Every user-voiced note, oldest first; storage errors stay honest."""
        with self._ride_lock:
            try:
                notes = load_notes(self.notes_path)
            except NoteError as error:
                status = 503 if error.code in ("storage_unavailable", "storage_corrupt") else 400
                raise ActionError(error.code, str(error), status) from None
        return {"notes": notes}

    def _add_note(self, payload):
        """Store one verbatim user note; the parent directory is created on demand."""
        require(type(payload) is dict and set(payload) == {"text"}, "Expected text only")
        with self._ride_lock:
            require(not self.ride_dir.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            self.ride_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                entry = add_note(self.notes_path, payload["text"])
            except NoteError as error:
                status = 503 if error.code in ("storage_unavailable", "storage_corrupt") else (
                    409 if error.code == "note_limit" else 400)
                raise ActionError(error.code, str(error), status) from None
        return {"status": "saved", "id": entry["id"], "created_at": entry["created_at"]}

    def _delete_note(self, payload):
        require(type(payload) is dict and set(payload) == {"id"}, "Expected id only")
        with self._ride_lock:
            try:
                return remove_note(self.notes_path, payload["id"])
            except NoteError as error:
                status = 404 if error.code == "not_found" else (
                    503 if error.code in ("storage_unavailable", "storage_corrupt") else 400)
                raise ActionError(error.code, str(error), status) from None

    def _ride_rate_limit(self, method):
        """Bound local disk traffic per single-user app, never an upstream quota.

        Existing coach routes have upstream 429 propagation, not a local bucket.
        Ride routes have no upstream; enforce 10 POST / 60 GET per minute here.
        Called once in dispatch, not twice via preflight+dispatch in the handler.
        """
        now = time.monotonic()
        with self._ride_lock:
            bucket = self._ride_requests[method]
            while bucket and now - bucket[0] >= 60:
                bucket.popleft()
            if len(bucket) >= (10 if method == "POST" else 60):
                error = ActionError("rate_limited", "Local ride request limit reached", 429)
                error.retry_after = str(max(1, int(60 - (now - bucket[0])) + 1))
                raise error
            bucket.append(now)

    def _save_ride(self, payload):
        """Validate before I/O; atomic, idempotent writes with no ID overwrite."""
        try:
            session = validate_session(payload)
        except RideMemoryError as error:
            raise ActionError(error.code, str(error)) from None
        raw = dumps(session).encode("utf-8")
        with self._ride_lock:
            require(not self.ride_dir.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            self.ride_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.ride_dir / (session["id"] + ".json")
            require(not target.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            if target.exists():
                require(target.stat().st_size <= MAX_RIDE_BODY_BYTES and
                        strict_json(target.read_bytes()) == session,
                        "Session ID already contains different data", "session_conflict", 409)
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.ride_dir, prefix=".pending-", delete=False) as file:
                        temporary = Path(file.name)
                        file.write(raw)
                        file.flush()
                        os.fsync(file.fileno())
                    temporary.chmod(0o600)
                    os.replace(temporary, target)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        return {"status": "saved", "id": session["id"], "storage": "ephemeral"}

    def _import_ride(self, payload):
        """Build v3 sessions from user-exported GPX/TCX text, then store them.

        The builder validates against the same contract as live rides, so the
        result flows through the standard idempotent save path unchanged.
        data may be one file text or a bounded list (batch import): each entry
        is built and stored independently — one bad file never blocks the rest,
        and per-file rejections carry only a sanitized code, never content.
        """
        require(type(payload) is dict and set(payload) == {"data"}, "Expected data only")
        data = payload["data"]
        if type(data) is str:
            try:
                session = build_import_session(data)
            except ImportBuildError as error:
                raise ActionError(error.code, str(error), 422) from None
            existed = (self.ride_dir / (session["id"] + ".json")).exists()
            result = self._save_ride(session)
            return {"status": "duplicate" if existed else "saved",
                    "id": result["id"], "source": session["source"]}
        require(type(data) is list and 1 <= len(data) <= MAX_IMPORT_BATCH,
                "Expected 1..%d file texts" % MAX_IMPORT_BATCH, "invalid_input")
        results = []
        for entry in data:
            if type(entry) is not str:
                results.append({"status": "error", "code": "invalid_input"})
                continue
            try:
                session = build_import_session(entry)
            except ImportBuildError as error:
                results.append({"status": "error", "code": error.code})
                continue
            existed = (self.ride_dir / (session["id"] + ".json")).exists()
            saved = self._save_ride(session)
            results.append({"status": "duplicate" if existed else "saved", "id": saved["id"]})
        return {"status": "ok",
                "saved": sum(1 for r in results if r["status"] == "saved"),
                "duplicate": sum(1 for r in results if r["status"] == "duplicate"),
                "failed": sum(1 for r in results if r["status"] == "error"),
                "results": results}

    def _ride_sessions(self, query):
        """Return newest persisted metadata only, with a strict 1..20 limit."""
        try:
            params = {} if not query else parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
        except ValueError:
            raise ActionError("invalid_input", "Expected at most one limit parameter") from None
        require(not params or (set(params) == {"limit"} and len(params["limit"]) == 1
                and re.fullmatch(r"[0-9]{1,2}", params["limit"][0]) is not None),
                "Expected limit from 1 to 20")
        limit = int(params["limit"][0]) if params else 20
        require(1 <= limit <= 20, "Expected limit from 1 to 20")
        result, skipped = [], 0
        with self._ride_lock:
            require(not self.ride_dir.is_symlink(), "Ride storage unavailable", "storage_unavailable", 503)
            files = sorted(self.ride_dir.glob("*.json"), key=lambda p: p.lstat().st_mtime_ns, reverse=True)
            for path in files:
                if len(result) == limit:
                    break
                try:
                    if path.is_symlink() or path.stat().st_size > MAX_RIDE_BODY_BYTES:
                        skipped += 1
                        continue
                    session = validate_session(strict_json(path.read_bytes()))
                except (RideMemoryError, ActionError, OSError):
                    skipped += 1
                    continue
                result.append({key: session[key] for key in (
                    "schema_version", "id", "started_at", "ended_at", "summary", "persona_version", "source")})
        return {"sessions": result, "storage": "ephemeral", "skipped_invalid": skipped}

    def _client(self):
        key = self.store.get_key()
        require(key is not None, "Connect an API key first", "auth_required", 401)
        return IntervalsClient(key, fetch=self.fetch)

    def _weekly(self, week_start):
        """Explicit Monday-bounded week summary; wellness fetch is out of scope."""
        require(type(week_start) is str, "week_start must be an ISO Monday")
        try:
            start = date.fromisoformat(week_start)
        except ValueError:
            raise ActionError("invalid_input", "week_start must be an ISO date (YYYY-MM-DD)") from None
        require(start.weekday() == 0, "week_start must be a Monday", "invalid_input")
        require(start <= self.today(), "week_start must not be in the future", "invalid_input")
        api = self._client()
        newest = (start + timedelta(days=6)).isoformat()
        # The engine also receives the preceding week for explicit comparison.
        activities = api.list_activities((start - timedelta(days=7)).isoformat(), newest)
        ctl = atl = None  # Latest reported values within the week; never estimated.
        for row in sorted(activities, key=lambda r: str(r.get("start_date_local") or "")):
            day = str(row.get("start_date_local") or "")[:10]
            if start.isoformat() <= day <= newest:
                for field, name in (("icu_ctl", "ctl"), ("icu_atl", "atl")):
                    value = row.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if name == "ctl":
                            ctl = value
                        else:
                            atl = value
        try:
            summary = build_weekly_summary(activities, None, week_start=start.isoformat(), ctl=ctl, atl=atl)
        except (TypeError, ValueError, OverflowError):
            raise ActionError("invalid_upstream_data", "Weekly data could not be summarized", 502) from None
        return {"weekly": summary, "wellness": "not_fetched", "verified": False}


def _bind_config(host=None, port=None):
    """Resolve CLI/factory > environment > defaults, rejecting unsafe bindings.

    Only IPv4 literals are supported by the existing IPv4 HTTP server. No DNS
    resolution or forwarded-header trust is introduced. Raises ValueError.
    """
    host = os.environ.get("LUMA_COACH_HOST", "127.0.0.1") if host is None else host
    _public_origin()  # fail fast on a malformed deployed origin
    try:
        address = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, TypeError):
        raise ValueError("LUMA_COACH_HOST must be an IPv4 literal") from None
    if port is None:
        # LUMA_COACH_PORT wins; otherwise honor the platform-injected PORT
        # (Render, Zeabur, and most PaaS routers), then the default.
        port = os.environ.get("LUMA_COACH_PORT") or os.environ.get("PORT") or str(DEFAULT_PORT)
    if isinstance(port, bool) or not re.fullmatch(r"[0-9]{1,5}", str(port)):
        raise ValueError("LUMA_COACH_PORT must be an integer from 1024 to 65535")
    port = int(port)
    if not 1024 <= port <= 65535 or port in (8765, 8791, 8793):
        raise ValueError("Choose a non-reserved port from 1024 to 65535")
    if not address.is_loopback:
        if os.environ.get("LUMA_COACH_ALLOW_REMOTE") != "1":
            raise ValueError("Non-loopback binding requires LUMA_COACH_ALLOW_REMOTE=1")
        warnings.warn("REMOTE BINDING: single-user coach has no public user authentication; "
                      "keep private. Business routes still require loopback Host/Origin.",
                      RuntimeWarning, stacklevel=2)
    return str(address), port


def create_server(port=None, app=None, *, host=None):
    """Create a validated server; explicit arguments override environment values."""
    host, port = _bind_config(host, port)
    app = app if app is not None else App()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def reply(self, status, value):
            page = status == 200 and type(value) is tuple
            mime = (value[2] if len(value) == 3 else "text/html; charset=utf-8") if page else "application/json; charset=utf-8"
            raw, csp = value[:2] if page else (dumps(value).encode("utf-8"), "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.send_response(status)
            for key, text in {"Content-Type": mime,
                              "Content-Length": str(len(raw)), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                              "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Content-Security-Policy": csp}.items():
                self.send_header(key, text)
            if not page and value.get("retry_after") is not None:
                self.send_header("Retry-After", value["retry_after"])
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionError):
                pass

        def handle_request(self):
            try:
                self.connection.settimeout(15)
                length = app.preflight(self.command, self.path, self.headers, self.server.server_port)
                body = self.rfile.read(length) if length else b""
                self.reply(200, app.dispatch(self.command, self.path, self.headers, body, self.server.server_port))
            except ActionError as error:
                value = {"error_code": error.code, "message": str(error), "retryable": getattr(error, "retryable", False)}
                if getattr(error, "retry_after", None) is not None:
                    value["retry_after"] = error.retry_after
                self.reply(error.status, value)
            except sqlite3.Error:
                self.reply(503, {"error_code": "storage_unavailable", "message": "Local credential storage unavailable", "retryable": False})
            except (TimeoutError, ConnectionError):
                self.reply(408, {"error_code": "request_timeout", "message": "Request timed out", "retryable": False})
            except OSError:
                self.reply(503, {"error_code": "storage_unavailable", "message": "Local resource unavailable", "retryable": False})
            except Exception:
                self.reply(500, {"error_code": "internal_error", "message": "Request failed; no automatic retry", "retryable": False})
        do_GET = do_POST = do_OPTIONS = do_PUT = do_DELETE = handle_request
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    try:
        server = create_server(args.port, host=args.host)
    except ValueError as error:
        parser.error(str(error))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
