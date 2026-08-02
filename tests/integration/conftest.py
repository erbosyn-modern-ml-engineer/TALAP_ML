from __future__ import annotations

import hashlib
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

# Stable signed BIGINT advisory lock key dedicated to TALAP talap_test
# integration tests. Session-level (pg_try_advisory_lock), never
# transaction-level, so the lock is released automatically when the owning
# connection closes (e.g. if a pytest process is killed).
TALAP_TEST_ADVISORY_LOCK_ID: int = int.from_bytes(
    hashlib.sha256(b"talap.talap_test.integration.session.v1").digest()[:8],
    byteorder="big",
    signed=True,
)

_APP_TABLES = (
    "message_processing_jobs",
    "whatsapp_delivery_statuses",
    "inbound_messages",
    "inbound_events",
    "channel_connections",
    "catalog_import_errors",
    "catalog_imports",
    "inventory",
    "product_variants",
    "product_indexing_tasks",
    "product_embeddings",
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


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _integration_session_lock() -> AsyncIterator[None]:
    """Serialize concurrent pytest runs against the shared talap_test DB.

    Acquires a PostgreSQL session-level advisory lock
    (``pg_try_advisory_lock``) on ONE dedicated connection that stays open
    for the entire pytest session. Session-level locks survive transaction
    boundaries and are automatically released when the connection closes, so
    a killed pytest process never blocks a later run.

    ``test_engine`` is function-scoped and per-test event loops make reusing
    one pooled engine across loops unsafe, so this fixture opens its own
    dedicated connection through the same test-database engine constructor.
    """
    _assert_test_database()
    settings = get_settings()
    engine = create_database_engine(settings, database_url=settings.test_database_url)
    connection = await engine.connect()
    try:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": TALAP_TEST_ADVISORY_LOCK_ID},
            )
        ).scalar_one()
        await connection.rollback()
    except Exception:
        await connection.close()
        await engine.dispose()
        raise
    if not acquired:
        await connection.close()
        await engine.dispose()
        raise RuntimeError(
            "Another TALAP integration test run is already using talap_test."
        )
    try:
        yield
    finally:
        await connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": TALAP_TEST_ADVISORY_LOCK_ID},
        )
        await connection.rollback()
        await connection.close()
        await engine.dispose()


@pytest_asyncio.fixture
def talap_test_advisory_lock_id() -> int:
    """Expose the stable advisory lock ID to tests without importing conftest."""
    return TALAP_TEST_ADVISORY_LOCK_ID


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
    _integration_session_lock: None,
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
