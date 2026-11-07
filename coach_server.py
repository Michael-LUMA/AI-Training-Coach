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
from datetime import date, datetime, timedelta
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
from ride_review import ReviewError
from ride_memory import validate_session, RideMemoryError
from ride_brain import build_ride_review
from ride_weekly import build_ride_weekly, aggregate_ride_weeks, split_week_sessions, weekly_template
from ride_server import strict_json, dumps

BRAND_NAME = "AI Ride Coach"
SUPPORT_EMAIL = "support@example.invalid"  # Placeholder; replace before public launch.
ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / ".runtime/coach/coach.sqlite3"
MAX_BODY_BYTES = 8192
MAX_RIDE_BODY_BYTES = 2 * 1024 * 1024
RIDE_POST = "/api/coach/ride/session"
RIDE_LIST = "/api/coach/ride/sessions"
RIDE_REVIEW = "/api/coach/ride/review"
RIDE_WEEKLY = "/api/coach/ride/weekly"
DEFAULT_PORT = 8794


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
           "; manifest-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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

    def __init__(self, store=None, *, fetch=None, today=None, llm_post=None):
        self.store = store if store is not None else CoachStore()
        self._billing = None
        self._waitlist = None
        self.fetch = fetch
        self.llm_post = llm_post
        self.today = today if today is not None else date.today
        self.token = secrets.token_urlsafe(32)
        # Credential-adjacent ephemeral data; tests/custom DBs stay isolated.
        self._ride_dir = None
        self._ride_lock = Lock()
        self._ride_requests = {"GET": deque(), "POST": deque()}

    @property
    def ride_dir(self):
        """Resolve storage only on ride requests, preserving injected store doubles."""
        if self._ride_dir is None:
            self._ride_dir = Path(self.store.path).parent / "data" / "ride_sessions"
        return self._ride_dir

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
        public = method == "GET" and parsed.path in ("/", "/ride", "/privacy", "/terms", "/ride.webmanifest", "/icon.svg", "/api/coach/config")
        if not public:
            token = headers.get("X-Coach-Token", "")
            require(type(token) is str and token.isascii() and secrets.compare_digest(token, self.token), "Session token required", "token_denied", 403)
        allowed = ((method == "GET" and parsed.path in ("/", "/privacy", "/terms", "/api/coach/config", "/api/coach/activities", "/ride", "/ride.webmanifest", "/icon.svg", RIDE_LIST)) or
                   (method == "POST" and path in ("/api/coach/connect", "/api/coach/review", "/api/coach/review-l2", "/api/coach/demo-review", "/api/coach/waitlist", "/api/coach/weekly", RIDE_POST, RIDE_REVIEW, RIDE_WEEKLY)))
        require(allowed, "Endpoint not found", "not_found", 404)
        if method == "POST":
            require(headers.get("Transfer-Encoding") is None and headers.get("Content-Type") == "application/json", "Fixed-length JSON required", "invalid_body", 415)
            raw = headers.get("Content-Length", "")
            require(type(raw) is str and re.fullmatch(r"[0-9]{1,8}", raw) is not None, "Body length invalid", "body_too_large", 413)
            length = int(raw)
            limit = MAX_RIDE_BODY_BYTES if path == RIDE_POST else MAX_BODY_BYTES
            require(0 < length <= limit, "Body size exceeds limit", "body_too_large", 413)
            return length
        return 0

    def dispatch(self, method, path, headers, body=b"", port=DEFAULT_PORT):
        length = self.preflight(method, path, headers, port)
        if urlsplit(path).path in (RIDE_POST, RIDE_LIST, RIDE_REVIEW, RIDE_WEEKLY):
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
            if parsed.path in ("/", "/ride", "/privacy", "/terms"):
                return _page({"/": "coach.html", "/ride": "ride.html", "/privacy": "privacy.html", "/terms": "terms.html"}[parsed.path])
            if parsed.path in ("/ride.webmanifest", "/icon.svg"):
                filename, mime = {"/ride.webmanifest": ("ride.webmanifest", "application/manifest+json; charset=utf-8"),
                                  "/icon.svg": ("icon.svg", "image/svg+xml")}[parsed.path]
                return ((ROOT / "static" / filename).read_bytes(),
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'", mime)
            if path == "/api/coach/config":
                key = self.store.get_key()
                return {"billing_enabled": bool(os.environ.get("CREEM_WEBHOOK_SECRET", "").strip()), "subscription_status": self.billing.current_status(), "brand_name": BRAND_NAME, "support_email": SUPPORT_EMAIL, "token": self.token, "connected": key is not None,
                        "masked_key": _masked(key), "verified": False, **self.store.encryption_status,
                        "mode": "local_only", "external_requests": "Only after explicit list/review actions"}
            parsed = urlsplit(path)
            if parsed.path == RIDE_LIST:
                return self._ride_sessions(parsed.query)
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
        try:
            payload = strict_json(body)
        except ActionError:
            raise ActionError("invalid_json", "Strict UTF-8 JSON required") from None
        if path == RIDE_POST:
            return self._save_ride(payload)
        if path == RIDE_WEEKLY:
            return self._weekly_ride(payload)
        if path == RIDE_REVIEW:
            return self._review_ride(payload)
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
            result["llm"] = build_llm_review(
                copy.deepcopy(review), api_key=os.environ.get("DASHSCOPE_API_KEY"), http_post=self.llm_post)
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
            previous, previous_time = None, None
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
                except (RideMemoryError, ActionError, OSError, ValueError):
                    continue
        result = build_ride_review(copy.deepcopy(memory), copy.deepcopy(previous),
                                   api_key=os.environ.get("DASHSCOPE_API_KEY"), http_post=self.llm_post)
        return {"speech_status": result["llm_status"],
                "speech": result["draft"]["speech"] if result["draft"] else None,
                "fallback": result["fallback"], "usage": result["usage"]}

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
        # Provider receives detached validated records, never the locked files.
        result = build_ride_weekly(current, previous,
                                  api_key=os.environ.get("DASHSCOPE_API_KEY"), http_post=self.llm_post)
        speech = (result["draft"]["speech"] if result["llm_status"] == "ok" else
                  weekly_template({**facts["week"], "source": facts["source"]}, facts["previous_week"]))
        def public_week(week):
            return {key: week[key] for key in ("count", "total_duration_s")} if week is not None else None
        return {"speech_status": result["llm_status"], "speech": speech,
                "fallback": result["fallback"], "usage": result["usage"],
                "week": public_week(facts["week"]), "previous_week": public_week(facts["previous_week"])}

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
