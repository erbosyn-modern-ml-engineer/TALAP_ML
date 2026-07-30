from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from talap.core.config import Settings
from talap.db.session import (
    create_database_engine,
    create_session_factory,
    dispose_database_engine,
)


async def test_engine_configuration() -> None:
    settings = Settings(_env_file=None)
    engine = create_database_engine(settings)

    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.url.database == "talap"

    await dispose_database_engine(engine)


async def test_invalid_explicit_override_raises_value_error() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        create_database_engine(
            settings,
            database_url="sqlite+aiosqlite:///:memory:",
        )


async def test_session_factory_creates_independent_sessions() -> None:
    settings = Settings(_env_file=None)
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)

    session_one = factory()
    session_two = factory()

    assert isinstance(session_one, AsyncSession)
    assert isinstance(session_two, AsyncSession)
    assert session_one is not session_two

    # autoflush and expire_on_commit are configured at factory level,
    # which is reflected in the session's info.
    # The async_sessionmaker sets these on the session.
    assert factory.kw.get("autoflush") is False, "autoflush must be disabled"
    assert factory.kw.get("expire_on_commit") is False, (
        "expire_on_commit must be disabled"
    )

    await session_one.close()
    await session_two.close()
    await dispose_database_engine(engine)
