# TBTX social asset pipeline

Hermes stays as the agent CLI. The governed social content engine is a self-contained FastAPI package in `social_engine/`.

Read `social_engine/README.md` for run, curl, and pytest.

Env names (Infisical only; do not put values in git or in the giant `.env.example`):

- `XAI_API_KEY` (or `GROK_API_KEY`)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `VENICE_API_KEY`
- `POSTIZ_API_KEY`
- `POSTIZ_API_URL`
- `OPENROUTER_API_KEY`

Local tests: `SOCIAL_ENGINE_FAKE=1`. Postiz payloads are always drafts.
