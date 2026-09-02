"""Mechanical asset checks. Fail closed. Never call Postiz on a failed verify."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from app.models import VerifyRequest, VerifyResult

FetchImage = Callable[[str], Awaitable[tuple[int, str, bytes]]]


async def http_fetch_image(url: str) -> tuple[int, str, bytes]:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        if not path.exists():
            return 404, "text/plain", b"missing"
        return 200, "image/png", path.read_bytes()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url)
        content_type = response.headers.get("content-type", "")
        return response.status_code, content_type, response.content


async def verify_asset(
    request: VerifyRequest,
    fetch_image: Optional[FetchImage] = None,
) -> VerifyResult:
    fetcher = fetch_image or http_fetch_image
    try:
        status, content_type, body = await fetcher(request.url)
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(ok=False, url=request.url, error=f"fetch failed: {exc}")

    if status != 200:
        return VerifyResult(
            ok=False,
            url=request.url,
            content_type=content_type,
            error=f"HTTP {status}",
        )
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime and not mime.startswith("image/"):
        return VerifyResult(
            ok=False,
            url=request.url,
            content_type=content_type,
            error=f"content-type {content_type} is not image/*",
        )
    try:
        with Image.open(BytesIO(body)) as image:
            width, height = image.size
            image.load()
    except UnidentifiedImageError:
        return VerifyResult(
            ok=False,
            url=request.url,
            content_type=content_type,
            error="body is not a readable image",
        )
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(
            ok=False,
            url=request.url,
            content_type=content_type,
            error=f"pillow failed: {exc}",
        )

    if width < request.min_width or height < request.min_height:
        return VerifyResult(
            ok=False,
            url=request.url,
            width=width,
            height=height,
            content_type=content_type,
            error=(
                f"dimensions {width}x{height} are below required "
                f"{request.min_width}x{request.min_height}"
            ),
        )
    return VerifyResult(
        ok=True,
        url=request.url,
        width=width,
        height=height,
        content_type=content_type,
    )
