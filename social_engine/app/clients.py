"""Live and fake clients. All request/response bodies are Pydantic models."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Optional, Protocol
from uuid import uuid4

import httpx

from app.config import Settings
from app.models import (
    CopyBundle,
    GrokChatRequest,
    GrokCopyRequest,
    PostizCreatePostsRequest,
    PostizCreatePostsResponse,
    PostizMedia,
    PostizUploadRequest,
    TelegramSendMessageRequest,
    TelegramSendMessageResponse,
    VeniceImageRequest,
    VeniceImageResult,
)
from app.prompts import build_messages, fake_copy

log = logging.getLogger(__name__)

ASPECT_MODEL_MARKERS = (
    "qwen-image",
    "nano-banana",
    "gpt-image",
    "grok-imagine",
    "seedream",
    "flux-2",
    "wan-2",
)


class GrokClient(Protocol):
    async def generate_copy(self, request: GrokCopyRequest) -> CopyBundle: ...


class VeniceClient(Protocol):
    async def generate_image(self, request: VeniceImageRequest) -> VeniceImageResult: ...


class PostizClient(Protocol):
    async def upload_from_url(self, request: PostizUploadRequest) -> PostizMedia: ...

    async def create_draft_posts(self, request: PostizCreatePostsRequest) -> PostizCreatePostsResponse: ...


class TelegramClient(Protocol):
    async def send_message(self, request: TelegramSendMessageRequest) -> TelegramSendMessageResponse: ...


def _parse_grok_content(content: str) -> CopyBundle:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    if isinstance(data, dict) and "copy" in data and isinstance(data["copy"], dict):
        data = data["copy"]
    return CopyBundle.model_validate(data)


class LiveGrokClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _endpoint(self) -> tuple[str, str, str]:
        key = self.settings.grok_key()
        if key:
            return self.settings.grok_api_url, key, self.settings.grok_model
        if self.settings.openrouter_api_key:
            return (
                self.settings.openrouter_api_url,
                self.settings.openrouter_api_key,
                self.settings.openrouter_model,
            )
        raise RuntimeError("No XAI_API_KEY, GROK_API_KEY, or OPENROUTER_API_KEY set")

    async def generate_copy(self, request: GrokCopyRequest) -> CopyBundle:
        url, key, model = self._endpoint()
        payload = GrokChatRequest(model=model, messages=build_messages(request))
        body = payload.model_dump()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_grok_content(content)


class FakeGrokClient:
    def __init__(self) -> None:
        self.calls: list[GrokCopyRequest] = []

    async def generate_copy(self, request: GrokCopyRequest) -> CopyBundle:
        self.calls.append(request)
        return fake_copy(request)


def aspect_ratio_for(width: int, height: int) -> str:
    pairs = {
        (1, 1): "1:1",
        (16, 9): "16:9",
        (9, 16): "9:16",
        (4, 5): "4:5",
        (5, 4): "5:4",
        (4, 3): "4:3",
        (3, 2): "3:2",
        (2, 3): "2:3",
        (3, 4): "3:4",
    }
    from math import gcd

    g = gcd(width, height)
    key = (width // g, height // g)
    return pairs.get(key, f"{key[0]}:{key[1]}")


def venice_uses_aspect(model: str) -> bool:
    lowered = model.lower()
    return any(marker in lowered for marker in ASPECT_MODEL_MARKERS)


class LiveVeniceClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _url(self) -> str:
        return self.settings.venice_api_url.rstrip("/") + "/image/generate"

    async def generate_image(self, request: VeniceImageRequest) -> VeniceImageResult:
        if not self.settings.venice_api_key:
            raise RuntimeError("VENICE_API_KEY is not set")
        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "format": "png",
            "hide_watermark": True,
        }
        if venice_uses_aspect(request.model):
            payload["aspect_ratio"] = aspect_ratio_for(request.width, request.height)
        else:
            payload["width"] = request.width
            payload["height"] = request.height

        last_error: Optional[Exception] = None
        delay = 0.5
        data: dict[str, Any] = {}
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    response = await client.post(
                        self._url(),
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self.settings.venice_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    last_error = None
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 2:
                    break
                jitter = random.random() * delay
                await asyncio.sleep(delay + jitter)
                delay *= 2
        if last_error:
            raise RuntimeError(f"Venice generate failed: {last_error}") from last_error

        image_url = _extract_venice_url(data, self.settings, request.platform)
        return VeniceImageResult(
            platform=request.platform,
            image_url=image_url,
            width=request.width,
            height=request.height,
            model=request.model,
        )


def _extract_venice_url(data: dict[str, Any], settings: Settings, platform: str) -> str:
    for key in ("images", "data", "urls"):
        value = data.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str) and first.startswith("http"):
                return first
            if isinstance(first, dict):
                for nested in ("url", "image_url", "src"):
                    if isinstance(first.get(nested), str) and first[nested].startswith("http"):
                        return first[nested]
            if isinstance(first, str) and not first.startswith("http"):
                return _persist_b64(first, settings, platform)
    if isinstance(data.get("url"), str) and data["url"].startswith("http"):
        return data["url"]
    raise RuntimeError("Venice response did not include an image URL or image bytes")


def _persist_b64(b64: str, settings: Settings, platform: str) -> str:
    import base64

    raw = b64
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    blob = base64.b64decode(raw)
    name = f"{platform}-{uuid4().hex}.png"
    dest = settings.media_dir() / name
    dest.write_bytes(blob)
    base = (settings.social_engine_public_base_url or "").rstrip("/")
    if base:
        return f"{base}/media/{name}"
    return dest.resolve().as_uri()


class FakeVeniceClient:
    def __init__(self, image_store: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self.image_store = image_store if image_store is not None else {}
        self.calls: list[VeniceImageRequest] = []
        self.fail_mode: Optional[str] = None

    def set_fail_mode(self, mode: Optional[str]) -> None:
        self.fail_mode = mode

    async def generate_image(self, request: VeniceImageRequest) -> VeniceImageResult:
        from io import BytesIO

        from PIL import Image

        self.calls.append(request)
        url = f"https://cdn.test/assets/{request.platform}.png"
        if self.fail_mode == "bad_size":
            width, height = 12, 12
        elif self.fail_mode == "bad_bytes":
            self.image_store[url] = {
                "status": 200,
                "content_type": "application/octet-stream",
                "body": b"not-an-image",
            }
            return VeniceImageResult(
                platform=request.platform,
                image_url=url,
                width=request.width,
                height=request.height,
                model=request.model,
            )
        elif self.fail_mode == "http_error":
            self.image_store[url] = {
                "status": 500,
                "content_type": "text/plain",
                "body": b"upstream failed",
            }
            return VeniceImageResult(
                platform=request.platform,
                image_url=url,
                width=request.width,
                height=request.height,
                model=request.model,
            )
        else:
            width, height = request.width, request.height

        buf = BytesIO()
        Image.new("RGB", (width, height), (18, 18, 20)).save(buf, format="PNG")
        self.image_store[url] = {
            "status": 200,
            "content_type": "image/png",
            "body": buf.getvalue(),
        }
        return VeniceImageResult(
            platform=request.platform,
            image_url=url,
            width=request.width,
            height=request.height,
            model=request.model,
        )


def normalize_postiz_base(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/public/v1"):
        return base
    if base.endswith("/api"):
        return base + "/public/v1"
    return base + "/public/v1"


class LivePostizClient:
    """Postiz public API adapter.

    Auth: the public docs put the raw API key in the Authorization header,
    not `Bearer <key>`. See https://docs.postiz.com/public-api/introduction
    Keep this adapter easy to swap if a later Postiz version wants Bearer.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = normalize_postiz_base(settings.postiz_api_url)

    def _headers(self) -> dict[str, str]:
        if not self.settings.postiz_api_key:
            raise RuntimeError("POSTIZ_API_KEY is not set")
        return {
            "Authorization": self.settings.postiz_api_key,
            "Content-Type": "application/json",
        }

    async def upload_from_url(self, request: PostizUploadRequest) -> PostizMedia:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{self.base}/upload-from-url",
                json=request.model_dump(),
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
        return PostizMedia.model_validate(data)

    async def create_draft_posts(self, request: PostizCreatePostsRequest) -> PostizCreatePostsResponse:
        if request.type != "draft" or request.status != "draft":
            raise RuntimeError("Refusing to send a non-draft Postiz payload")
        payload = request.wire_payload()
        if payload.get("type") != "draft":
            raise RuntimeError("Postiz payload type escaped draft lock")
        payload.pop("status", None)
        payload.pop("schedule", None)
        payload.pop("publish", None)
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{self.base}/posts",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
        posts = data if isinstance(data, list) else data.get("posts") or [data]
        return PostizCreatePostsResponse(posts=posts)


class FakePostizClient:
    def __init__(self) -> None:
        self.uploads: list[PostizUploadRequest] = []
        self.creates: list[PostizCreatePostsRequest] = []
        self.called = False

    @property
    def last_create(self) -> Optional[PostizCreatePostsRequest]:
        return self.creates[-1] if self.creates else None

    async def upload_from_url(self, request: PostizUploadRequest) -> PostizMedia:
        self.uploads.append(request)
        ident = f"media-{uuid4().hex[:12]}"
        return PostizMedia(id=ident, path=request.url, name=ident)

    async def create_draft_posts(self, request: PostizCreatePostsRequest) -> PostizCreatePostsResponse:
        if request.status != "draft" or request.type != "draft":
            raise RuntimeError("Fake Postiz refused a non-draft payload")
        self.called = True
        self.creates.append(request)
        return PostizCreatePostsResponse(
            posts=[{"postId": f"post-{uuid4().hex[:8]}", "integration": item.integration.id} for item in request.posts]
        )


class LiveTelegramClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send_message(self, request: TelegramSendMessageRequest) -> TelegramSendMessageResponse:
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body: dict[str, Any] = {"chat_id": request.chat_id, "text": request.text}
        if request.reply_markup:
            body["reply_markup"] = request.reply_markup
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            data = response.json()
        result = data.get("result") or {}
        return TelegramSendMessageResponse(
            ok=bool(data.get("ok", True)),
            message_id=str(result.get("message_id")) if result.get("message_id") is not None else None,
        )


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[TelegramSendMessageRequest] = []

    async def send_message(self, request: TelegramSendMessageRequest) -> TelegramSendMessageResponse:
        self.messages.append(request)
        return TelegramSendMessageResponse(ok=True, message_id=str(1000 + len(self.messages)))


def default_clients(settings: Settings) -> tuple[GrokClient, VeniceClient, PostizClient, TelegramClient]:
    if settings.use_fake_clients():
        return FakeGrokClient(), FakeVeniceClient(), FakePostizClient(), FakeTelegramClient()
    grok: GrokClient
    if settings.grok_key() or settings.openrouter_api_key:
        grok = LiveGrokClient(settings)
    else:
        grok = FakeGrokClient()
    venice: VeniceClient = LiveVeniceClient(settings) if settings.venice_api_key else FakeVeniceClient()
    postiz: PostizClient = LivePostizClient(settings) if settings.postiz_api_key else FakePostizClient()
    telegram: TelegramClient = (
        LiveTelegramClient(settings) if settings.telegram_bot_token else FakeTelegramClient()
    )
    return grok, venice, postiz, telegram
