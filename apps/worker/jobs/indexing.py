"""Focused job facades for the product-indexing worker lifecycle.

These wrappers are the job-layer entry points a future ARQ worker will call.
They never start an infinite loop and never connect to PostgreSQL at import
time: connections are only opened when a function is invoked.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.core import get_settings
from talap.db.models import ProductIndexingTaskStatus
from talap.embeddings.jina import JinaEmbeddingClient
from talap.embeddings.types import EmbeddingClient
from talap.indexing.processor import (
    IndexingProcessResult,
    process_claimed_indexing_task,
)
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
    "process_one_indexing_task",
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


def _build_jina_client() -> JinaEmbeddingClient:
    settings = get_settings()
    api_key = settings.jina_api_key.get_secret_value() if settings.jina_api_key else None
    return JinaEmbeddingClient(
        api_key=api_key,
        base_url=settings.jina_base_url,
        model=settings.jina_embedding_model,
        dimensions=settings.jina_embedding_dimensions,
        timeout_seconds=settings.jina_timeout_seconds,
        max_retries=settings.jina_max_retries,
    )


async def process_one_indexing_task(
    *,
    claimed_task: ClaimedIndexingTask,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_client: EmbeddingClient | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay: timedelta = DEFAULT_RETRY_DELAY,
) -> IndexingProcessResult:
    """Process exactly one claimed task into a durable ProductEmbedding.

    Creates the production :class:`JinaEmbeddingClient` from Settings when no
    client is injected (raising ``JinaEmbeddingConfigurationError`` when the
    API key is unconfigured, so production fails safely). Never loops, never
    auto-claims batches, and never runs at import time.
    """
    created_client = embedding_client is None
    client = embedding_client if embedding_client is not None else _build_jina_client()
    try:
        return await process_claimed_indexing_task(
            claimed_task=claimed_task,
            session_factory=session_factory,
            embedding_client=client,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )
    finally:
        if created_client:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
