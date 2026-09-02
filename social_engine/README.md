# TBTX social asset pipeline engine

Self-contained FastAPI service. It drafts governed social copy, waits for a Telegram **or** FLOW dashboard approve/reject, generates Venice stills (motion-first poster frames), verifies pixels, then stages Postiz **drafts**. It never schedules or publishes.

This package sits next to Hermes. It does not rewrite the Hermes CLI.

## View the FLOW dashboard (task envelope + creative review)

The Agent AS dashboard dist is vendored under `dashboard/` (fetched from public `erikhinla/flow-as`, not cloned). The engine serves it and implements the FLOW control API so you do not need docker compose.

```bash
cd social_engine
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SOCIAL_ENGINE_FAKE=1 TELEGRAM_WEBHOOK_SECRET=dev-secret uvicorn app.main:app --port 8088 --host 127.0.0.1
```

Open **http://127.0.0.1:8088/flow-control**

1. Submit a **title** (min 5 chars) and **goal** (min 10). Default risk is reputation → Alpha.
2. After Grok copy, the task is `review_required` with the copy bundle (and media URLs once assets exist).
3. Select the task. **Approve** runs Venice + verify + Postiz **draft only**. **Block** stores the reason and never calls Postiz.

Locked CTA: `Start Here → https://transformby10x.ai/`. No invented offers. No publish path.

Same-origin API: `/api/flow` (what the dashboard calls) and `/v1/flow`. CORS allows localhost:5173, 8088, and 3000 if you run the Vite app separately.

## Post a test brief (Telegram path)

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

Envelope submit (same pipeline):

```bash
curl -sS -X POST http://127.0.0.1:8088/v1/flow/submit \
  -H 'Content-Type: application/json' \
  -d '{"title":"Agents still ping you","goal":"Name the extra job the tools assigned","risk_tier":"reputation"}'
```

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

Covers optimistic lock, webhook idempotency, verify failure (Postiz not called), e2e revise+approve to a Postiz draft, 403 on a bad webhook secret, envelope submit → `review_required` with copy, dashboard approve → Postiz draft, block (no Postiz), and `GET /flow-control` HTML.

## Status machine

Drafts: `pending_copy` → `awaiting_approval` → `revising` (loop) → `processing_assets` → `staged` | `failed`

FLOW envelope (dashboard): `pending` | `review_required` | `active` | `completed` | `failed` | `blocked`

Social briefs: `task_type=content_prep`, `risk_tier=reputation`, `owner_role=alpha`, `review_required=true`. After Grok copy the envelope is `review_required` (draft `awaiting_approval`). Approve uses the same optimistic-lock claim as Telegram, then Venice+verify+Postiz draft only.

## Dashboard vs full BizBrain

Jobs / skills / performance tabs call BizBrain routes that this engine stubs with **200 empty** payloads (`/api/tasks` lists social envelopes; `/api/performance/*` and `/api/intake/queues/status` are empty). Agent Zero review routes are not required by FLOW Control. Full BizBrain still owns worker jobs, skill learning, and perf analysis.
