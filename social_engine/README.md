# TBTX social asset pipeline engine

Self-contained FastAPI service. It drafts governed social copy, waits for a Telegram approve/reject, generates Venice stills (motion-first poster frames), verifies pixels, then stages Postiz **drafts**. It never schedules or publishes.

This package sits next to Hermes. It does not rewrite the Hermes CLI.

## Run locally (fake clients)

```bash
cd social_engine
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SOCIAL_ENGINE_FAKE=1 TELEGRAM_WEBHOOK_SECRET=dev-secret uvicorn app.main:app --port 8088
```

## Post a test brief

```bash
curl -sS -X POST http://127.0.0.1:8088/briefs \
  -H 'Content-Type: application/json' \
  -d '{"brief":"Agents ping you when the tools do not agree","telegram_chat_id":"12345"}'
```

Then approve via the Telegram webhook (header must match `TELEGRAM_WEBHOOK_SECRET`):

```bash
curl -sS -X POST http://127.0.0.1:8088/telegram/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Telegram-Bot-Api-Secret-Token: dev-secret' \
  -d '{"update_id":1,"callback_query":{"id":"1","data":"approve:DRAFT_ID:1","message":{"message_id":1,"chat":{"id":12345}}}}'
```

Replace `DRAFT_ID` with the id from `/briefs`. Check `GET /drafts/{draft_id}`.

## Live services

Copy `.env.example` to `.env`. Values come from Infisical, never from a sheet and never committed.

| Name | Used for |
| --- | --- |
| `XAI_API_KEY` or `GROK_API_KEY` | Grok copy |
| `OPENROUTER_API_KEY` | Fallback chat completions |
| `TELEGRAM_BOT_TOKEN` | Approval messages |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook header check |
| `VENICE_API_KEY` | Image generation |
| `POSTIZ_API_KEY` | Upload + create post |
| `POSTIZ_API_URL` | Postiz base URL |
| `POSTIZ_INTEGRATION_*` | Channel ids in Postiz |

Postiz `type`/`status` is hardcoded to `draft` in code. There is no schedule path.

Venice: pixel models (`venice-sd35` default) get `width`+`height`. Aspect models (Qwen / nano-banana / gpt-image family) get `aspect_ratio`. Timeout 45s, three attempts with backoff and jitter.

If Venice returns raw bytes, set `SOCIAL_ENGINE_PUBLIC_BASE_URL` so Postiz can fetch `/media/{file}`.

## Tests

```bash
cd social_engine
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Covers optimistic lock, webhook idempotency, verify failure (Postiz not called), e2e revise+approve to a Postiz draft, and 403 on a bad webhook secret.

## Status machine

`pending_copy` → `awaiting_approval` → `revising` (loop) → `processing_assets` → `staged` | `failed`

Approve uses an atomic version check. Duplicate Telegram `update_id` values return 200 and stop. Startup resumes any row left in `processing_assets`.
