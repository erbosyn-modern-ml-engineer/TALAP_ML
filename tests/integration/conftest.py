from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import pytest_asyncio
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talap.core import get_settings
from talap.db import create_database_engine, create_session_factory

_REPO_ROOT = Path(__file__).resolve().parents[2]

_APP_TABLES = (
    "catalog_import_errors",
    "catalog_imports",
    "inventory",
    "product_variants",
    "product_indexing_tasks",
    "products",
    "merchants",
)

_TRUNCATE_STATEMENT = "TRUNCATE TABLE " + ", ".join(_APP_TABLES) + " CASCADE"


def _assert_test_database() -> None:
    settings = get_settings()
    database_name = urlparse(settings.test_database_url).path.lstrip("/")
    assert database_name == "talap_test", (
        f"Integration tests must target the talap_test database, got {database_name!r}."
    )


def _alembic_head() -> str:
    config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("path_separator", "os")
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("Alembic has no current head revision.")
    return head


@pytest_asyncio.fixture
async def test_engine() -> AsyncIterator[AsyncEngine]:
    _assert_test_database()
    settings = get_settings()
    engine = create_database_engine(settings, database_url=settings.test_database_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    test_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(test_engine)


@pytest_asyncio.fixture(autouse=True)
async def _check_test_revision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    head = _alembic_head()
    async with session_factory() as session:
        db_revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert head == db_revision, (
        f"talap_test revision {db_revision!r} does not match Alembic head {head!r}."
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_app_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as session:
        await session.execute(text(_TRUNCATE_STATEMENT))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(text(_TRUNCATE_STATEMENT))
        await session.commit()


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
