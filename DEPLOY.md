# DEPLOY — AI Ride Coach (working title)

中文摘要：Render 免费档部署步骤、环境变量、健康检查与已知限制。本地优先设计；公网部署需显式允许远程绑定，现增加单测试者密码保护，不是公众多用户账号系统。

## Architecture

- Single stdlib Python process (`coach_server.py`). No build step, no dependencies.
- Static pages (`static/coach.html`, `privacy.html`, `terms.html`) served by the same process.
- Upstream: Intervals.icu API (`https://intervals.icu/api/v1`), called only on explicit user actions.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LUMA_COACH_HOST` | no | `127.0.0.1` | Bind address (IPv4 literal only) |
| `LUMA_COACH_PORT` | no | `8794` | Port, 1024–65535, reserved ports rejected |
| `LUMA_COACH_ALLOW_REMOTE` | for public deploy | unset | Must be exactly `1` to bind non-loopback; startup warns loudly |
| `LUMA_COACH_MASTER_KEY` | on non-Windows | unset | Master key for credential encryption (HKDF/HMAC construction) |
| `LUMA_COACH_DB` | no | `.runtime/coach/coach.sqlite3` | Credential DB path |
| `LUMA_COACH_PUBLIC_ORIGIN` | for public deploy | unset | Bare origin of the public URL, e.g. `https://ai-ride-coach.onrender.com`. Retargets the Host/Origin allowlist from loopback to this hostname; malformed values fail startup |
| `DASHSCOPE_API_KEY` | no | unset | Qwen key for Mark's post-ride brain drafts; unset returns `not_configured` and the template speaks instead |
| `AMAP_API_KEY` | no | unset | AMap **Web-service** key for the bicycling route proxy; unset returns `not_configured` and ride recording is unaffected |
| `LLM_BACKUP_BASE_URL` | no | unset | Generic OpenAI-compatible backup provider: full **https** chat/completions URL. All four backup vars must be set and valid or the backup stays out of the plan |
| `LLM_BACKUP_API_KEY` | no | unset | Backup provider bearer key |
| `LLM_BACKUP_MODEL_FAST` | no | unset | Backup model name for fast lanes (live/review/weekly/small talk) |
| `LLM_BACKUP_MODEL_SMART` | no | unset | Backup model name for smart lanes (personal/knowledge Q&A) |

Model routing: every lane tries DashScope first (fast lanes `qwen-flash`, smart lanes `qwen-plus`; short small talk in the personal lane drops to flash). On `upstream_error`/`error_upstream` only, the call fails over once to the backup provider; verification rejections never fail over. Unset backup vars keep single-provider behavior exactly as before.

Without any encryption backend (non-Windows + no master key) the service **refuses to store keys** (fail closed).

## Railway (current live host — deployed 2026-11-07)

Live at `https://ai-ride-coach-production-7887.up.railway.app` (trial credit, ~30 days).

1. Railway dashboard → **New Project → Deploy from GitHub repo** → pick this repo. Railway detects the `Dockerfile` (python:3.12-slim, non-root, port via injected `PORT`).
2. **Networking → Generate Domain** for a public `*.up.railway.app` URL.
3. **Variables**: `LUMA_COACH_ALLOW_REMOTE=1`, `LUMA_COACH_MASTER_KEY=<random 32+ chars>`, `LUMA_COACH_PUBLIC_ORIGIN=https://<assigned-domain>` — the value **must include the `https://` scheme**; a malformed value aborts startup by design and the previous deployment keeps serving.
4. Pushes to `main` auto-redeploy. Verify: `GET /health` → `{"status":"ok"}`; open the URL, connect an Intervals.icu key, list activities, request one review.

## Render free tier (blueprint ready — awaiting card verification)

The repo root carries `render.yaml`. Render dashboard → **New → Blueprint** → pick the repo; the blueprint wires everything except one value. Note: Render asked for a credit-card identity check (2026-11), so the MVP launched on Railway first.

1. Blueprint sets: `startCommand: python coach_server.py --host 0.0.0.0`, health check `/health`, `LUMA_COACH_ALLOW_REMOTE=1`, auto-generated `LUMA_COACH_MASTER_KEY`, Python 3.12.
2. During the blueprint deploy Render prompts for `LUMA_COACH_PUBLIC_ORIGIN` (marked `sync: false`). First deploy with a placeholder, then set it to the assigned URL (`https://<service>.onrender.com`) and redeploy — without it browser requests are denied by design (fail closed).
3. Verify: `GET /health` → `{"status":"ok"}`; open the URL, connect an Intervals.icu key, list activities, request one review.

## Known limitations (honest list)

- **Single-slot demo**: one credential slot shared by all visitors (a later key replaces an earlier one), session token is per-process memory. No public multi-user auth.
- **Trial credit runway**: the Railway trial (~$5) funds roughly 30 days of a tiny always-on service; afterwards migrate or attach payment.
- **SQLite on ephemeral disk**: container filesystem is not persistent — stored API keys vanish on redeploy/restart. Users must re-connect. Acceptable for MVP; migration path: persistent disk or hosted KV (not implemented).
- **stdlib-only encryption construction** (HKDF + HMAC-CTR + HMAC-SHA256 tag): not an audited cipher; migrate to Fernet/libsodium once dependencies are allowed.
- Business routes still enforce loopback Host/Origin headers; this is defense-in-depth, **not** public authentication.
- Payment, OAuth, and multi-user accounts are not implemented.


## Private iPhone test gate — 2026-11-11 (supersedes earlier deployment instructions)

This release is for a **single private tester**, not public registration or a payment service.
Non-loopback binding OR configuring a public origin now requires `LUMA_COACH_TEST_PASSWORD`.
Startup fails before opening a socket if the password is absent/invalid or the public origin is not HTTPS.
Loopback development without a public origin or password is unchanged.

1. Keep the existing platform variables and API keys. Add `LUMA_COACH_TEST_PASSWORD` as a platform secret.
   Generate it locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   It must be 32–128 URL-safe ASCII characters (`A-Z a-z 0-9 _ -`); do not reuse an API key.
   Never put it in source control, a shared document, a URL, or a screenshot.
2. Set `LUMA_COACH_PUBLIC_ORIGIN=https://<actual-assigned-domain>` (no path/trailing slash).
   The platform must terminate trusted HTTPS; do not expose the backend's plain HTTP port directly.
3. Publish the reviewed current working-tree files, not an old HEAD. No publication has been performed by preparing this change.
4. Anonymous `GET /health` remains 200. Anonymous `/mark`, config, records and paid APIs must be 401.
   Open `/mark` with Safari; use HTTP Basic username `boss` and the generated test password.
   Original Host/Origin and `X-Coach-Token` checks remain required after login.
5. Check the `X-Mark-Build` response header. Expected UI fingerprint for this release: `e24e0333cb27228a`.
   This identifies the Mark HTML only, not the entire server or the live deployment state.
6. A desktop browser check is not iPhone acceptance. Test foreground recording/playback, record save and refresh recovery on the phone.
   Lock-screen/background capture is **not promised** by this web release.

Only exact read-only `GET /health` bypasses the gate. Billing webhooks are also blocked without login:
**do not apply this private-test release to a production payment service**.
HTTP Basic has no reliable in-page logout; closing a tab may not clear the browser's cached credentials.
Rotate the platform password and restart the service to revoke access. Do not share the password.
This shared gate is not per-user authorization, rate limiting or a multi-user account system.
Existing ephemeral storage remains ephemeral: export important test records before redeploy/restart;
this change does not provision a persistent volume or preserve server records across deployment.
No paid APIs were invoked by the gate tests; actual phone voice usage may use existing service credits.


## Cloud aggregate memory M2/M3 — 2026-11-11

Optional PostgreSQL persistence is isolated from the legacy SQLite credential slot.
Install `requirements-cloud.txt` (the Dockerfile does this), then set `DATABASE_URL`
through the host's secret configuration. No model API change is required.
Configured database initialization must succeed before the service binds; without
DATABASE_URL existing local features stay available, while cloud endpoints return 503.
New `/api/auth/*` and `/api/rides/*` routes remain behind the private tester login
and same-origin boundary. They do not make Mark a public multi-user product.
Known device IDs currently recover existing tokens: public release requires proof
of token possession / authorized device pairing first. Do not remove the private gate.
Only allowlisted summaries are persisted; complete sessions and raw telemetry are
rejected by the cloud routes. UI integration, production database acceptance and
container-restart persistence verification are still outstanding.
See `docs/cloud-memory-M2-M3-20261111.md` for contracts, limits and release blockers.


## Numeric riding profiles M4 — 2026-11-12

The existing opt-in PostgreSQL schema now also creates `profiles` (derived numeric
cache). No additional model, dependency or UI setup is required. New cloud ride
writes/deletions atomically invalidate the user's cache, then queue a bounded
background refresh. Same-user source mutations and profile refreshes serialize
via a users-row transaction lock; cache misses recompute on demand.
A profile requires at least three completed, positive-duration, non-simulated
rides. Baselines use the most recent 30 rides: integer hour mode (smallest
hour on ties), and duration/distance/HR medians. Distance/HR baselines each
require three observed values; lifetime distance needs only one. Totals use all
eligible history. Missing/insufficient measurements stay null, with counts. China-first calendar
uses UTC+08 and Monday weeks; run/hike are not mixed into riding baselines.
Real PostgreSQL acceptance remains opt-in with a disposable `MARK_TEST_DATABASE_URL`.
No production release, container-restart acceptance or M5 prompt wiring is implied.
See `docs/profile-M4-20261112.md` for exact semantics, tests and remaining blockers.
