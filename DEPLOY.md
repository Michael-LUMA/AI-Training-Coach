# DEPLOY — AI Ride Coach (working title)

中文摘要：Render 免费档部署步骤、环境变量、健康检查与已知限制。本地优先设计；公网部署需显式允许远程绑定，且当前无公开用户认证，仅限单用户演示。

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
