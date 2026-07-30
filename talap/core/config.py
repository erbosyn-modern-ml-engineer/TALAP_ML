from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ─────────────────────────────────────────────────────
    app_env: str = "development"
    app_name: str = "talap-ai-backend"
    app_debug: bool = True

    # ── PostgreSQL ──────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/talap",
        repr=False,
    )
    test_database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/talap_test",
        repr=False,
    )

    # ── Redis ───────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        repr=False,
    )

    # ── DeepSeek runtime ────────────────────────────────────────────────
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: SecretStr | None = None
    deepseek_model_primary: str = "deepseek-v4-flash"
    deepseek_model_escalation: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: int = Field(default=30, gt=0)
    deepseek_max_retries: int = Field(default=1, ge=0, le=5)

    # ── Meta WhatsApp Cloud API ─────────────────────────────────────────
    meta_app_secret: SecretStr | None = None
    meta_verify_token: SecretStr | None = None
    meta_access_token: SecretStr | None = None
    meta_graph_api_version: str | None = None
    meta_phone_number_id: str | None = None
    meta_whatsapp_business_account_id: str | None = None

    # ── Telegram Bot API ────────────────────────────────────────────────
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None

    # ── Internal service authentication ─────────────────────────────────
    internal_service_token: SecretStr | None = None

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _validate_postgresql_asyncpg_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "Database URL must start with 'postgresql+asyncpg://'. "
                "Got scheme that does not match."
            )
        return v
    @field_validator(
        "deepseek_api_key",
        "meta_app_secret",
        "meta_verify_token",
        "meta_access_token",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "internal_service_token",
        mode="before",
    )
    @classmethod
    def _coerce_empty_secret_to_none(
        cls, v: object
    ) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
