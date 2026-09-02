"""FastAPI entry: briefs, drafts, Telegram webhook, health, resume."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.clients import (
    FakeGrokClient,
    FakePostizClient,
    FakeTelegramClient,
    FakeVeniceClient,
    GrokClient,
    PostizClient,
    TelegramClient,
    VeniceClient,
    default_clients,
)
from app.config import Settings, get_settings
from app.db import Database, parse_copy
from app.models import (
    ApprovalAction,
    BriefIn,
    BriefOut,
    CopyBundle,
    DraftOut,
    HealthOut,
    TelegramUpdate,
)
from app.pipeline import Pipeline
from app.verify import FetchImage, http_fetch_image

log = logging.getLogger(__name__)

APPROVE_RE = re.compile(r"^\s*approve(?:[:\s]+([0-9a-f]{8,}))?\s*$", re.I)
REJECT_RE = re.compile(r"^\s*reject(?:[:\s]+([0-9a-f]{8,}))?[:\s]*(.*)$", re.I | re.S)


def parse_action(update: TelegramUpdate) -> ApprovalAction:
    if update.callback_query and update.callback_query.data:
        chat_id = None
        if update.callback_query.message:
            chat_id = str(update.callback_query.message.chat.id)
        data = update.callback_query.data.strip()
        parts = data.split(":")
        kind = parts[0].lower()
        draft_id = parts[1] if len(parts) > 1 else None
        version = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        if kind in ("approve", "a"):
            return ApprovalAction(kind="approve", draft_id=draft_id, expected_version=version, chat_id=chat_id)
        if kind in ("reject", "r"):
            return ApprovalAction(kind="reject", draft_id=draft_id, expected_version=version, chat_id=chat_id)
        return ApprovalAction(kind="ignore", chat_id=chat_id)

    if update.message and update.message.text:
        chat_id = str(update.message.chat.id)
        text = update.message.text.strip()
        m = APPROVE_RE.match(text)
        if m:
            return ApprovalAction(kind="approve", draft_id=m.group(1), chat_id=chat_id)
        m = REJECT_RE.match(text)
        if m:
            return ApprovalAction(
                kind="reject",
                draft_id=m.group(1),
                feedback_text=(m.group(2) or "").strip() or text,
                chat_id=chat_id,
            )
        if text.lower().startswith("reject"):
            return ApprovalAction(kind="reject", feedback_text=text[6:].lstrip(" :"), chat_id=chat_id)
    return ApprovalAction(kind="ignore")


def _draft_out(row: dict[str, Any]) -> DraftOut:
    return DraftOut(
        draft_id=row["draft_id"],
        brief=row["brief"],
        status=row["status"],
        version=row["version"],
        current_copy=parse_copy(row.get("current_copy_json")),
        approved_copy=parse_copy(row.get("approved_copy_json")),
        telegram_chat_id=row.get("telegram_chat_id"),
        last_error=row.get("last_error"),
        retry_count=row.get("retry_count") or 0,
        video_first=row.get("video_first") or 1,
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
    )


def create_app(
    settings: Optional[Settings] = None,
    grok: Optional[GrokClient] = None,
    venice: Optional[VeniceClient] = None,
    postiz: Optional[PostizClient] = None,
    telegram: Optional[TelegramClient] = None,
    fetch_image: Optional[FetchImage] = None,
    db: Optional[Database] = None,
) -> FastAPI:
    settings = settings or Settings()
    database = db or Database(settings.db_path())
    if grok is None or venice is None or postiz is None or telegram is None:
        d_grok, d_venice, d_postiz, d_telegram = default_clients(settings)
        grok = grok or d_grok
        venice = venice or d_venice
        postiz = postiz or d_postiz
        telegram = telegram or d_telegram
    fetch_image = fetch_image or http_fetch_image
    pipeline = Pipeline(database, settings, grok, venice, postiz, telegram, fetch_image)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = database
        app.state.pipeline = pipeline
        app.state.grok = grok
        app.state.venice = venice
        app.state.postiz = postiz
        app.state.telegram = telegram
        app.state.fetch_image = fetch_image
        pending = database.list_processing()
        for draft_id in pending:
            asyncio.create_task(pipeline.process_assets(draft_id))
        yield

    app = FastAPI(title="TBTX social engine", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.pipeline = pipeline
    app.state.grok = grok
    app.state.venice = venice
    app.state.postiz = postiz
    app.state.telegram = telegram
    app.state.fetch_image = fetch_image

    def get_pipeline() -> Pipeline:
        return app.state.pipeline

    def get_db() -> Database:
        return app.state.db

    @app.get("/health", response_model=HealthOut)
    async def health() -> HealthOut:
        database.get_draft("__missing__")
        return HealthOut(status="ok", fake=settings.use_fake_clients(), db="ok")

    @app.post("/briefs", response_model=BriefOut)
    async def create_brief(body: BriefIn, pipe: Pipeline = Depends(get_pipeline)) -> BriefOut:
        row = await pipe.create_from_brief(body.brief, body.telegram_chat_id)
        copy = parse_copy(row["current_copy_json"])
        if copy is None:
            raise HTTPException(500, "copy generation produced no bundle")
        return BriefOut(draft_id=row["draft_id"], version=row["version"], status=row["status"], copy_payload=copy)

    @app.get("/drafts/{draft_id}", response_model=DraftOut)
    async def get_draft(draft_id: str, store: Database = Depends(get_db)) -> DraftOut:
        row = store.get_draft(draft_id)
        if not row:
            raise HTTPException(404, "draft not found")
        return _draft_out(row)

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        background: BackgroundTasks,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
        pipe: Pipeline = Depends(get_pipeline),
        store: Database = Depends(get_db),
    ) -> JSONResponse:
        expected = settings.telegram_webhook_secret
        provided = x_telegram_bot_api_secret_token or ""
        if not expected or not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=403, detail="webhook secret mismatch")

        payload = await request.json()
        update = TelegramUpdate.model_validate(payload)
        update_id = str(update.update_id)

        if not store.claim_update_id(update_id):
            return JSONResponse({"ok": True, "idempotent": True})

        action = parse_action(update)
        if action.kind == "ignore":
            return JSONResponse({"ok": True, "ignored": True})

        draft_id = action.draft_id
        if not draft_id and action.chat_id:
            latest = store.latest_awaiting_for_chat(action.chat_id)
            if latest:
                draft_id = latest["draft_id"]
                if action.expected_version is None:
                    action.expected_version = latest["version"]
        if not draft_id:
            return JSONResponse({"ok": True, "ignored": True, "reason": "no draft"})

        row = store.get_draft(draft_id)
        if not row:
            return JSONResponse({"ok": True, "ignored": True, "reason": "missing draft"})

        if action.kind == "approve":
            expected_version = action.expected_version if action.expected_version is not None else row["version"]
            claimed = store.claim_approval(draft_id, expected_version)
            if not claimed:
                return JSONResponse({"ok": True, "claimed": False})
            background.add_task(pipe.process_assets, draft_id)
            return JSONResponse({"ok": True, "claimed": True, "draft_id": draft_id})

        # reject / feedback
        store.set_status(draft_id, "revising")
        feedback = action.feedback_text or "Please revise. Keep the CTA. No new offers."
        background.add_task(pipe.revise, draft_id, feedback)
        return JSONResponse({"ok": True, "revising": True, "draft_id": draft_id})

    @app.get("/media/{name}")
    async def media(name: str):
        path = settings.media_dir() / name
        if not path.exists() or not path.is_file():
            raise HTTPException(404, "media not found")
        return FileResponse(path)

    return app


app = create_app()
