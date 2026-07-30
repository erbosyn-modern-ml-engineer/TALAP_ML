from __future__ import annotations

import pytest
from pydantic import ValidationError

from talap.core.config import Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.test_database_url.startswith("postgresql+asyncpg://")
    assert settings.deepseek_model_primary == "deepseek-v4-flash"
    assert settings.deepseek_model_escalation == "deepseek-v4-pro"


def test_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:secret@localhost:5432/custom",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")

    settings = Settings(_env_file=None)

    assert settings.app_debug is False
    assert settings.database_url == (
        "postgresql+asyncpg://user:secret@localhost:5432/custom"
    )
    assert settings.deepseek_api_key is not None
    assert settings.deepseek_api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)
    assert "test-secret" not in str(settings)


@pytest.mark.parametrize(
    ("field_name", "bad_url"),
    [
        ("database_url", "sqlite:///test.db"),
        ("database_url", "sqlite+aiosqlite:///:memory:"),
        ("database_url", "postgresql://user:pass@localhost/db"),
        ("database_url", "postgresql+psycopg://user:pass@localhost/db"),
        ("test_database_url", "sqlite:///test.db"),
        ("test_database_url", "sqlite+aiosqlite:///:memory:"),
        ("test_database_url", "postgresql://user:pass@localhost/db"),
        ("test_database_url", "postgresql+psycopg://user:pass@localhost/db"),
    ],
)
def test_invalid_database_scheme(field_name: str, bad_url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: bad_url})


def test_empty_secret_fields_default_to_none() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="",
        meta_access_token="",
    )

    assert settings.deepseek_api_key is None
    assert settings.meta_access_token is None


def test_url_fields_with_credentials_are_not_exposed_in_repr() -> None:
    """Passwords in database_url, test_database_url, and redis_url
    must not appear in repr(settings) or str(settings)."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:main-db-secret@localhost:5432/talap",
        test_database_url="postgresql+asyncpg://user:test-db-secret@localhost:5432/talap_test",
        redis_url="redis://:redis-secret@localhost:6379/0",
    )

    output = repr(settings) + str(settings)

    assert "main-db-secret" not in output
    assert "test-db-secret" not in output
    assert "redis-secret" not in output
