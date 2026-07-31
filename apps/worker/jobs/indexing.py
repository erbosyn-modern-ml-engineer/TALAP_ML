"""Focused job facades for the product-indexing worker lifecycle.

These wrappers are the job-layer entry points a future ARQ worker will call.
They never start an infinite loop and never connect to PostgreSQL at import
time: connections are only opened when a function is invoked.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db.models import ProductIndexingTaskStatus
from talap.indexing.worker import (
    ClaimedIndexingTask,
    StaleIndexingTaskClaimError,
    claim_indexing_tasks,
    mark_indexing_task_completed,
    mark_indexing_task_failed,
)

__all__ = [
    "StaleIndexingTaskClaimError",
    "claim_indexing_batch",
    "complete_indexing_task",
    "fail_indexing_task",
]

DEFAULT_CLAIM_LIMIT = 10
DEFAULT_STALE_AFTER = timedelta(minutes=5)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = timedelta(minutes=5)


async def claim_indexing_batch(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int = DEFAULT_CLAIM_LIMIT,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> list[ClaimedIndexingTask]:
    """Claim one batch of due indexing tasks and return their snapshots."""
    return await claim_indexing_tasks(
        session_factory=session_factory,
        limit=limit,
        stale_after=stale_after,
    )


async def complete_indexing_task(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    expected_attempt: int,
) -> None:
    """Mark a claimed task COMPLETED.

    ``expected_attempt`` must be the claim generation from the claiming
    snapshot (``claimed.attempts`` / ``claimed.claim_attempt``); a stale
    claim raises ``StaleIndexingTaskClaimError``.
    """
    await mark_indexing_task_completed(
        session_factory=session_factory,
        task_id=task_id,
        expected_attempt=expected_attempt,
    )


async def fail_indexing_task(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    expected_attempt: int,
    error_message: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay: timedelta = DEFAULT_RETRY_DELAY,
) -> ProductIndexingTaskStatus:
    """Record a failure: retry (PENDING) or permanent FAILED.

    ``expected_attempt`` must be the claim generation from the claiming
    snapshot (``claimed.attempts`` / ``claimed.claim_attempt``); a stale
    claim raises ``StaleIndexingTaskClaimError`` and leaves the task
    unchanged.
    """
    return await mark_indexing_task_failed(
        session_factory=session_factory,
        task_id=task_id,
        expected_attempt=expected_attempt,
        error_message=error_message,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )
