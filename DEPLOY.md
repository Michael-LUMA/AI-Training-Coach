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

Without any encryption backend (non-Windows + no master key) the service **refuses to store keys** (fail closed).

## Render free tier (blueprint ready — awaiting account)

The repo root carries `render.yaml`. Render dashboard → **New → Blueprint** → pick the repo; the blueprint wires everything except one value.

1. Blueprint sets: `startCommand: python coach_server.py --host 0.0.0.0 --port $PORT`, health check `/health`, `LUMA_COACH_ALLOW_REMOTE=1`, auto-generated `LUMA_COACH_MASTER_KEY`, Python 3.12.
2. During the blueprint deploy Render prompts for `LUMA_COACH_PUBLIC_ORIGIN` (marked `sync: false`). First deploy with a placeholder, then set it to the assigned URL (`https://<service>.onrender.com`) and redeploy — without it browser requests are denied by design (fail closed).
3. Verify: `GET /health` → `{"status":"ok"}`; open the URL, connect an Intervals.icu key, list activities, request one review.

## Known limitations (honest list)

- **Single-user**: one credential slot, session token is per-process memory. No public multi-user auth.
- **Free-tier sleep**: Render free services sleep on idle; first request after sleep is slow.
- **SQLite on ephemeral disk**: free tier filesystem is not persistent — stored API keys vanish on redeploy/restart. Users must re-connect. Acceptable for MVP; migration path: persistent disk or hosted KV (not implemented).
- **stdlib-only encryption construction** (HKDF + HMAC-CTR + HMAC-SHA256 tag): not an audited cipher; migrate to Fernet/libsodium once dependencies are allowed.
- Business routes still enforce loopback Host/Origin headers; this is defense-in-depth, **not** public authentication.
- Payment, OAuth, and multi-user accounts are not implemented.
