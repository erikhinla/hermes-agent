import os
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SOCIAL_ENGINE_FAKE", "1")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("SOCIAL_ENGINE_DB", "/tmp/social_engine_import.sqlite")

from app.clients import FakeGrokClient, FakePostizClient, FakeTelegramClient, FakeVeniceClient
from app.config import Settings
from app.main import create_app

SECRET = "test-secret"
SECRET_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


@dataclass
class Harness:
    client: TestClient
    grok: FakeGrokClient
    venice: FakeVeniceClient
    postiz: FakePostizClient
    telegram: FakeTelegramClient
    settings: Settings
    app: object
    images: dict


@pytest.fixture
def harness(tmp_path):
    images: dict = {}
    grok = FakeGrokClient()
    venice = FakeVeniceClient(images)
    postiz = FakePostizClient()
    telegram = FakeTelegramClient()
    settings = Settings(
        social_engine_fake=True,
        social_engine_db=str(tmp_path / "engine.sqlite"),
        telegram_webhook_secret=SECRET,
        telegram_bot_token="fake-token",
        venice_image_model="venice-sd35",
        postiz_api_url="https://api.postiz.com",
        postiz_integration_linkedin="int-linkedin",
        postiz_integration_x="int-x",
        postiz_integration_instagram="int-instagram",
        postiz_integration_facebook="int-facebook",
        postiz_integration_reddit="int-reddit",
        postiz_integration_youtube="int-youtube",
    )

    async def fetch_image(url: str):
        rec = images.get(url)
        if rec is None:
            return 404, "text/plain", b"missing"
        return rec["status"], rec["content_type"], rec["body"]

    app = create_app(
        settings=settings,
        grok=grok,
        venice=venice,
        postiz=postiz,
        telegram=telegram,
        fetch_image=fetch_image,
    )
    with TestClient(app) as client:
        yield Harness(
            client=client,
            grok=grok,
            venice=venice,
            postiz=postiz,
            telegram=telegram,
            settings=settings,
            app=app,
            images=images,
        )


def approve_payload(update_id: int, draft_id: str, version: int, chat_id: int = 42) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "data": f"approve:{draft_id}:{version}",
            "from": {"id": 1, "is_bot": False, "username": "erik"},
            "message": {
                "message_id": 9,
                "chat": {"id": chat_id, "type": "private"},
                "text": "draft",
            },
        },
    }


def reject_payload(update_id: int, draft_id: str, version: int, feedback: str, chat_id: int = 42) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 11,
            "chat": {"id": chat_id, "type": "private"},
            "text": f"REJECT {draft_id} {feedback}",
            "from": {"id": 1, "is_bot": False},
        },
    }
