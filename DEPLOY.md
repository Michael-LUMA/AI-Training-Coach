# DEPLOY — AI Ride Coach (working title)

中文摘要：当前为Railway＋PostgreSQL私测部署，保留身份与访问门禁；Render步骤和无依赖描述属于历史方案。工程检查与模型质量验收分开记录，未公开发布。

## Latest engineering update — 2026-11-13 (generation grounding)

This section supersedes the older current-status section below. See
`docs/generation-grounding-20261113.md` and `docs/acceptance-generation-20261113/`.
Final deployment-repository regression: 1847 tests, 1842 passed, 5 skipped, no failures.
The candidate fixes event/display provenance, intent guessing, oral climbing labels,
and unsupported equality of overall conditions/performance. qwen-max remains selected.
Recorded v3/v4 diagnostics each accepted 8/10 automatically; semantic review still fails.
The final additional safety guard was not resampled. Model quality is NOT accepted.
M7 restart/readback evidence remains valid by source continuity, not a new restart claim.
Private gates remain; raw GPS is not uploaded; climb_ahead remains disabled.
Blind evaluation and rider materials remain paused. Temporary SSH cleanup is unconfirmed.
Deployed runtime commit: `05f6b16dadad88470208c2a4104d732921092013`.
Railway deployment `0fc0441e-9e90-47cc-8e2e-b5355125c930` is SUCCESS.
Container source hashes and 13 offline replay tests match; seven targeted HTTP gates pass.
The original verification report remains failed due to a mistaken browser-token expectation
on a cloud-token route. See both generation_production_verification JSON files in the
acceptance directory; the follow-up corrects the harness, not product security.
No new model calls/user writes/database restart occurred in that HTTP follow-up.
Documentation-only follow-up commits remain local; they are not a new runtime deployment.

## Current private deployment — 2026-11-13 (historical snapshot)

This section supersedes milestone status below. Current private origin:
`https://ai-ride-coach-production-7887.up.railway.app`; open `/mark` with existing tester credentials.
Runtime code commit: `191b83d69e29f7d3c40cded4211bda4d5a7dccc6`.
Documentation-only follow-up commits remain local and intentionally do not trigger deployment.

M7 cloud summaries/profiles/dialog/notes, export, confirmed hard deletion and frontend identity are deployed.
Actual PostgreSQL DATABASE_URL use and qwen-max on both model routes were checked in the container.
A real container restart at `f790b7a` preserved readback using the original cloud tokens.
Hard-delete five-table zero counts, revoked-token 401 and other-user isolation passed.
The subsequent source diff leaves persistence/auth/export/delete/frontend unchanged; see continuity evidence.
Repeated device IDs return 409, never recover a token. Basic/same-origin/browser/cloud gates remain enabled.
Raw GPS is not uploaded. Browser-local legacy data is distinct from cloud ownership operations.

L3 context/explanation guards and L4 speed-drop/HR-drift/arbitration are implemented for private testing;
this is not release or rider acceptance. L4.3 climb_ahead is disabled without reliable forward terrain.
The latest full Python run: 1834 tests, 1829 passed, 5 skipped, 0 failed.
Six fixed real-model calls: only 2 accepted; 4 template fallbacks. Model quality gate remains FAILED.
Blind evaluation is deferred by owner instruction; old rider materials remain inactive.
No physical iPhone, Safari background, or rider acceptance is claimed.
Temporary acceptance access cleanup remains unconfirmed after a policy-denied command;
see the explicit incomplete-cleanup record in the acceptance evidence.
See `docs/L4-acceptance-20261113.md` and `docs/acceptance-20261113/`.
Historical M7 evidence remains in `docs/data-ownership-M7-20261112.md`.

## Architecture (legacy overview)

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

Model routing defaults (both routes are overridden to qwen-max in the current private deployment): every lane tries DashScope first (fast lanes `qwen-flash`, smart lanes `qwen-plus`; short small talk in the personal lane drops to flash). On `upstream_error`/`error_upstream` only, the call fails over once to the backup provider; verification rejections never fail over. Unset backup vars keep single-provider behavior exactly as before.

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
Historical M2/M3 issue (fixed in M7): device IDs previously recovered existing tokens.
M7 refuses duplicate registrations with 409; do not remove the private gate.
Only allowlisted summaries are persisted; complete sessions and raw telemetry are
rejected by the cloud routes. UI integration, production database acceptance and
container-restart persistence were outstanding at M2/M3; see current M7 acceptance above.
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
