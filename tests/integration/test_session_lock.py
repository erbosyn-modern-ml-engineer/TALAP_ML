"""Focused deterministic tests for the talap_test session advisory lock.

The session-scoped autouse ``_integration_session_lock`` fixture in conftest.py
holds a PostgreSQL session-level advisory lock on one dedicated connection for
the whole pytest session. These tests verify that behavior without starting
nested pytest processes.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def test_advisory_lock_id_is_a_stable_signed_bigint(
    talap_test_advisory_lock_id: int,
) -> None:
    # Deterministic across runs and processes: fits PostgreSQL BIGINT.
    assert isinstance(talap_test_advisory_lock_id, int)
    assert -(2**63) <= talap_test_advisory_lock_id < 2**63


async def test_session_lock_is_held_by_the_dedicated_connection(
    test_engine: AsyncEngine,
    talap_test_advisory_lock_id: int,
) -> None:
    # The autouse session fixture already holds the TALAP session-level
    # advisory lock on its dedicated connection; a different PostgreSQL
    # session must NOT be able to re-acquire the same lock.
    async with test_engine.connect() as connection:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": talap_test_advisory_lock_id},
            )
        ).scalar_one()
        assert acquired is False


async def test_other_advisory_lock_ids_are_unaffected(
    test_engine: AsyncEngine,
    talap_test_advisory_lock_id: int,
) -> None:
    # A different lock ID is not blocked by the session fixture's lock.
    other_id = talap_test_advisory_lock_id + 1
    async with test_engine.connect() as connection:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": other_id},
            )
        ).scalar_one()
        assert acquired is True
        await connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": other_id},
        )
        await connection.rollback()
