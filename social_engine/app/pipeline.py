"""Brief -> Grok -> Telegram approval -> Venice -> verify -> Postiz draft."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.clients import (
    FakeVeniceClient,
    GrokClient,
    PostizClient,
    TelegramClient,
    VeniceClient,
)
from app.config import Settings
from app.db import Database, parse_copy
from app.models import (
    CTA_LINE,
    CopyBundle,
    GrokCopyRequest,
    PostizCreatePostsRequest,
    PostizIntegrationRef,
    PostizMediaRef,
    PostizPostItem,
    PostizPostValue,
    PostizUploadRequest,
    TelegramSendMessageRequest,
    VeniceImageRequest,
    VerifyRequest,
)
from app.verify import FetchImage, http_fetch_image, verify_asset

log = logging.getLogger(__name__)

# Pixel sizes the engine generates. Motion-first stills (poster frames).
PLATFORM_SIZES: dict[str, tuple[int, int]] = {
    "linkedin": (1200, 1200),
    "carousel": (1080, 1350),
    "x": (1600, 900),
    "instagram": (1080, 1080),
    "facebook": (1080, 1080),
    "story": (1080, 1920),
    "reels": (1080, 1920),
    "youtube_thumb": (1280, 720),
}

POSTIZ_SETTINGS: dict[str, dict[str, Any]] = {
    "linkedin": {"__type": "linkedin"},
    "x": {"__type": "x", "who_can_reply_post": "everyone"},
    "instagram": {"__type": "instagram", "post_type": "post"},
    "facebook": {"__type": "facebook", "url": "https://transformby10x.ai/"},
    "reddit": {
        "__type": "reddit",
        "subreddit": [
            {
                "value": {
                    "subreddit": "smallbusiness",
                    "title": "The extra job the tools assigned to you",
                    "type": "self",
                    "url": "",
                    "is_flair_required": False,
                    "flair": None,
                }
            }
        ],
    },
    "youtube": {
        "__type": "youtube",
        "title": "Managing Digital Fog",
        "type": "unlisted",
        "selfDeclaredMadeForKids": "no",
    },
}

PLATFORM_TO_INTEGRATION = {
    "linkedin": "linkedin",
    "carousel": "linkedin",
    "x": "x",
    "instagram": "instagram",
    "story": "instagram",
    "reels": "instagram",
    "facebook": "facebook",
    "youtube_thumb": "youtube",
}


class Pipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        grok: GrokClient,
        venice: VeniceClient,
        postiz: PostizClient,
        telegram: TelegramClient,
        fetch_image: Optional[FetchImage] = None,
    ):
        self.db = db
        self.settings = settings
        self.grok = grok
        self.venice = venice
        self.postiz = postiz
        self.telegram = telegram
        self.fetch_image = fetch_image or http_fetch_image

    async def create_from_brief(
        self,
        brief: str,
        telegram_chat_id: Optional[str] = None,
        draft_id: Optional[str] = None,
    ) -> dict[str, Any]:
        draft_id = draft_id or uuid4().hex
        self.db.create_draft(draft_id, brief, telegram_chat_id=telegram_chat_id, video_first=1)
        copy = await self.grok.generate_copy(GrokCopyRequest(brief=brief, mode="generate"))
        row = self.db.save_copy(draft_id, copy, status="awaiting_approval")
        self.db.insert_revision(uuid4().hex, draft_id, row["version"], copy, feedback_text=None)
        if telegram_chat_id:
            await self._notify_telegram(draft_id, telegram_chat_id, copy, row["version"])
        return row

    async def revise(self, draft_id: str, feedback_text: str) -> dict[str, Any]:
        row = self.db.get_draft(draft_id)
        if not row:
            raise RuntimeError(f"draft {draft_id} not found")
        current = parse_copy(row["current_copy_json"])
        self.db.set_status(draft_id, "revising")
        copy = await self.grok.generate_copy(
            GrokCopyRequest(
                brief=row["brief"],
                mode="revise",
                rejected_copy=current,
                feedback_text=feedback_text,
            )
        )
        updated = self.db.save_copy(draft_id, copy, status="awaiting_approval", bump_version=True)
        self.db.insert_revision(
            uuid4().hex, draft_id, updated["version"], copy, feedback_text=feedback_text
        )
        chat_id = updated.get("telegram_chat_id")
        if chat_id:
            await self._notify_telegram(draft_id, chat_id, copy, updated["version"])
        return updated

    async def process_assets(self, draft_id: str) -> dict[str, Any]:
        """Venice + verify + Postiz. Safe to resume from processing_assets."""
        row = self.db.get_draft(draft_id)
        if not row:
            raise RuntimeError(f"draft {draft_id} not found")
        if row["status"] not in ("processing_assets", "approved"):
            return row
        copy = parse_copy(row["approved_copy_json"] or row["current_copy_json"])
        if copy is None:
            self.db.mark_failed(draft_id, "no approved copy to render")
            return self.db.get_draft(draft_id)

        try:
            assets = await self._ensure_assets(draft_id, copy)
            verified = await self._verify_all(draft_id, assets)
            if not verified:
                return self.db.get_draft(draft_id)
            await self._stage_postiz(draft_id, copy, assets)
            self.db.mark_staged(draft_id, copy)
        except Exception as exc:  # noqa: BLE001
            log.exception("asset pipeline failed for %s", draft_id)
            self.db.mark_failed(draft_id, str(exc))
        return self.db.get_draft(draft_id)

    async def _ensure_assets(self, draft_id: str, copy: CopyBundle) -> list[dict[str, Any]]:
        existing = {row["platform"]: row for row in self.db.list_media(draft_id)}
        prompt_base = copy.visual_prompt or (
            "Cinematic still as a poster frame from motion. TBTX brand. "
            "Editorial photoreal, human tension, not clipart."
        )
        for platform, (width, height) in PLATFORM_SIZES.items():
            if platform in existing and existing[platform].get("image_url"):
                continue
            request = VeniceImageRequest(
                prompt=(
                    f"{prompt_base} Platform crop {platform} {width}x{height}. "
                    "Motion-first poster frame, TBTX campaign, not a template."
                ),
                platform=platform,
                width=width,
                height=height,
                model=self.settings.venice_image_model,
            )
            result = await self.venice.generate_image(request)
            asset_id = uuid4().hex
            self.db.upsert_media(
                asset_id=asset_id,
                draft_id=draft_id,
                platform=platform,
                image_url=result.image_url,
                width=result.width,
                height=result.height,
                verified=False,
            )
        return self.db.list_media(draft_id)

    async def _verify_all(self, draft_id: str, assets: list[dict[str, Any]]) -> bool:
        for asset in assets:
            if asset.get("verified"):
                continue
            platform = asset["platform"]
            min_w, min_h = PLATFORM_SIZES[platform]
            result = await verify_asset(
                VerifyRequest(url=asset["image_url"], min_width=min_w, min_height=min_h),
                fetch_image=self.fetch_image,
            )
            if not result.ok:
                self.db.mark_failed(draft_id, f"{platform}: {result.error}")
                return False
            self.db.set_media_verified(asset["asset_id"], result.width or min_w, result.height or min_h, True)
            asset["verified"] = 1
            asset["width"] = result.width
            asset["height"] = result.height
        return True

    async def _stage_postiz(self, draft_id: str, copy: CopyBundle, assets: list[dict[str, Any]]) -> None:
        if copy.postiz_post_ids:
            return
        integrations = self.settings.postiz_integrations()
        if not integrations:
            integrations = {
                "linkedin": "int-linkedin",
                "x": "int-x",
                "instagram": "int-instagram",
                "facebook": "int-facebook",
                "reddit": "int-reddit",
                "youtube": "int-youtube",
            }
            if not self.settings.use_fake_clients() and not isinstance(self.venice, FakeVeniceClient):
                # Live mode with no integration ids: still upload media, skip create.
                integrations = {}

        media_by_platform = {a["platform"]: a for a in assets}
        for asset in assets:
            if asset.get("postiz_media_id"):
                continue
            uploaded = await self.postiz.upload_from_url(PostizUploadRequest(url=asset["image_url"]))
            self.db.set_postiz_media_id(asset["asset_id"], uploaded.id)
            asset["postiz_media_id"] = uploaded.id
            asset["postiz_path"] = uploaded.path

        if not integrations:
            return

        items: list[PostizPostItem] = []
        used = set()
        for platform, asset in media_by_platform.items():
            integ_key = PLATFORM_TO_INTEGRATION.get(platform)
            if not integ_key or integ_key in used:
                continue
            integ_id = integrations.get(integ_key)
            if not integ_id:
                continue
            used.add(integ_key)
            media_id = asset.get("postiz_media_id")
            path = asset.get("postiz_path") or asset["image_url"]
            images = [PostizMediaRef(id=str(media_id), path=path)] if media_id else []
            items.append(
                PostizPostItem(
                    integration=PostizIntegrationRef(id=integ_id),
                    value=[PostizPostValue(content=copy.copy_for(platform), image=images)],
                    settings=POSTIZ_SETTINGS.get(integ_key, {"__type": integ_key}),
                )
            )

        if "reddit" in integrations and "reddit" not in used:
            items.append(
                PostizPostItem(
                    integration=PostizIntegrationRef(id=integrations["reddit"]),
                    value=[PostizPostValue(content=copy.reddit, image=[])],
                    settings=POSTIZ_SETTINGS["reddit"],
                )
            )

        if not items:
            return

        request = PostizCreatePostsRequest(
            type="draft",
            date=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            shortLink=False,
            tags=[],
            posts=items,
        )
        if request.status != "draft":
            raise RuntimeError("Postiz status escaped draft lock")
        response = await self.postiz.create_draft_posts(request)
        copy.postiz_post_ids = [str(p.get("postId") or p.get("id") or "") for p in response.posts]

    async def _notify_telegram(
        self, draft_id: str, chat_id: str, copy: CopyBundle, version: int
    ) -> None:
        preview = (
            f"Draft {draft_id[:8]} v{version} is waiting.\n\n"
            f"Quote: {copy.quote_line or copy.x[0]}\n\n"
            f"LinkedIn (trim):\n{copy.linkedin[:400]}\n\n"
            f"{CTA_LINE}\n\n"
            "APPROVE or REJECT with a note."
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "APPROVE", "callback_data": f"approve:{draft_id}:{version}"},
                    {"text": "REJECT", "callback_data": f"reject:{draft_id}:{version}"},
                ]
            ]
        }
        sent = await self.telegram.send_message(
            TelegramSendMessageRequest(chat_id=str(chat_id), text=preview, reply_markup=markup)
        )
        if sent.message_id:
            self.db.set_telegram_message(draft_id, str(chat_id), str(sent.message_id))
