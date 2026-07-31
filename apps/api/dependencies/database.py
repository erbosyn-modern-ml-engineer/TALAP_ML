from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db import async_session_factory


def get_api_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the runtime async session factory for API dependencies."""
    return async_session_factory
