from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from talap.core import get_settings
from talap.core.config import Settings


def create_database_engine(
    settings: Settings,
    *,
    database_url: str | None = None,
) -> AsyncEngine:
    resolved_url = database_url or settings.database_url

    if not resolved_url.startswith("postgresql+asyncpg://"):
        raise ValueError(
            "Database URL must start with 'postgresql+asyncpg://'."
        )

    return create_async_engine(
        resolved_url,
        echo=settings.app_debug,
        pool_pre_ping=True,
    )


def create_session_factory(
    database_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=database_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )


settings = get_settings()
engine = create_database_engine(settings)
async_session_factory = create_session_factory(engine)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_context() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_database_engine(
    database_engine: AsyncEngine = engine,
) -> None:
    await database_engine.dispose()
