"""Environment-driven settings. Secrets come from env/Infisical only."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    xai_api_key: Optional[str] = None
    grok_api_key: Optional[str] = None
    grok_model: str = "grok-4"
    grok_api_url: str = "https://api.x.ai/v1/chat/completions"

    openrouter_api_key: Optional[str] = None
    openrouter_api_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_model: str = "x-ai/grok-4"

    telegram_bot_token: Optional[str] = None
    telegram_webhook_secret: str = ""

    venice_api_key: Optional[str] = None
    venice_api_url: str = "https://api.venice.ai/api/v1"
    venice_image_model: str = "venice-sd35"

    postiz_api_key: Optional[str] = None
    postiz_api_url: str = "https://api.postiz.com"
    postiz_integration_linkedin: Optional[str] = None
    postiz_integration_x: Optional[str] = None
    postiz_integration_instagram: Optional[str] = None
    postiz_integration_facebook: Optional[str] = None
    postiz_integration_reddit: Optional[str] = None
    postiz_integration_youtube: Optional[str] = None

    social_engine_fake: bool = False
    social_engine_db: str = "social_engine.sqlite"
    social_engine_public_base_url: Optional[str] = None
    social_engine_media_dir: str = "media"

    def grok_key(self) -> Optional[str]:
        return self.xai_api_key or self.grok_api_key

    def use_fake_clients(self) -> bool:
        if self.social_engine_fake:
            return True
        return not self.grok_key()

    def db_path(self) -> Path:
        return Path(self.social_engine_db)

    def media_dir(self) -> Path:
        p = Path(self.social_engine_media_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def postiz_integrations(self) -> dict[str, str]:
        mapping = {
            "linkedin": self.postiz_integration_linkedin,
            "x": self.postiz_integration_x,
            "instagram": self.postiz_integration_instagram,
            "facebook": self.postiz_integration_facebook,
            "reddit": self.postiz_integration_reddit,
            "youtube": self.postiz_integration_youtube,
        }
        return {k: v for k, v in mapping.items() if v}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
