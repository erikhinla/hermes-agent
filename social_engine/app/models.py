"""Pydantic contracts for copy, HTTP, Telegram, Venice, and Postiz."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CTA_LINE = "Start Here → https://transformby10x.ai/"
CTA_URL = "https://transformby10x.ai/"

DraftStatus = Literal[
    "pending_copy",
    "awaiting_approval",
    "revising",
    "approved",
    "processing_assets",
    "staged",
    "failed",
]

VALID_STATUSES: tuple[str, ...] = (
    "pending_copy",
    "awaiting_approval",
    "revising",
    "approved",
    "processing_assets",
    "staged",
    "failed",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_cta(text: str) -> str:
    body = (text or "").strip()
    if CTA_URL.lower() in body.lower() or "start here" in body.lower():
        if CTA_URL not in body:
            body = f"{body}\n\n{CTA_LINE}"
        return body
    if not body:
        return CTA_LINE
    return f"{body}\n\n{CTA_LINE}"


def strip_dash_punctuation(text: str) -> str:
    """Public copy may not use em or en dashes. Keep the CTA arrow."""
    if not text:
        return text
    lines = []
    for line in text.split("\n"):
        if "transformby10x.ai" in line.lower() and "Start Here" in line:
            lines.append(line)
            continue
        cleaned = line.replace("\u2014", ". ").replace("\u2013", ", ")
        cleaned = re.sub(r" {2,}", " ", cleaned)
        lines.append(cleaned)
    return "\n".join(lines)


class CopyBundle(BaseModel):
    """Structured Grok output. video_first lives here so the status CHECK stays intact."""

    model_config = ConfigDict(extra="ignore")

    linkedin: str
    reddit: str
    instagram: str
    facebook: str
    x: list[str]
    youtube_tiktok_script: Optional[str] = None
    quote_line: Optional[str] = None
    video_first: bool = True
    visual_prompt: Optional[str] = None
    flagship: bool = False
    postiz_post_ids: list[str] = Field(default_factory=list)

    @field_validator("x", mode="before")
    @classmethod
    def coerce_x(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(parts) == 1:
            numbered = re.split(r"\n(?=\s*(?:\d+[\).:-]|[-*])\s+)", text)
            parts = [re.sub(r"^\s*(?:\d+[\).:-]|[-*])\s+", "", p).strip() for p in numbered if p.strip()]
        return parts or [text]

    @model_validator(mode="after")
    def lock_public_copy(self) -> "CopyBundle":
        self.linkedin = ensure_cta(strip_dash_punctuation(self.linkedin))
        self.reddit = strip_dash_punctuation(self.reddit)
        if CTA_URL not in self.reddit and "start here" not in self.reddit.lower():
            # Reddit stays almost no promo. Put the URL once, quietly, at the end.
            self.reddit = f"{self.reddit.rstrip()}\n\n{CTA_URL}"
        self.instagram = ensure_cta(strip_dash_punctuation(self.instagram))
        self.facebook = ensure_cta(strip_dash_punctuation(self.facebook))
        self.x = [strip_dash_punctuation(t) for t in self.x]
        if self.x and CTA_URL not in "\n".join(self.x):
            self.x.append(CTA_LINE)
        if self.youtube_tiktok_script:
            self.youtube_tiktok_script = ensure_cta(strip_dash_punctuation(self.youtube_tiktok_script))
        if self.quote_line:
            self.quote_line = strip_dash_punctuation(self.quote_line)
        return self

    def x_text(self) -> str:
        return "\n\n".join(self.x)

    def copy_for(self, platform: str) -> str:
        if platform in ("x", "twitter"):
            return self.x_text()
        if platform in ("story", "reels", "instagram"):
            return self.instagram
        if platform == "carousel":
            return self.linkedin
        if platform == "youtube_thumb":
            return self.youtube_tiktok_script or self.quote_line or self.linkedin
        if platform == "reddit":
            return self.reddit
        if platform == "facebook":
            return self.facebook
        if platform == "linkedin":
            return self.linkedin
        return self.linkedin


class GrokCopyRequest(BaseModel):
    brief: str
    mode: Literal["generate", "revise"] = "generate"
    rejected_copy: Optional[CopyBundle] = None
    feedback_text: Optional[str] = None


class GrokChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class GrokChatRequest(BaseModel):
    model: str
    messages: list[GrokChatMessage]
    temperature: float = 0.4
    response_format: dict[str, str] = Field(default_factory=lambda: {"type": "json_object"})


class GrokChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    content: str
    raw: dict[str, Any] = Field(default_factory=dict)


class VeniceImageRequest(BaseModel):
    prompt: str
    platform: str
    width: int
    height: int
    model: str
    negative_prompt: str = (
        "clipart, cartoon, stock photo, fake dashboard, neon cyber, "
        "template layout, extra fingers, watermark, unreadable text"
    )


class VeniceImageResult(BaseModel):
    platform: str
    image_url: str
    width: int
    height: int
    model: str


class VerifyRequest(BaseModel):
    url: str
    min_width: int
    min_height: int


class VerifyResult(BaseModel):
    ok: bool
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    content_type: Optional[str] = None
    error: Optional[str] = None


class PostizUploadRequest(BaseModel):
    url: str


class PostizMedia(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    path: str
    name: Optional[str] = None


class PostizMediaRef(BaseModel):
    id: str
    path: str


class PostizPostValue(BaseModel):
    content: str
    image: list[PostizMediaRef] = Field(default_factory=list)


class PostizIntegrationRef(BaseModel):
    id: str


class PostizPostItem(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    integration: PostizIntegrationRef
    value: list[PostizPostValue]
    settings: dict[str, Any] = Field(default_factory=dict)


class PostizCreatePostsRequest(BaseModel):
    """Postiz create-post body.

    Postiz documents the discriminator as `type` with enum draft|schedule|now.
    This engine hardcodes draft and never sends schedule or now.
    `status` is a computed alias so tests can assert status == 'draft'.
    It is NOT sent on the wire (Postiz would not expect it).
    """

    type: Literal["draft"] = "draft"
    date: str
    shortLink: bool = False
    tags: list[dict[str, str]] = Field(default_factory=list)
    posts: list[PostizPostItem]

    @model_validator(mode="after")
    def force_draft(self) -> "PostizCreatePostsRequest":
        object.__setattr__(self, "type", "draft")
        return self

    @property
    def status(self) -> Literal["draft"]:
        return "draft"

    def wire_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["type"] = "draft"
        payload.pop("status", None)
        return payload


class PostizCreatePostsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    posts: list[dict[str, Any]] = Field(default_factory=list)


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    is_bot: Optional[bool] = None
    username: Optional[str] = None


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    type: Optional[str] = None


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    message_id: int
    chat: TelegramChat
    text: Optional[str] = None
    from_user: Optional[TelegramUser] = Field(default=None, alias="from")


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    from_user: Optional[TelegramUser] = Field(default=None, alias="from")
    message: Optional[TelegramMessage] = None
    data: Optional[str] = None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")
    update_id: int
    message: Optional[TelegramMessage] = None
    callback_query: Optional[TelegramCallbackQuery] = None


class TelegramSendMessageRequest(BaseModel):
    chat_id: str
    text: str
    reply_markup: Optional[dict[str, Any]] = None


class TelegramSendMessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message_id: Optional[str] = None
    ok: bool = True


class BriefIn(BaseModel):
    brief: str
    telegram_chat_id: Optional[str] = None


class BriefOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    draft_id: str
    version: int
    status: str
    copy_payload: CopyBundle = Field(alias="copy")


class DraftOut(BaseModel):
    draft_id: str
    brief: str
    status: str
    version: int
    current_copy: Optional[CopyBundle] = None
    approved_copy: Optional[CopyBundle] = None
    telegram_chat_id: Optional[str] = None
    last_error: Optional[str] = None
    retry_count: int = 0
    video_first: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class HealthOut(BaseModel):
    status: str
    fake: bool
    db: str


class ApprovalAction(BaseModel):
    kind: Literal["approve", "reject", "ignore"]
    draft_id: Optional[str] = None
    expected_version: Optional[int] = None
    feedback_text: Optional[str] = None
    chat_id: Optional[str] = None
